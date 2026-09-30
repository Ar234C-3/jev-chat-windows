# -*- coding: utf-8 -*-
"""裁剪 QQ 帧的指定区域并 OCR——给「这块是什么」的指认用（白字深底会自动反色重试）。

    python probe/qq_crop_ocr.py <png> <x> <y> <w> <h>
"""
from __future__ import annotations

import sys

import numpy as np
from PIL import Image, ImageOps


def main() -> int:
    if len(sys.argv) != 6:
        print(__doc__)
        return 2
    path, x, y, w, h = sys.argv[1], *(int(v) for v in sys.argv[2:6])
    img = Image.open(path).convert("RGB")
    pad = 16
    box = (max(0, x - pad), max(0, y - pad),
           min(img.width, x + w + pad), min(img.height, y + h + pad))
    crop = img.crop(box)
    out = path.rsplit(".", 1)[0] + f"_crop_{x}_{y}.png"
    crop.save(out)
    print(f"裁剪 {box} → {out}  ({crop.width}x{crop.height})")

    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()
    result, _ = ocr(np.asarray(crop))
    print(f"原图 OCR: {result if result else '（空）'}")
    if not result:
        inv = ImageOps.invert(crop)
        result, _ = ocr(np.asarray(inv))
        print(f"反色 OCR: {result if result else '（空）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
