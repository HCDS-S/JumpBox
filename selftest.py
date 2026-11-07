# -*- coding: utf-8 -*-
"""剪贴盒功能自测:python selftest.py — 全过打印 PASS。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtGui import QImage, QColor, QPainter
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel, QWidget

import clipboard_box as m

FAILS = []


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        FAILS.append(name)


def make_img(w, h):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(QColor(180, 60, 40))
    p = QPainter(img)
    p.setPen(QColor(20, 30, 200))
    p.drawEllipse(10, 10, w - 20, h - 20)
    p.end()
    return img


def paste(box, app, text=None, image=None):
    """模拟:设置剪贴板 + 在盒子里按 Ctrl+V(旧守卫需要 120ms 冷却)。"""
    if not getattr(m, "SIG_GUARD", False):
        QTest.qWait(250)
    if text is not None:
        app.clipboard().setText(text)
    else:
        app.clipboard().setImage(image)
    QTest.keyClick(box, Qt.Key_V, Qt.ControlModifier)


def main():
    app = QApplication([])
    cb = app.clipboard()
    saved_clip = cb.text()          # 测试结束恢复用户剪贴板
    box = m.ClipBox()

    check("启动为空(无历史记录)", len(box.items) == 0)

    # 1) 自动捕获默认关
    cb.setText("不应被自动捕获")
    box._debounce.start()
    QTest.qWait(300)
    check("自动捕获默认关", len(box.items) == 0)

    # 2) Ctrl+V 存入
    paste(box, app, text="手动粘贴的内容A")
    check("Ctrl+V 存入", len(box.items) == 1)

    # 3) 重复粘贴去重
    paste(box, app, text="手动粘贴的内容A")
    paste(box, app, text="内容B")
    texts = [it.get("text") or it.get("preview") for it in box.items]
    check("去重且最新在上", len(box.items) == 2 and (texts[0] or "").startswith("内容B"))

    # 4) 点击取回文本
    box._copy_item(box.items[1]["id"])
    check("点击取回文本", cb.text() == "手动粘贴的内容A")

    # 5) 图片:全图驻内存(取回无损),缩略图仅供显示
    src = make_img(800, 600)
    paste(box, app, image=src)
    it = box.items[0]
    check("图片全图驻内存", it["image"].width() == 800 and it.get("thumb")
          and it["thumb"].width() <= 66)
    box._copy_item(it["id"])
    back = cb.image()
    check("点击取回原图", (not back.isNull()) and back.width() == 800 and back.height() == 600)

    # 6) 省略号
    paste(box, app, text="超" * 300)
    box.resize(300, 360)
    box.show()
    QTest.qWait(50)
    shown = box._rows[box.items[0]["id"]]._preview_lbl.text()
    check("长文本省略号", shown.endswith("…") and len(shown) < 120)
    box.hide()

    # 7) 固定与清空
    box._toggle_pin(box.items[0]["id"])
    keep_id = box.items[0]["id"]
    box._clear()
    check("清空保留固定项", len(box.items) == 1 and box.items[0]["id"] == keep_id)

    # 8) 淘汰上限
    for i in range(40):
        box._add("text", text=f"淘汰测试 {i}")
    check("上限 30 条", len(box.items) <= 30)
    check("固定项未被淘汰", any(it["id"] == keep_id for it in box.items))
    check("满盒后新增仍入盒(淘汰最旧)", any(it.get("text") == "淘汰测试 39" for it in box.items))

    # 9) Esc 收起
    box.show()
    box._was_active = True
    QTest.keyClick(box, Qt.Key_Escape)
    check("Esc 收起", not box.isVisible())

    # 10) 置顶常驻:激活别的窗口后盒子仍在(不因失焦消失)
    box.show()
    other = QWidget()
    other.show()
    QTest.qWait(100)
    app.sendEvent(box, QEvent(QEvent.ActivationChange))
    check("点击别处不消失(置顶常驻)", box.isVisible())
    other.hide()
    box.hide()

    # 11) 纯内存:再建一个盒子也是空的(无任何存档可读)
    box2 = m.ClipBox()
    check("无存档可读", len(box2.items) == 0)

    # 12) 预览无 "[N行]" 前缀,只取开头文字
    paste(box, app, text="第一行内容\n第二行内容\n第三行内容")
    it = box.items[0]
    check("预览无行数前缀", it["preview"] == "第一行内容"
          and not it["preview"].startswith("["))

    # 13) 显全气泡:打开含全文,再点一次关闭;QWindow 事件不炸进程
    from PyQt5.QtGui import QWindow
    from PyQt5.QtWidgets import QTextBrowser
    box._show_full(it["id"])
    ok_open = (box._bubble is not None and box._bubble.item_id == it["id"])
    full_tb = box._bubble.findChild(QTextBrowser)
    check("显全气泡打开含全文", ok_open and full_tb is not None
          and "第二行内容" in full_tb.toPlainText())
    # 模拟原生窗口层事件(obj=QWindow,真实环境必现)——过滤器不得抛异常
    box.show()
    box.eventFilter(QWindow(), QEvent(QEvent.MouseButtonPress))
    box.eventFilter(QWindow(), QEvent(QEvent.WindowDeactivate))
    check("QWindow 事件不崩溃", box.isVisible())
    box._show_full(it["id"])
    check("再点显全关闭气泡", box._bubble is None)

    # 14) 全选:按存入时间顺序合并
    box3 = m.ClipBox()
    box3._add("text", text="最早存入AAA")
    box3._add("text", text="中间存入BBB")
    box3._add("text", text="最新存入CCC")
    box3._copy_all()
    check("全选按存入时间合并", cb.text() == "最早存入AAA\n中间存入BBB\n最新存入CCC")
    # 固定项与未固定重复内容不重复合并
    dup = box3.items[-1]                      # 最旧的 AAA
    box3._toggle_pin(dup["id"])
    box3._add("text", text="最早存入AAA")
    box3._copy_all()
    check("全选去重(固定+未固定同名只算一次)", cb.text().count("最早存入AAA") == 1)

    # 15) 点选累积:点几条,剪贴板实时就是那几条(点击顺序);取消褪去高亮
    box4 = m.ClipBox()
    box4._add("text", text="最早存入AAA")
    box4._add("text", text="中间存入BBB")
    box4._add("text", text="最新存入CCC")
    QTest.mouseClick(box4._rows[box4.items[0]["id"]], Qt.LeftButton)   # 点 CCC
    check("点一条=剪贴板即这条", cb.text() == "最新存入CCC")
    QTest.mouseClick(box4._rows[box4.items[2]["id"]], Qt.LeftButton)   # 点 AAA
    check("点两条=剪贴板两条(点击顺序)", cb.text() == "最新存入CCC\n最早存入AAA")
    QTest.mouseClick(box4._rows[box4.items[0]["id"]], Qt.LeftButton)   # 再点 CCC = 取消选中
    check("再点同条=移出选择", cb.text() == "最早存入AAA"
          and len(box4._selected_ids) == 1)
    box4._cancel_selection()
    check("取消后无点选", len(box4._selected_ids) == 0
          and not any(r.property("selected") == "true" for r in box4._rows.values()))
    QTest.mouseClick(box4._rows[box4.items[1]["id"]], Qt.LeftButton)   # 点 BBB
    check("取消后重新单点", cb.text() == "中间存入BBB")

    cb.setText(saved_clip)
    print()
    if FAILS:
        print(f"共 {len(FAILS)} 项失败: {FAILS}")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
