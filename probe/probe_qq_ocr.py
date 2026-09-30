# -*- coding: utf-8 -*-
"""QQ 全链路集成探针：帧 → chat_area 锚点 → read_title 会话名 → Reader.read 分类合并 → new_lines 去重。

验证微信那套识别骨架（who_said 颜色分类 / 发言人名 / 同泡多行合并 / 滚动去重）在 QQ 上的端到端表现，
不经过 UI、不联网（OCR 离线）。用法：开着 QQ（最好一个单聊 + 一个群聊），项目根在 PYTHONPATH：

    python probe/probe_qq_ocr.py
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)               # probe_qq 同目录
sys.path.insert(0, os.path.dirname(HERE))  # 项目根（app 包）

from probe_qq import grab_frame, list_qq_windows  # noqa: E402  （stdout 重定向由它做，别再包一层）

from app.chatapps import QQ  # noqa: E402
from app.ocr import Reader, read_title  # noqa: E402
from app.capture import chat_area, window_title  # noqa: E402


def main() -> int:
    wins = [w for w in list_qq_windows() if w[3] == "qq.exe" and w[1] != "QQ" and w[2] > 200_000]
    if not wins:
        print("没找到 QQ 聊天窗口（开着吗？别最小化）")
        return 1
    wins.sort(key=lambda w: -w[2])
    for hwnd, title, area, _exe in wins[:2]:  # 最多测两个窗口：单聊 + 群聊
        print(f"\n===== 窗口 {title!r} =====")
        frame = grab_frame(hwnd)
        if frame is None:
            print("  4s 内没抓到非黑帧，跳过")
            continue
        info = chat_area(frame)
        if not info:
            print("  chat_area 认不出消息区，跳过")
            continue
        x0, yt, x1, yi, bg, y0 = info
        print(f"  锚点 x[{x0}:{x1}] y[{yt}:{yi}] 底色=#{int(bg[0]):02x}{int(bg[1]):02x}{int(bg[2]):02x}")

        name = read_title(frame[y0:yt, x0:x1], QQ)
        wt = re.sub(r"等\d+个会话$", "", window_title(hwnd)).strip()
        print(f"  会话名方案：OCR头部={name!r}  窗口标题(实际采用)={wt!r}")

        reader = Reader(QQ)
        t0 = time.perf_counter()
        lines = reader.read(frame[yt:yi, x0:x1], bg)
        ms = int((time.perf_counter() - t0) * 1000)
        print(f"  OCR+分类 {ms}ms，识别 {len(lines)} 条（同泡多行已合并）：")
        for who, nm, text, y in lines:
            who_mark = "我" if who == "me" else "对方"
            print(f"    y{y:>4} [{who_mark}] {(nm + ' · ') if nm else ''}{text[:42]}")
        new = reader.new_lines(lines)
        print(f"  new_lines 去重后新消息: {len(new)} 条")
        kinds = Counter(b[4] for b in reader.last_boxes)
        print(f"  框分类统计: {dict(kinds)}")
        print("  ↑ 请对照窗口核对：me/her 分得对不对、发言人名有没有挂错、图片字有没有混进来")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
