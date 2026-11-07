# -*- coding: utf-8 -*-
"""剪贴盒性能基准(纯内存版,无 IO)。

用法:
    python bench.py --tag xx            # 记录快照
    python bench.py --tag xx --compare baseline
"""
import argparse
import ctypes
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PyQt5.QtGui import QColor, QImage, QPainter, QPen
from PyQt5.QtWidgets import QApplication

N_IMG, N_TXT = 9, 19
BIG_W, BIG_H = 2560, 1440


def noise_image(w, h, salt):
    """确定性伪噪声大图(接近真实截图的处理成本)。"""
    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    p.fillRect(0, 0, w, h, QColor(30 + salt % 100, 90, 160))
    rnd = random.Random(salt)
    for _ in range(1500):
        x, y = rnd.randrange(w), rnd.randrange(h)
        p.setPen(QPen(QColor(rnd.randrange(256), rnd.randrange(256),
                             rnd.randrange(256), 200), 3))
        p.drawLine(x, y, (x + rnd.randrange(200)) % w, (y + rnd.randrange(200)) % h)
    p.end()
    return img


def build_box(m):
    """28 条目(9 大图 + 19 文本),批量构建期间不刷新界面。"""
    box = m.ClipBox()
    real_render = box.render
    box.render = lambda: None
    for i in range(N_IMG):
        box._add("image", image=m.normalize_image(noise_image(BIG_W, BIG_H, salt=i)))
    for i in range(N_TXT):
        box._add("text", text=f"性能基准文本条目 {i:02d} " + "内容" * 30)
    box.render = real_render
    box.render()
    return box


def timed(fn, reps=3):
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return round(best, 1)


def rss_mb():
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo(ctypes.c_void_p(k32.GetCurrentProcess()),
                               ctypes.byref(pmc), pmc.cb)
    return round(pmc.WorkingSetSize / 1048576, 1)


def bench_startup():
    """子进程冷启动:构造 ClipBox(纯内存,无存档可载)。"""
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, r'{HERE}')\n"
        "from PyQt5.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "import clipboard_box as m\n"
        "t0 = time.perf_counter()\n"
        "m.ClipBox()\n"
        "print(round((time.perf_counter() - t0) * 1000, 1))\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code],
                       capture_output=True, text=True, timeout=300)
    try:
        return float(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print("startup bench stderr:", r.stderr[-400:])
        return -1.0


def run_suite(app, m):
    res = {}
    box = build_box(m)

    # 1) 诊断:逐图计算内容签名的单价(捕获时对"新内容"要做一次)
    img = m.normalize_image(noise_image(BIG_W, BIG_H, salt=999))
    res["sig_scan_ms"] = timed(
        lambda: ([m.image_sig(it["image"]) for it in box.items
                  if it["kind"] == "image"], m.image_sig(img)), reps=3)

    # 2) 纯捕获路径(不含界面刷新):去重比对 + 入列 + 生成显示缩略图
    add_img = m.normalize_image(noise_image(BIG_W, BIG_H, salt=997))
    add_sig = m.image_sig(add_img)
    real_render = box.render
    box.render = lambda: None
    res["capture_core_ms"] = timed(
        lambda: box._add("image", image=add_img, sig=add_sig), reps=3)
    box.render = real_render

    # 3) 端到端追加一条大图
    img2 = m.normalize_image(noise_image(BIG_W, BIG_H, salt=996))
    res["add_image_ms"] = timed(lambda: box._add("image", image=img2), reps=2)

    # 4) 端到端追加一条短文本
    res["add_text_ms"] = timed(
        lambda: box._add("text", text=f"追加文本 {time.perf_counter()}"), reps=2)

    # 5) 全量刷新 28 条列表的渲染耗时(热)
    res["render_ms"] = timed(box.render, reps=3)

    # 6) 常驻内存(9+ 张 2560x1440 全图驻留内存的工作集)
    res["rss_mb"] = rss_mb()

    # 7) 冷启动构造耗时(无存档可载)
    res["startup_ms"] = bench_startup()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--compare", default="baseline")
    args = ap.parse_args()

    app = QApplication([])
    import clipboard_box as m
    res = run_suite(app, m)

    out = os.path.join(HERE, f"bench_{args.tag}.json")
    json.dump({"tag": args.tag, "ts": datetime.now().strftime("%H:%M:%S"), **res},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n===== [{args.tag}] =====")
    for k, v in res.items():
        print(f"  {k:<14} {v}")

    cmp_file = os.path.join(HERE, f"bench_{args.compare}.json")
    if args.compare != args.tag and os.path.exists(cmp_file):
        base = json.load(open(cmp_file, encoding="utf-8"))
        print(f"\n----- 对比 [{args.compare}] -> [{args.tag}] -----")
        for k in res:
            b, n = base.get(k), res[k]
            if not b or n < 0 or b < 0:
                continue
            unit = "MB" if k.endswith("_mb") else "ms"
            delta = (n - b) / b * 100
            print(f"  {k:<14} {b:>9.1f} -> {n:>9.1f} {unit}  ({delta:+.0f}%)")


if __name__ == "__main__":
    main()
