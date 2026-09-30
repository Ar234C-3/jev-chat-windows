# -*- coding: utf-8 -*-
"""QQ 接入探针 v1：枚举 QQ 窗口 → 各抓一帧存盘 → 中央带主色表 → 通用锚点自检。

回答 ChatApp 注册需要的四件事（只读你自己开着的 QQ 窗口；按仓库惯例 probe/ 下允许存图）：
  1. QQ 的进程名与窗口标题分布 —— 定 ChatApp 的 exes / main_title / skip_titles
  2. 每个候选窗口一张 PNG —— 肉眼确认布局：单主窗内嵌聊天，还是独立聊天窗口
  3. 中央消息带的主色表 —— 写 is_me（我方气泡）谓词的实测依据
  4. chat_area() 通用像素锚点能不能认出消息区 —— 决定 capture.py 要不要为 QQ 动刀

用法（项目根在 PYTHONPATH）：
    开着 QQ，最好一个单聊 + 一个群聊窗口，然后：
    python probe/probe_qq.py
"""
from __future__ import annotations

import ctypes
import io
import os
import sys
import time
from ctypes import wintypes

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

EXES = ("qq.exe", "tim.exe")  # QQ NT 与 TIM 都认


def list_qq_windows() -> list[tuple[int, str, int, str]]:
    """(hwnd, 标题, 面积, exe)，只收可见顶层窗口。"""
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    out = []

    def exe_of(pid: int) -> str:
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_uint(1024)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return os.path.basename(buf.value).lower() if ok else ""

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = exe_of(pid.value)
        if exe not in EXES:
            return True
        title = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, title, 256)
        rect = wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        out.append((int(hwnd), title.value, area, exe))
        return True

    u32.EnumWindows(cb, 0)
    return out


def grab_frame(hwnd: int, timeout: float = 4.0):
    """WGC 抓一帧非黑图就停。跟 app.capture.Capture 的区别：不依赖 chat_area 锚点成功，
    锚点失败本身也是探针要报告的信息，所以这里拿原始帧。"""
    from windows_capture import WindowsCapture

    got: dict = {"frame": None}

    # windows_capture 校验回调函数名：必须叫 on_frame_arrived / on_closed，且两个都要注册
    def on_frame_arrived(frame, _control):
        if got["frame"] is not None:
            return
        buf = np.ascontiguousarray(frame.frame_buffer[:, :, :3][:, :, ::-1])  # BGRA → RGB
        if buf.max() > 0:
            got["frame"] = buf

    def on_closed(_control=None):
        pass

    cap = WindowsCapture(cursor_capture=None, draw_border=None, window_hwnd=hwnd)
    cap.event(on_frame_arrived)
    cap.event(on_closed)
    ctl = cap.start_free_threaded()
    t0 = time.perf_counter()
    try:
        while got["frame"] is None and time.perf_counter() - t0 < timeout:
            time.sleep(0.05)
    finally:
        try:
            ctl.stop()
        except Exception:
            pass
    return got["frame"]


def band_colors(img: np.ndarray, n: int = 12) -> list[tuple[tuple[int, int, int], int, float]]:
    """中央 25%~75% 区域的主色表（步进采样，够快）。写 is_me 谓词就看这张表。"""
    H, W = img.shape[:2]
    band = img[H // 4: H * 3 // 4: 3, W // 4: W * 3 // 4: 3].reshape(-1, 3)
    vals, cnt = np.unique(band, axis=0, return_counts=True)
    order = np.argsort(cnt)[::-1][:n]
    total = int(cnt.sum())
    return [((int(vals[i][0]), int(vals[i][1]), int(vals[i][2])), int(cnt[i]),
             cnt[i] / total) for i in order]


def safe_name(title: str, idx: int) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in title)
    return f"qq_{idx}_{keep[:24] or 'untitled'}.png"


def main() -> int:
    wins = list_qq_windows()
    if not wins:
        print(f"没找到可见的 {EXES} 窗口。请先把 QQ 打开、把要适配的聊天窗口摆在桌面上（别最小化），再跑一次。")
        return 1
    print(f"找到 {len(wins)} 个 QQ/TIM 窗口：")
    for hwnd, title, area, exe in wins:
        w = int(area ** 0.5)
        print(f"  hwnd={hwnd:<8} {exe:<9} {area // 1000:>8}k px²  标题={title!r}")

    try:
        from app.capture import chat_area
    except Exception as exc:
        chat_area = None
        print(f"(app.capture.chat_area 不可用：{exc})")

    from PIL import Image

    out_dir = os.path.dirname(os.path.abspath(__file__))
    picked = 0
    for hwnd, title, area, exe in wins:
        if area < 200_000:  # 太小的多半是浮窗/通知，跳过
            continue
        frame = grab_frame(hwnd)
        if frame is None:
            print(f"\n[{title!r}] 4s 内没抓到非黑帧（最小化？被别的捕获占着？）")
            continue
        picked += 1
        path = os.path.join(out_dir, safe_name(title, picked))
        Image.fromarray(frame).save(path)
        print(f"\n[{title!r}] 已存 {os.path.basename(path)}  {frame.shape[1]}x{frame.shape[0]}")
        if chat_area is not None:
            area_info = chat_area(frame)
            print(f"  chat_area 通用锚点: {area_info if area_info else '认不出消息区 ✗'}")
        print("  中央带主色（写 is_me 的依据，前 8 个）：")
        for (r, g, b), cnt, pct in band_colors(frame)[:8]:
            print(f"    #{r:02x}{g:02x}{b:02x}  {r:>3},{g:>3},{b:>3}  {pct:6.2%}  ({cnt})")

    print("\n下一步：把上面的窗口清单和主色表发我（或直接让我读 probe/qq_*.png），")
    print("我据此定 exes/main_title/skip_titles 和 is_me 谓词，再进 chatapps.py 注册。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
