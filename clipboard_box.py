#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JumpBox —— 屏幕上的复制暂存盒

点桌面快捷方式(或全局热键 Ctrl+Alt+B)唤出小盒,把复制的东西 Ctrl+V 存进来,
要用时点击条目放回剪贴板,再去任意地方 Ctrl+V。

- 置顶常驻:永远浮在最上层,点击别处/切换窗口都不会消失(按 Esc 或点 ✕ 才收起)
- 文字超长显示省略号,图片低分辨率缩略图
- 自动捕获可开关(默认关,开启后任何 Ctrl+C 自动入盒)
- 📌 固定条目不会被淘汰或清空;重复存入自动去重上浮
- 上限 30 条(图片最多 10 张);纯内存设计:不读写任何文件、无历史记录,
  进程结束即全部清空
"""

import hashlib
import html
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid

from PyQt5.QtCore import (QAbstractNativeEventFilter, QEvent, QMimeData, QPoint,
                          QPointF, QRectF, QSize, Qt, QTimer, QUrl,
                          pyqtSignal as Signal)
from PyQt5.QtGui import (QColor, QFont, QIcon, QImage, QPainter,
                         QPainterPath, QPixmap)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import (QAction, QApplication, QFrame, QGraphicsDropShadowEffect,
                             QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
                             QSystemTrayIcon, QTextBrowser, QVBoxLayout, QWidget)

INSTANCE_KEY = "ClipboardBoxSingleInstance"   # 单实例互斥/唤起
HOTKEY_ID = 1                                 # 全局热键 Ctrl+Alt+B
WM_HOTKEY = 0x0312

MAX_ITEMS = 30      # 总条目上限
MAX_IMAGES = 10     # 图片条目上限(控制内存占用)
IMG_MAX_EDGE = 2560 # 超大图缩到该边长再存
DEBOUNCE_MS = 120   # 剪贴板抖动合并窗口
SIG_GUARD = True    # 回环守卫用内容签名比对(不再依赖时间窗)

# 设计约定:纯内存工具——除"文件粘贴模式"的临时文件外不读写任何文件、
# 无历史记录,进程结束即全部清空
STAGING_DIR = os.path.join(tempfile.gettempdir(), "JumpBoxPaste")
DEBUG = bool(os.environ.get("JUMPBOX_DEBUG"))  # 调试:普通窗口(UIA 可见)+ 行为日志

STYLES = """
#box { background: rgba(249,251,254,246); border: 1px solid #d3dae4; border-radius: 12px; }
#titleLbl { color:#2b3342; font-weight:600; font-size:13px; background:transparent; }
#mini { border:none; background:transparent; color:#c3cad6; font-size:13px;
        padding:2px 5px; border-radius:6px; }
QFrame#row:hover ~ #mini { color:#8b95a7; }
#mini:hover { color:#2f7cf6; background:#e9f1ff; }
#mini:checked { color:#2f7cf6; background:#e3edff; }
#row { background:#ffffff; border:1px solid #e7ebf1; border-radius:10px; }
#row:hover { border-color:#b9d2f5; background:#f5f9ff; }
#row[fixed="true"] { border-color:#efd9a8; background:#fffdf3; }
#row[selected="true"] { border-color:#4c8bf5; background:#e8f0fe; }
#typeIcon { font-size:14px; }
#preview { color:#242b38; font-size:12px; background:transparent; }
#thumb { background:#f2f4f8; border:1px solid #e7ebf1; border-radius:6px; }
#timeLbl { color:#a2aab8; font-size:10px; background:transparent; }
#timeLbl[flashed="true"] { color:#2f9e5f; font-size:11px; font-weight:600; }
#hint { color:#9aa3b2; font-size:11px; background:transparent; }
#empty { color:#a6aebc; font-size:12px; background:transparent; }
#footerBtn { border:none; background:transparent; color:#4c7fe8; font-size:12px;
             padding:3px 8px; border-radius:6px; }
#footerBtn:hover { background:#e9f1ff; }
#countLbl { color:#9aa3b2; font-size:11px; background:transparent; }
QScrollArea { border:none; background:transparent; }
"""


# ---------------------------------------------------------------- utilities

def fmt_ts(ts: float) -> str:
    """今天显示 HH:MM,更早显示 MM-DD HH:MM。"""
    lt, now = time.localtime(ts), time.localtime()
    same_day = time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d", now)
    return time.strftime("%H:%M", lt) if same_day else time.strftime("%m-%d %H:%M", lt)


def text_preview(text: str) -> str:
    """条目预览:只取第一段开头的文字(视觉省略号由 ClipRow 按宽度做)。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return "(空白文本)"
    return lines[0][:200]


def image_sig(img: QImage):
    """图片内容签名:对位图做一次哈希。结果随条目缓存,去重时不再重算。"""
    try:
        h = hashlib.blake2b(img.constBits().asstring(img.sizeInBytes()),
                            digest_size=16).hexdigest()
    except Exception:
        h = str(img.cacheKey())
    return ("image", img.width(), img.height(), h)


def text_sig(text: str):
    return ("text", hashlib.blake2b(text.encode("utf-8", "surrogatepass"),
                                    digest_size=16).hexdigest())


def normalize_image(img: QImage) -> QImage:
    m = max(img.width(), img.height())
    if m > IMG_MAX_EDGE:
        # scaled 返回全新图像,无需再 copy
        return img.scaled(IMG_MAX_EDGE, IMG_MAX_EDGE,
                          Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return img.copy()  # 脱离剪贴板缓冲,确保长期持有


def _dbg(msg):
    """调试日志(仅 JUMPBOX_DEBUG=1 时启用)。"""
    try:
        with open(os.path.join(tempfile.gettempdir(), "jumpbox_debug.log"),
                  "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def make_app_icon() -> QIcon:
    """程序化画一个托盘/窗口图标:蓝色圆角盒。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#4c8bf5"))
    p.drawRoundedRect(6, 18, 52, 38, 10, 10)                 # 盒身
    p.setBrush(QColor("#9dc1fb"))
    p.drawRoundedRect(12, 8, 40, 16, 7, 7)                   # 盒盖
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(26, 30, 12, 12, 3, 3)                  # 锁扣
    p.end()
    return QIcon(pm)


_PIN_ICON_CACHE = {}


def pin_icon(red: bool) -> QIcon:
    """图钉图标:未固定灰色,固定红色(带白色头带,斜置)。"""
    if red not in _PIN_ICON_CACHE:
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        head = QColor("#E5484D") if red else QColor("#B7BFCA")
        band = QColor("#FFFFFF") if red else QColor("#F2F4F8")
        collar = QColor("#C22F35") if red else QColor("#939CAC")
        needle = QColor("#B9C0CC") if red else QColor("#C6CCD6")
        p.setPen(Qt.NoPen)
        p.translate(16, 16)
        p.rotate(45)
        p.setBrush(needle)
        p.drawRect(QRectF(-1.2, 0, 2.4, 13))
        p.setBrush(collar)
        p.drawRoundedRect(QRectF(-4.5, -3, 9, 4.5), 1.5, 1.5)
        p.setBrush(head)
        p.drawEllipse(QPointF(0, -8.5), 8.5, 7.5)
        p.setBrush(band)
        p.drawRoundedRect(QRectF(-6, -11, 12, 3.6), 1.5, 1.5)
        p.end()
        _PIN_ICON_CACHE[red] = QIcon(pm)
    return _PIN_ICON_CACHE[red]


# ---------------------------------------------------------------- row widget

class ClipRow(QFrame):
    clicked = Signal(str)
    pinToggled = Signal(str)
    removeRequested = Signal(str)
    showFull = Signal(str)

    def __init__(self, item: dict, parent=None):
        super().__init__(parent)
        self.item_id = item["id"]
        self.token = item.get("sig") or ("id", item["id"])  # 内容令牌,增量刷新用
        self._time_text = fmt_ts(item["ts"])
        self._flash = False
        self.setObjectName("row")
        if item.get("pinned"):
            self.setProperty("fixed", "true")
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 4, 6)
        lay.setSpacing(6)

        if item["kind"] == "image":
            icon_lbl = QLabel()
            icon_lbl.setObjectName("thumb")
            pm = QPixmap.fromImage(item.get("thumb") or item["image"]).scaled(
                QSize(64, 48), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_lbl.setPixmap(pm)
            icon_lbl.setFixedSize(66, 50)
            icon_lbl.setAlignment(Qt.AlignCenter)
            lay.addWidget(icon_lbl)
            self._base_preview = (f"图片 {item.get('w') or item['image'].width()}"
                                  f"×{item.get('h') or item['image'].height()}")
        else:
            ticon = QLabel("📝" if len(item.get("text", "")) < 400 else "📄")
            ticon.setObjectName("typeIcon")
            lay.addWidget(ticon)
            self._base_preview = text_preview(item.get("text", ""))
        self._preview_lbl = QLabel()

        self._preview_lbl.setObjectName("preview")
        self._preview_lbl.setTextInteractionFlags(Qt.NoTextInteraction)

        mid = QVBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(2)
        mid.addWidget(self._preview_lbl)
        self._time_lbl = QLabel(self._time_text)
        self._time_lbl.setObjectName("timeLbl")
        mid.addWidget(self._time_lbl)
        lay.addLayout(mid, 1)

        self._full_btn = QPushButton("显全")
        self._full_btn.setObjectName("mini")
        self._full_btn.setProperty("fullBtn", True)
        self._full_btn.setToolTip("查看全文/大图(点空白处或再点一次关闭)")
        self._full_btn.setCursor(Qt.PointingHandCursor)
        self._full_btn.clicked.connect(lambda _: self.showFull.emit(self.item_id))

        self._pin_btn = QPushButton()
        self._pin_btn.setObjectName("mini")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setToolTip("固定(不会被自动淘汰或清空)")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setFixedSize(22, 22)
        self._pin_btn.setIconSize(QSize(16, 16))
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        self.apply_pin_state(bool(item.get("pinned")))

        del_btn = QPushButton("🗑")
        del_btn.setObjectName("mini")
        del_btn.setToolTip("删除这条")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(lambda _: self.removeRequested.emit(self.item_id))

        lay.addWidget(self._full_btn)
        lay.addWidget(self._pin_btn)
        lay.addWidget(del_btn)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._apply_elide)  # 布局稳定后按实际宽度省略

    def _apply_elide(self):
        w = max(60, self._preview_lbl.width())
        self._preview_lbl.setText(
            self._preview_lbl.fontMetrics().elidedText(
                self._base_preview, Qt.ElideRight, w))

    def _on_pin_toggled(self, checked: bool):
        self.apply_pin_state(checked)
        self.pinToggled.emit(self.item_id)

    def apply_pin_state(self, checked: bool):
        self._pin_btn.blockSignals(True)
        self._pin_btn.setChecked(checked)
        self._pin_btn.setIcon(pin_icon(checked))
        self._pin_btn.blockSignals(False)

    def set_selected(self, sel: bool):
        self.setProperty("selected", "true" if sel else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.item_id)
        super().mousePressEvent(e)

    def flash_copied(self):
        """短暂显示『已复制』反馈。"""
        if self._flash:
            return
        self._flash = True
        self._time_lbl.setProperty("flashed", "true")
        self._time_lbl.setText("✓ 已复制,可 Ctrl+V")
        self._time_lbl.style().unpolish(self._time_lbl)
        self._time_lbl.style().polish(self._time_lbl)
        QTimer.singleShot(1200, self._unflash)

    def _unflash(self):
        try:
            self._flash = False
            self._time_lbl.setProperty("flashed", "false")
            self._time_lbl.setText(self._time_text)
            self._time_lbl.style().unpolish(self._time_lbl)
            self._time_lbl.style().polish(self._time_lbl)
        except RuntimeError:
            pass  # 行已被增量刷新删除,反馈随行失效


# ---------------------------------------------------------------- full popup

POP_STYLES = """
#popCard { background:#ffffff; border:1px solid #dbe3ee; border-radius:12px; }
#popHead { background:#f4f8ff; border-top-left-radius:12px; border-top-right-radius:12px;
           border-bottom:1px solid #e8eef7; }
#popTitle { color:#31405a; font-size:12px; font-weight:600; background:transparent; }
#popMeta { color:#9aa3b2; font-size:11px; background:transparent; }
#popClose { border:none; background:transparent; color:#b7c0cd; font-size:13px;
            border-radius:6px; }
#popClose:hover { color:#e5484d; background:#fdecec; }
QTextBrowser { background:transparent; border:none; color:#2a3242; font-size:12px; }
"""


def _rounded_pixmap(pm: QPixmap, radius: int) -> QPixmap:
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pm.width(), pm.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def _sanitize_name(s: str) -> str:
    for ch in '\\/:*?"<>|\n\r\t':
        s = s.replace(ch, "")
    return (s.strip() or "内容")[:16]


class FullPopup(QFrame):
    """"显全"气泡:圆角卡片 + 投影,标题栏显示类型/字数/时间;失活或 ✕ 关闭。"""

    closeRequested = Signal()

    def __init__(self, item: dict):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.item_id = item["id"]
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(POP_STYLES)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 24)   # 底部多留,容纳投影
        card = QFrame()
        card.setObjectName("popCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(30, 40, 60, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        clam = QVBoxLayout(card)
        clam.setContentsMargins(0, 0, 0, 0)
        clam.setSpacing(0)

        # 头部:标题 + 元信息 + 关闭
        head = QFrame()
        head.setObjectName("popHead")
        head.setFixedHeight(36)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 0, 8, 0)
        hl.setSpacing(6)
        if item["kind"] == "text":
            title, meta_txt = "📝 全文", f"{len(item.get('text') or '')} 字"
        else:
            title = "🖼 大图"
            meta_txt = (f"{item.get('w') or item['image'].width()}×"
                        f"{item.get('h') or item['image'].height()}")
        t = QLabel(title)
        t.setObjectName("popTitle")
        m = QLabel(meta_txt + " · " + fmt_ts(item["ts"]))
        m.setObjectName("popMeta")
        x = QPushButton("✕")
        x.setObjectName("popClose")
        x.setFixedSize(24, 24)
        x.setCursor(Qt.PointingHandCursor)
        x.clicked.connect(self.closeRequested)
        hl.addWidget(t)
        hl.addStretch(1)
        hl.addWidget(m)
        hl.addWidget(x)
        clam.addWidget(head)

        # 内容区
        inner_w = 400 - 24
        if item["kind"] == "text":
            tb = QTextBrowser()
            tb.setObjectName("popText")
            tb.setOpenLinks(False)
            tb.setFrameShape(QFrame.NoFrame)
            body = html.escape(item.get("text") or "").replace("\n", "<br>")
            tb.setHtml(f'<div style="line-height:175%">{body}</div>')
            tb.setFixedWidth(inner_w)
            tb.document().setTextWidth(inner_w - 10)
            doc_h = int(tb.document().size().height())
            body_h = max(56, min(doc_h + 12, 430))
            tb.setFixedHeight(body_h)
            clam.addWidget(tb)
            card_h = 36 + body_h
        else:
            pm = QPixmap.fromImage(item["image"]).scaled(
                440, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            lbl = QLabel()
            lbl.setPixmap(_rounded_pixmap(pm, 10))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setContentsMargins(12, 10, 12, 12)
            clam.addWidget(lbl)
            card_h = 36 + min(400, pm.height()) + 22
        card.setFixedSize(400, card_h)
        self.setFixedSize(428, card_h + 36)        # 14+14 左右,12+24 上下


# ---------------------------------------------------------------- main window

class ClipBox(QWidget):
    def __init__(self):
        super().__init__()
        wflag = Qt.Window if os.environ.get("JUMPBOX_WINDOW") == "window" else Qt.Tool
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | wflag)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("JumpBox")
        self.setWindowIcon(make_app_icon())

        self.items = []          # [{id,kind,text,image,ts,pinned,pinned_at}]
        self._rows = {}
        self._empty_lbl = None
        self._bubble = None      # "显全"气泡(同一时间最多一个)
        self._selected_ids = []  # 点选:按点击顺序累积
        self._selected_set = set()
        shutil.rmtree(STAGING_DIR, ignore_errors=True)  # 清掉崩溃残留的粘贴临时文件
        self._last_deact_id = None
        self._last_deact_t = 0.0
        self._last_write = None  # 自己刚写回剪贴板的内容签名(防回环)
        self.auto_on = False     # 默认手动模式:在盒子里按 Ctrl+V 才存入
        if DEBUG:
            self.auto_on = True  # 调试时开启自动捕获,便于外部注入测试内容
        QApplication.instance().installEventFilter(self)

        self._build_ui()
        self._place_default()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._capture)

        cb = QApplication.clipboard()
        cb.dataChanged.connect(self._debounce.start)

        self._setup_tray()
        self.render()

    # ---- UI -------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        box = QFrame()
        box.setObjectName("box")
        root.addWidget(box)

        outer = QVBoxLayout(box)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        # 标题栏
        title = QFrame()
        tl = QHBoxLayout(title)
        tl.setContentsMargins(4, 2, 2, 2)
        tl.setSpacing(4)
        tlabel = QLabel("📦 JumpBox")
        tlabel.setObjectName("titleLbl")
        tl.addWidget(tlabel)
        tl.addStretch(1)

        self._auto_btn = QPushButton("自动捕获 ○")
        self._auto_btn.setObjectName("mini")
        self._auto_btn.setCheckable(True)
        self._auto_btn.setChecked(False)
        self._auto_btn.setToolTip("开启后,任何 Ctrl+C 复制的内容自动进入盒子")
        self._auto_btn.toggled.connect(self._toggle_auto)
        tl.addWidget(self._auto_btn)

        hide_btn = QPushButton("—")
        hide_btn.setObjectName("mini")
        hide_btn.setToolTip("收起到系统托盘")
        hide_btn.clicked.connect(self.hide)
        tl.addWidget(hide_btn)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("mini")
        close_btn.setToolTip("收起到系统托盘(托盘菜单可退出)")
        close_btn.clicked.connect(self.hide)
        tl.addWidget(close_btn)

        # 标题栏拖动
        title.setCursor(Qt.SizeAllCursor)
        title.mousePressEvent = self._title_press
        title.mouseMoveEvent = self._title_move
        outer.addWidget(title)

        # 列表
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        self._rows_layout = QVBoxLayout(container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(5)
        self._scroll.setWidget(container)
        outer.addWidget(self._scroll, 1)

        # 底栏
        foot = QFrame()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(6, 0, 2, 0)
        fl.setSpacing(2)
        self._count_lbl = QLabel("0 条")
        self._count_lbl.setObjectName("countLbl")
        fl.addWidget(self._count_lbl)
        hint = QLabel("· 点击取回")
        hint.setObjectName("hint")
        fl.addWidget(hint)
        fl.addStretch(1)

        self._all_btn = QPushButton("全选")
        self._all_btn.setObjectName("footerBtn")
        self._all_btn.setToolTip("按存入时间顺序,把所有文本合并放回剪贴板(一次 Ctrl+V 全部贴出)")
        self._all_btn.clicked.connect(self._copy_all)
        fl.addWidget(self._all_btn)

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("footerBtn")
        cancel_btn.setToolTip("清除点选高亮(剪贴板保持当前内容)")
        cancel_btn.clicked.connect(self._cancel_selection)
        fl.addWidget(cancel_btn)

        grab_btn = QPushButton("📥 存入")
        grab_btn.setObjectName("footerBtn")
        grab_btn.setToolTip("把当前剪贴板内容手动存入盒子(等同在盒子里按 Ctrl+V)")
        grab_btn.clicked.connect(lambda: self._capture(force=True))
        fl.addWidget(grab_btn)

        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("footerBtn")
        clear_btn.setToolTip("清空未固定的条目(📌 固定的会保留)")
        clear_btn.clicked.connect(self._clear)
        fl.addWidget(clear_btn)
        outer.addWidget(foot)

        self.setStyleSheet(STYLES)
        self.setFocusPolicy(Qt.ClickFocus)
        self.resize(300, 360)

    def _title_press(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPos() - self.frameGeometry().topLeft()

    def _title_move(self, e):
        if e.buttons() & Qt.LeftButton and hasattr(self, "_drag_off"):
            self.move(e.globalPos() - self._drag_off)

    # ---- 弹窗行为 -----------------------------------------------------------

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_V and e.modifiers() & Qt.ControlModifier:
            self._capture(force=True)      # 在盒子里按 Ctrl+V → 存入剪贴板内容
            return
        if e.key() == Qt.Key_Escape:
            self.hide()                    # Esc 主动收起
            return
        super().keyPressEvent(e)

    def show_and_focus(self):
        """从快捷方式/热键/托盘唤起盒子。"""
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass

    # ---- 显全气泡 -----------------------------------------------------------

    def _show_full(self, item_id: str):
        if self._bubble is not None and self._bubble.item_id == item_id:
            self._close_bubble()               # 再点一次 = 关闭
            return
        if (getattr(self, "_last_deact_id", None) == item_id
                and time.time() - self._last_deact_t < 0.45):
            # 气泡刚因本条目上的点击(盒子夺回激活)而关闭 = 用户想关,不再重开
            self._last_deact_id = None
            return
        self._close_bubble()
        item = next((it for it in self.items if it["id"] == item_id), None)
        if not item:
            return
        self._bubble = FullPopup(item)
        self._bubble.closeRequested.connect(self._close_bubble)
        row = self._rows.get(item_id)
        if row is not None:
            gp = row.mapToGlobal(QPoint(0, 0))
            screen = QApplication.primaryScreen().availableGeometry()
            x = gp.x() - self._bubble.width() - 12
            x = max(screen.left() + 8, min(x, screen.right() - self._bubble.width() - 8))
            y = max(screen.top() + 8, min(gp.y() - 6,
                                          screen.bottom() - self._bubble.height() - 8))
            self._bubble.move(x, y)
        self._bubble.show()

    def _close_bubble(self):
        if self._bubble is not None:
            if DEBUG:
                _dbg("close_bubble")
            self._bubble.hide()
            self._bubble.deleteLater()
            self._bubble = None

    def eventFilter(self, obj, e):
        try:
            # 气泡失去激活(用户点了任何别的地方)= 关闭。
            # 注意:不能在这里调 isAncestorOf 之类的方法——原生事件可能以
            # QWindow 形态进入过滤器,而过滤器里的未捕获异常会杀死整个进程。
            if self._bubble is not None and e.type() == QEvent.WindowDeactivate:
                if obj is self._bubble or obj is self._bubble.windowHandle():
                    self._last_deact_id = self._bubble.item_id
                    self._last_deact_t = time.time()
                    if DEBUG:
                        _dbg("bubble deactivated -> close")
                    self._close_bubble()
        except Exception:
            pass                               # 过滤器内绝不允许异常逃逸
        return super().eventFilter(obj, e)

    def hide(self):
        if DEBUG:
            _dbg("BOX HIDE", traceback.format_stack()[-4:-1])
        self._close_bubble()
        super().hide()

    # ---- capture ----------------------------------------------------------

    def _toggle_auto(self, on: bool):
        self.auto_on = on
        self._auto_btn.setText("自动捕获 ●" if on else "自动捕获 ○")

    def _capture(self, force: bool = False):
        """读取当前剪贴板并存入盒子。force=True 绕过自动捕获开关。"""
        if not force and not self.auto_on:
            return

        cb = QApplication.clipboard()
        mime = cb.mimeData()
        kind, text, image = None, None, None

        if mime.hasImage():
            qi = cb.image()
            if not qi.isNull():
                kind, image = "image", normalize_image(qi)
        if kind is None and mime.hasUrls():
            urls = "\n".join(u.toString() for u in mime.urls())
            if urls.strip():
                kind, text = "text", urls
        if kind is None and mime.hasText():
            t = cb.text()
            if t.strip():
                kind, text = "text", t
        if kind is None:
            return

        # 回环守卫:自己刚写回的内容,签名相同则不回收(与时间窗无关)
        sig = text_sig(text) if kind == "text" else image_sig(image)
        if self._last_write is not None and sig == self._last_write:
            self._last_write = None
            return
        self._add(kind, text=text, image=image, sig=sig)

    # ---- store ------------------------------------------------------------

    def _add(self, kind, text=None, image=None, sig=None):
        now = time.time()
        if self.items and now <= self.items[0]["ts"]:
            now = self.items[0]["ts"] + 0.001    # 保证存入时间严格递增(全选排序用)
        key = sig or (image_sig(image) if kind == "image" else text_sig(text or ""))
        new = {"id": uuid.uuid4().hex[:8], "kind": kind, "ts": now,
               "pinned": False, "pinned_at": None, "sig": key}

        hit = next((it for it in self.items
                    if not it["pinned"] and it.get("sig") == key), None)
        if hit is not None:                        # 去重命中:复用原内容,上浮
            self.items.remove(hit)
            new["id"] = hit["id"]
            for k in ("text", "image", "thumb", "preview", "w", "h"):
                if hit.get(k) is not None:
                    new[k] = hit[k]
        elif kind == "text":
            new["text"] = text
            new["preview"] = text_preview(text)
        else:
            new["image"] = image                   # 全图驻内存,取回无损
            new["thumb"] = image.scaled(64, 48, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation)  # 仅供显示
            new["w"], new["h"] = image.width(), image.height()
            new["preview"] = f"图片 {image.width()}×{image.height()}"

        self.items.insert(0, new)
        self._evict()
        self.render()

    def _drop(self, item: dict):
        try:
            self.items.remove(item)
        except ValueError:
            pass

    def _evict(self):
        while len(self.items) > MAX_ITEMS:          # 总量:淘汰最旧的未固定项
            victims = [it for it in self.items if not it["pinned"]]
            if not victims:
                break
            self._drop(min(victims, key=lambda x: x["ts"]))
        while True:                                  # 图片单独限额
            imgs = [it for it in self.items if it["kind"] == "image"]
            if len(imgs) <= MAX_IMAGES:
                break
            unpinned = [it for it in imgs if not it["pinned"]]
            if not unpinned:
                break
            self._drop(min(unpinned, key=lambda x: x["ts"]))

    def _ordered(self):
        return sorted(self.items,
                      key=lambda it: (not it["pinned"],
                                      -(it.get("pinned_at") or 0) if it["pinned"] else -it["ts"]))

    # ---- actions ------------------------------------------------------------

    def _copy_item(self, item_id: str):
        item = next((it for it in self.items if it["id"] == item_id), None)
        if not item:
            return
        cb = QApplication.clipboard()
        if item["kind"] == "image":
            self._last_write = item.get("sig") or image_sig(item["image"])
            cb.setImage(item["image"])
        else:
            self._last_write = item.get("sig") or text_sig(item.get("text") or "")
            cb.setText(item["text"])
        row = self._rows.get(item_id)
        if row:
            row.flash_copied()

    def _copy_all(self):
        """全选:按存入时间顺序把所有条目放回剪贴板(纯文本合并;含图片走文件模式)。"""
        seen, items = set(), []
        for it in sorted(self.items, key=lambda x: x["ts"]):
            if it.get("sig") in seen:
                continue
            seen.add(it.get("sig"))
            items.append(it)
        if not items:
            self._flash_count("盒子是空的")
            return
        self._apply_selection_to_clipboard(items)

    def _on_row_clicked(self, item_id: str):
        """点选累积:点过的条目带蓝框,剪贴板实时等于所选内容(按点击顺序)。"""
        row = self._rows.get(item_id)
        if item_id in self._selected_set:
            self._selected_set.discard(item_id)
            if item_id in self._selected_ids:
                self._selected_ids.remove(item_id)
            if row:
                row.set_selected(False)
        else:
            self._selected_set.add(item_id)
            self._selected_ids.append(item_id)
            if row:
                row.set_selected(True)
        self._apply_selection_to_clipboard()

    def _apply_selection_to_clipboard(self, items=None):
        """点选/全选 → 剪贴板。

        纯文本(单条或多条)= 合并文本;
        含图片 = 文件模式:每条写成一个临时文件进"文件列表",
        一次粘贴得到一组文件(资源管理器/微信/QQ/Word 均认)。
        Windows 剪贴板一次只能承载一张位图,多图只能走文件。
        """
        if items is None:
            items = [next((x for x in self.items if x["id"] == i), None)
                     for i in self._selected_ids]
            items = [it for it in items if it]
        if not items:
            return                          # 取消到空:剪贴板保持现状
        texts = [it for it in items if it["kind"] == "text" and it.get("text")]
        imgs = [it for it in items if it["kind"] == "image"]

        if len(items) == 1:                 # 单条:保持直取(图片=位图,聊天直接贴)
            it = items[0]
            if it["kind"] == "text":
                QApplication.clipboard().setText(it["text"])
            else:
                self._last_write = it.get("sig") or image_sig(it["image"])
                QApplication.clipboard().setImage(it["image"])
            return

        if not imgs:                        # 多条纯文本:按顺序合并
            joined = "\n".join(it["text"] for it in texts)
            self._last_write = text_sig(joined)
            QApplication.clipboard().setText(joined)
            self._flash_count(f"✓ 已合并 {len(texts)} 条文本")
            return

        # 含图片:文件模式
        try:
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
            os.makedirs(STAGING_DIR, exist_ok=True)
            paths = []
            for idx, it in enumerate(items, 1):
                if it["kind"] == "text":
                    fp = os.path.join(STAGING_DIR, f"{idx:02d}_文本_"
                                      f"{_sanitize_name(text_preview(it['text']))}.txt")
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(it["text"])
                else:
                    fp = os.path.join(STAGING_DIR, f"{idx:02d}_图片_"
                                      f"{_sanitize_name(it.get('preview') or '')}.png")
                    if not it["image"].save(fp, "PNG"):
                        raise OSError(fp)
                paths.append(fp)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
            self._last_write = text_sig(
                "\n".join(QUrl.fromLocalFile(p).toString() for p in paths))
            QApplication.clipboard().setMimeData(mime)
            self._flash_count(f"✓ 已打包 {len(paths)} 个文件"
                              f"(图片 {len(imgs)} 张),粘贴即得一组文件")
        except OSError:
            self._flash_count("文件模式失败:请改为逐条点击取回")

    def _cancel_selection(self):
        """取消:清掉点选高亮(剪贴板保持当前内容)。"""
        if not self._selected_ids:
            self._flash_count("当前没有点选的内容")
            return
        for i in self._selected_ids:
            row = self._rows.get(i)
            if row:
                row.set_selected(False)
        self._selected_ids = []
        self._selected_set = set()

    def _staging_clear(self):
        """清掉文件粘贴模式的临时目录(进程退出时调用;剪贴板随之失效属正常)。"""
        shutil.rmtree(STAGING_DIR, ignore_errors=True)

    def _flash_count(self, msg: str):
        self._count_lbl.setText(msg)
        QTimer.singleShot(1500,
                          lambda: self._count_lbl.setText(f"{len(self.items)} 条"))

    def _toggle_pin(self, item_id: str):
        item = next((it for it in self.items if it["id"] == item_id), None)
        if not item:
            return
        item["pinned"] = not item["pinned"]
        item["pinned_at"] = time.time() if item["pinned"] else None
        row = self._rows.get(item_id)
        if row:  # 原地更新固定样式,不重建行
            row.apply_pin_state(item["pinned"])
            row.setProperty("fixed", "true" if item["pinned"] else "false")
            row.style().unpolish(row)
            row.style().polish(row)
        self.render()

    def _remove(self, item_id: str):
        item = next((it for it in self.items if it["id"] == item_id), None)
        if item:
            self._drop(item)
            self.render()

    def _clear(self):
        self.items = [it for it in self.items if it["pinned"]]
        self.render()

    # ---- render -------------------------------------------------------------

    def render(self):
        """增量刷新:只增删变化的行,未变化的行原样复用(缩略图/滚动位置不重建)。"""
        if self._bubble is not None and \
                self._bubble.item_id not in {it["id"] for it in self.items}:
            self._close_bubble()               # 条目已消失,气泡不留
        if self._selected_ids:                 # 点选清理:被删/淘汰的移出选择
            alive = {it["id"] for it in self.items}
            if any(i not in alive for i in self._selected_ids):
                self._selected_ids = [i for i in self._selected_ids if i in alive]
                self._selected_set = set(self._selected_ids)
        ordered = self._ordered()
        live = {it["id"]: it for it in ordered}

        # 1) 移除失效行(条目已删,或内容签名变了)
        for rid, row in list(self._rows.items()):
            it = live.get(rid)
            if it is None or row.token != (it.get("sig") or ("id", rid)):
                row.hide()
                row.deleteLater()
                del self._rows[rid]

        # 2) 为新条目建行
        for it in ordered:
            if it["id"] in self._rows:
                continue
            row = ClipRow(it)
            row.clicked.connect(self._on_row_clicked)
            row.pinToggled.connect(self._toggle_pin)
            row.removeRequested.connect(self._remove)
            row.showFull.connect(self._show_full)
            if it["id"] in self._selected_set:
                row.set_selected(True)
            self._rows[it["id"]] = row

        # 3) 按目标顺序重排既有控件
        while self._rows_layout.count():
            self._rows_layout.takeAt(0)
        for it in ordered:
            self._rows_layout.addWidget(self._rows[it["id"]])

        # 4) 空状态与计数
        if not self.items:
            if self._empty_lbl is None:
                self._empty_lbl = QLabel("盒子是空的\n\n复制内容后,在这里按 Ctrl+V 存入\n点击条目即可取回 👇")
                self._empty_lbl.setObjectName("empty")
                self._empty_lbl.setAlignment(Qt.AlignCenter)
            self._empty_lbl.show()
            self._rows_layout.addWidget(self._empty_lbl)
        elif self._empty_lbl is not None:
            self._empty_lbl.hide()

        self._rows_layout.addStretch(1)
        self._count_lbl.setText(f"{len(self.items)} 条")

    # ---- 纯内存设计:无存档读写,进程结束即清空 ---------------------------------

    def _place_default(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - 300 - 18, screen.top() + (screen.height() - 360) // 2)

    # ---- tray / lifecycle ------------------------------------------------------

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip("JumpBox — 复制的内容会暂存在这里")
        menu = QMenu()
        act_show = QAction("显示 / 隐藏盒子", self)
        act_show.triggered.connect(self.toggle_visible)
        act_clear = QAction("清空未固定条目", self)
        act_clear.triggered.connect(self._clear)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_clear)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.toggle_visible()
            if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, e):
        e.ignore()          # 关闭 = 收到托盘,不退出
        self.hide()

    def _quit(self):
        self.tray.hide()
        QApplication.quit()


# ---------------------------------------------------------------- entry

class _WinHotkeyFilter(QAbstractNativeEventFilter):
    """全局热键:收到 WM_HOTKEY 时回调(仅 Windows 生效)。"""

    def __init__(self, callback):
        super().__init__()
        self._cb = callback

    def nativeEventFilter(self, eventType, message):
        if bytes(eventType) == b"windows_generic_MSG":
            try:
                import ctypes.wintypes as wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self._cb()
            except Exception:
                pass
        return False, 0


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("JumpBox")
    app.setWindowIcon(make_app_icon())
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 9))

    # 单实例:已有盒子在跑 → 唤起它,本进程立即退出
    probe = QLocalSocket()
    probe.connectToServer(INSTANCE_KEY)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return

    box = ClipBox()
    app.aboutToQuit.connect(box._staging_clear)   # 退出时清掉粘贴临时文件
    box.show_and_focus()

    # 第二实例/快捷方式再次点击 → 唤起已有盒子
    QLocalServer.removeServer(INSTANCE_KEY)
    server = QLocalServer()
    server.listen(INSTANCE_KEY)
    server.newConnection.connect(box.show_and_focus)

    # 全局热键 Ctrl+Alt+B 随时唤出/收起
    hotkey_filter = None
    if sys.platform == "win32":
        try:
            import ctypes
            MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4
            if ctypes.windll.user32.RegisterHotKey(
                    None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("B")):
                hotkey_filter = _WinHotkeyFilter(box.toggle_visible)
                app.installNativeEventFilter(hotkey_filter)
        except Exception:
            pass

    rc = app.exec_()
    if hotkey_filter is not None and sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
