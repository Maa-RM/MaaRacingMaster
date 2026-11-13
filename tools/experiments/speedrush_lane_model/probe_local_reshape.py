# -*- coding: utf-8 -*-
"""车头区局部重映射（第二步）：位置证伪后，隔离「线形」这一变量。

`probe_local_remap.py` 已证明：把车头区那条竖直粗白虚线平移到画面任何位置
都仍是盲（max 0.001~0.004），而把右侧已检出的斜白线搬进车头区照样检出
（max 0.999）。所以位置不是主因，问题在那条线自身的形态。

本探针只动「车头区这一块」的几何（横向压缩 / 剪切 / 纵向压缩 / 等比缩小），
不预知线的位置、不做整图变换、不重训、不做概率融合。变换后的块放回原位或
搬到远带，量整块的响应——整块量而不依赖线框，避免再犯「框定偏了」的错。

自包含，不 import 业务码。用法：.venv/Scripts/python.exe probe_local_reshape.py
产物：first_look_out/remap/reshape_summary.md、reshape_*.jpg
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
MODEL = HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx"
RAW = (Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
       / "20261109_104823_manual_p2/raw/0204_raw.jpg")
OUT = HERE / "first_look_out" / "remap"
PAD = 12
GRAY = (114, 114, 114)

SRC = (200, 360, 240, 360)             # 车头区 160x120，含近场中央竖直粗白虚线
LINE_REL = (58, 82, 25, 120)           # 线在块内的相对框（用于空白对照）
FAR = (240, 400, 180, 300)             # 远带同尺寸落点

VARIANTS = [
    ("orig", {}),
    ("sx0.60", dict(sx=0.60)),
    ("sx0.40", dict(sx=0.40)),
    ("sx0.25", dict(sx=0.25)),
    ("k+0.20", dict(k=0.20)),
    ("k-0.20", dict(k=-0.20)),
    ("k+0.40", dict(k=0.40)),
    ("sx0.50+k0.20", dict(sx=0.50, k=0.20)),
    ("sy0.60", dict(sy=0.60)),
    ("scale0.50", dict(sx=0.50, sy=0.50)),
    ("scale0.35", dict(sx=0.35, sy=0.35)),
]


def lane_prob(sess, key, content_bgr):
    im = cv2.copyMakeBorder(content_bgr, PAD, PAD, 0, 0,
                            cv2.BORDER_CONSTANT, value=GRAY)
    x = np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
    lo = sess.run([key], {sess.get_inputs()[0].name: x})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    return (e / e.sum(0))[1][PAD:-PAD]


def tile_of(base, box):
    return base[box[2]:box[3], box[0]:box[1]].copy()


def transform(tile, sx=1.0, sy=1.0, k=0.0):
    """块内仿射：x' = sx·x + k·(y-h/2) + (1-sx)·w/2；y' = sy·y + (1-sy)·h/2。
    k 让竖直线变斜（符合透视），sx 把线压窄。边界复制，不引入灰边接缝。"""
    h, w = tile.shape[:2]
    M = np.float32([[sx, k, (1 - sx) * w / 2 - k * h / 2],
                    [0, sy, (1 - sy) * h / 2]])
    return cv2.warpAffine(tile, M, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def blank(tile):
    """抹掉块内白线（同宽沥青平移覆盖），做变换假响应的对照。"""
    out = tile.copy()
    x0, x1, y0, y1 = LINE_REL
    w = x1 - x0
    out[y0:y1, x0:x1] = tile[y0:y1, max(0, x0 - w):max(0, x0 - w) + w]
    return out


def stat(prob, box):
    sub = prob[box[2]:box[3], box[0]:box[1]]
    return float(sub.max()), float(sub.mean()), int((sub > 0.30).sum())


def shot(name, frame, prob, box, note):
    vis = cv2.resize(frame, (1280, 720))
    heat = np.zeros((360, 640, 3), np.uint8)
    heat[prob > 0.30] = (0, 255, 0)
    blend = cv2.addWeighted(vis, 0.75,
                            cv2.resize(heat, (1280, 720), interpolation=cv2.INTER_NEAREST),
                            0.45, 0)
    cv2.rectangle(blend, (box[0] * 2, box[2] * 2), (box[1] * 2, box[3] * 2), (0, 0, 255), 2)
    cv2.putText(blend, note, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2)
    cv2.imwrite(str(OUT / f"reshape_{name}.jpg"), blend, [cv2.IMWRITE_JPEG_QUALITY, 90])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    key = next(n for n in [o.name for o in sess.get_outputs()] if "ll" in n)
    full = cv2.imread(str(RAW))
    if full is None:
        raise SystemExit(f"缺帧：{RAW}")
    base = cv2.resize(full, (640, 360), interpolation=cv2.INTER_AREA)
    rows = ["# 车头区局部重映射·线形变量隔离（现役 plus_small）", "",
            "位置变量已由 `probe_local_remap.py` 证伪。这里只改车头区这一块的几何，"
            "量整块 max / mean / >0.3 像素；空白对照必须仍接近 0。", "",
            "| 变换 | 落点 | max | mean | >0.3 像素 |",
            "|---|---|---|---|---|"]

    def run(label, tile, T, save=False):
        f = base.copy()
        f[T[2]:T[3], T[0]:T[1]] = tile
        if T != SRC:
            f[SRC[2]:SRC[3], SRC[0]:SRC[1]] = GRAY
        p = lane_prob(sess, key, f)
        mx, mean, n = stat(p, T)
        rows.append(f"| {label} | {'原位' if T == SRC else '远带'} | "
                    f"{mx:.4f} | {mean:.4f} | {n} |")
        if save:
            shot(label.replace("+", "_").replace(".", ""), f, p, T, label)
        return mx, n

    for name, kw in VARIANTS:
        tile = transform(tile_of(base, SRC), **kw) if kw else tile_of(base, SRC)
        run(f"{name}·原位", tile, SRC, save=name in ("orig", "sx0.40", "k+0.20", "scale0.50"))
        run(f"{name}·远带", tile, FAR, save=name in ("sx0.40", "k+0.20", "scale0.50"))

    # 变换假响应对照：抹掉白线后做同样的压缩/剪切
    for name, kw in (("blank-sx0.40", dict(sx=0.40)), ("blank-k+0.20", dict(k=0.20))):
        tile = transform(blank(tile_of(base, SRC)), **kw)
        run(f"{name}·远带", tile, FAR, save=True)

    (OUT / "reshape_summary.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))
    print("产物：", OUT)


if __name__ == "__main__":
    main()
