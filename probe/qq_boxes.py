# -*- coding: utf-8 -*-
"""QQ 帧 raw 框审计：逐框打印 who_said 的判决依据与 Reader.read 的最终去向。

回答「OCR 到底看见了什么、每一框死在哪一步」：
    底色模式占比（<45% 判图片） / 对比度（≥150 才算 her） / is_me / on_pane（印在面板底色上）
    → 最终：me / her / name / gray丢 / image丢 / tiny丢 / 收录
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app.chatapps import QQ  # noqa: E402
from app.capture import chat_area  # noqa: E402
from app.ocr import _engine, who_said  # noqa: E402


def audit(path: str) -> None:
    img = np.asarray(Image.open(path).convert("RGB"))
    info = chat_area(img)
    if not info:
        print(f"{os.path.basename(path)}: 锚点失败")
        return
    x0, yt, x1, yi, bg, y0 = info
    pane = img[yt:yi, x0:x1]
    pane_bg = bg
    print(f"\n===== {os.path.basename(path)[:28]}  面板 {pane.shape[1]}x{pane.shape[0]} =====")
    engine = _engine(QQ)
    res, _ = engine(pane, use_cls=False)
    print(f"raw det 框数: {len(res or [])}")
    raws = []
    for box, text, score in sorted(res or [], key=lambda r: r[0][0][1]):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        rect = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
        reg = pane[int(min(ys)):int(max(ys)), int(min(xs)):int(max(ys))].astype(int)
        if reg.size == 0:
            continue
        vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
        mode = vals[cnt.argmax()]
        share = float(cnt.max()) / (reg.shape[0] * reg.shape[1])
        kind, mbg, h = who_said(pane, box, QQ)
        on_pane = mbg is not None and np.abs(np.asarray(mbg) - pane_bg).sum() <= 6
        # 复刻 Reader.read 的分支，给出最终去向
        if kind is None:
            disp = "丢·图片(底色不平)"
        elif kind == "gray":
            disp = "丢·gray(对比不足)"
        elif on_pane and kind in ("her", "me"):
            taken = bool(on_pane and box[0][0] < 0.25 * pane.shape[1] and len(text) <= 16
                         and not any(c in text for c in ":："))
            disp = "name(发言人)" if taken else "丢·印在面板底色上"
        else:
            disp = f"收录·{kind}"
        mode_hex = f"#{int(mode[0]):02x}{int(mode[1]):02x}{int(mode[2]):02x}"
        print(f"  y{rect[1]:>4} x{rect[0]:>4} share={share:.0%} 底色={mode_hex} "
              f"kind={kind} 墨高={h:>2} conf={score:.2f} → {disp}  {text[:30]!r}")


def main() -> int:
    for name in sorted(f for f in os.listdir(HERE)
                       if f.startswith("qq_") and "_crop_" not in f and f.endswith(".png")):
        audit(os.path.join(HERE, name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
