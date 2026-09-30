# -*- coding: utf-8 -*-
"""QQ 帧解剖 v3：Read 工具看不了图，就用像素把布局讲出来。

v3 新增：竖直分界检测——用户确认 qq_5 是「左侧竖排标签页 + 右侧聊天面板」（像浏览器侧边标签），
标签卡会被误当成消息，所以先找标签列/面板之间的竖直分界线，把消息区切到右侧面板再分析。

每块输出：绝对 y 范围、x 范围、左右（相对面板）、泡内白字/黑字（气泡有系统性文字，贴图没有）、主填充色。
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.capture import chat_area  # noqa: E402


def runs_of_row(row: np.ndarray, min_run: int = 20):
    """一行里长度≥min_run 的同色连续段 → [(起, 止, rgb)]。"""
    out = []
    n = len(row)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and (row[j + 1] == row[i]).all():
            j += 1
        if j - i + 1 >= min_run:
            out.append((i, j, tuple(int(v) for v in row[i])))
        i = j + 1
    return out


def vertical_split(img: np.ndarray, bg: tuple[int, int, int]) -> int | None:
    """标签列/面板的竖直分界：左半区「整列同色、非底色、中性灰」的细线，取最靠右的一根。
    没画分界线的窗口返回 None（维持原分析）。"""
    H, W = img.shape[:2]
    bg_arr = np.array(bg)
    best = None
    for x in range(int(W * 0.03), int(W * 0.55)):
        col = img[int(H * 0.08):int(H * 0.92), x]
        vals, cnt = np.unique(col, axis=0, return_counts=True)
        mode, mc = vals[cnt.argmax()], int(cnt.max())
        frac = mc / len(col)
        if frac > 0.75 and not np.array_equal(mode, bg_arr):
            if abs(int(mode[0]) - int(mode[1])) < 40 and abs(int(mode[1]) - int(mode[2])) < 40:
                if best is None or x > best:
                    best = x
    return best


def analyze(path: str) -> None:
    img = np.asarray(Image.open(path).convert("RGB"))
    info = chat_area(img)
    print(f"\n===== {os.path.basename(path)}  {img.shape[1]}x{img.shape[0]} =====")
    if not info:
        print("  chat_area 认不出消息区，跳过")
        return
    x0, yt, x1, yi, bg, _y0 = info
    bg = tuple(int(v) for v in bg)
    split = vertical_split(img, bg)
    note = ""
    if split is not None and x0 <= split < (x0 + x1) // 2:
        x0 = split + 3  # 切掉标签列
        note = f"  ← 竖直分界 x={split}，已切左侧标签列"
    print(f"  消息区 x[{x0}:{x1}] y[{yt}:{yi}]  底色=#{bg[0]:02x}{bg[1]:02x}{bg[2]:02x}{note}")

    chat = img[yt:yi, x0:x1]
    H, W = chat.shape[:2]
    row_runs = []
    for y in range(H):
        runs = [r for r in runs_of_row(chat[y], min_run=20) if r[2] != bg]
        row_runs.append(runs)

    blocks, cur, gap = [], None, 0
    for y, runs in enumerate(row_runs):
        if runs:
            if cur is None:
                cur = {"y0": y, "y1": y, "runs": list(runs)}
            else:
                cur["y1"] = y
                cur["runs"].extend(runs)
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > 12:
                blocks.append(cur)
                cur = None
    if cur:
        blocks.append(cur)

    print(f"  消息块 {len(blocks)} 个：")
    side_colors: dict = {"right": {}, "left": {}, "mid": {}}
    for b in blocks:
        if b["y1"] - b["y0"] < 6:
            continue
        colors: dict = {}
        for s, e, rgb in b["runs"]:
            colors[rgb] = colors.get(rgb, 0) + (e - s + 1)
        if not colors:
            continue
        wsum = sum(e - s + 1 for s, e, _ in b["runs"])
        wx = sum((s + e) / 2 * (e - s + 1) for s, e, _ in b["runs"])
        cx = wx / wsum / W if wsum else 0.5
        side = "right" if cx > 0.55 else ("left" if cx < 0.45 else "mid")
        top = sorted(colors.items(), key=lambda kv: -kv[1])
        fill = top[0][0]
        for rgb, cnt in top[:3]:
            side_colors[side][rgb] = side_colors[side].get(rgb, 0) + cnt
        # 泡内文字判据：主填充 bbox 里数白字/黑字——气泡有系统性文字，贴图没有
        sub = chat[b["y0"]:b["y1"] + 1]
        m = np.all(sub == np.array(fill), axis=-1)
        ys, xs = np.nonzero(m)
        white = black = 0
        if len(xs) > 200:
            reg = sub[max(0, ys.min() - 4):ys.max() + 4,
                      max(0, xs.min() - 4):xs.max() + 4].astype(int)
            lum = reg.mean(axis=-1)
            near = np.abs(reg - np.array(fill)).sum(-1) < 60
            white = int((~near & (lum > 235)).sum())
            black = int((~near & (lum < 70)).sum())
        x_lo, x_hi = (x0 + int(xs.min()), x0 + int(xs.max())) if len(xs) else (x0, x0)
        desc = "  ".join(f"#{r:02x}{g:02x}{b_:02x}({c}px)" for (r, g, b_), c in top[:3])
        print(f"    y{yt + b['y0']:>4}~{yt + b['y1']:<4} x[{x_lo}~{x_hi}] {side:>5}  "
              f"泡内 白字{white:>5}/黑字{black:>5}  {desc}")

    for side in ("right", "left", "mid"):
        cols = side_colors[side]
        if not cols:
            continue
        top = sorted(cols.items(), key=lambda kv: -kv[1])[:4]
        desc = "  ".join(f"#{r:02x}{g:02x}{b_:02x}({c}px)" for (r, g, b_), c in top)
        print(f"  >> {side:>5} 汇总: {desc}")
    right = max(side_colors["right"].items(), key=lambda kv: kv[1], default=None)
    left = max(side_colors["left"].items(), key=lambda kv: kv[1], default=None)
    if right and left:
        print(f"  结论候选: 面板右侧主色=#{right[0][0]:02x}{right[0][1]:02x}{right[0][2]:02x}"
              f"  面板左侧主色=#{left[0][0]:02x}{left[0][1]:02x}{left[0][2]:02x}"
              f"  底色=#{bg[0]:02x}{bg[1]:02x}{bg[2]:02x}")


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(p for p in glob.glob(os.path.join(here, "qq_*.png"))
                   if "_crop_" not in os.path.basename(p))
    if not files:
        print("没有 probe/qq_*.png，先跑 probe_qq.py")
        return 1
    for p in files:
        analyze(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
