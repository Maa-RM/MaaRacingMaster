# -*- coding: utf-8 -*-
"""C 方案验证：新权重经过生产同口径线提取（颜色/残差/线角/拟合门）后，
到底还剩几条线、目标线在不在、野线有没有被滤掉。

复用 `probe_first_look.py` 的生产同语义实现（row_segments → build_tracks →
fit_line → 颜色/残差/线角门），不重写一套。

用法：.venv/Scripts/python.exe probe_gate_filter.py [权重名]
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import probe_first_look as P  # noqa: E402

NEW = HERE / "weights" / (sys.argv[1] if len(sys.argv) > 1
                          else "plus_small_samcenter_384x640.onnx")
OLD = HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx"
RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug"
FRAMES = [
    ("f0204", RAW / "speedrush/20261109_104823_manual_p2/raw/0204_raw.jpg",
     (258, 282, 265, 360)),
    ("f0202", RAW / "20261110_204716/speedrush/drive/20261110_205224_p1/raw/0202_raw.jpg",
     (310, 339, 253, 360)),
    ("f0210", RAW / "speedrush/20261109_104823_manual_p2/raw/0210_raw.jpg", None),
    ("f0300", RAW / "speedrush/20261109_104823_manual_p2/raw/0300_raw.jpg", None),
    ("f0500", RAW / "speedrush/20261109_104823_manual_p2/raw/0500_raw.jpg", None),
]


def lines_of(sess, bgr):
    # 与生产一致：BGR 直喂（不要转 RGB，见 probe_first_look.lane_prob 注释）
    prob = P.lane_prob(sess, bgr, padded=True)
    return P.extract_lines(bgr, prob), prob


def covers(line, box):
    """线在目标 ROI 的 y 范围内是否穿过该 x 区间（内容域坐标）。"""
    if box is None:
        return False
    x0, x1, y0, y1 = box
    ys = np.arange(max(int(line["y0"]) // 2, y0), min(int(line["y1"]) // 2, y1), 3)
    if len(ys) == 0:
        return False
    xs = (line["s"] * (ys * 2) + line["a"]) / 2.0
    return bool(((xs >= x0 - 6) & (xs <= x1 + 6)).any())


def main():
    so = ort.InferenceSession(str(OLD), providers=["CPUExecutionProvider"])
    sn = ort.InferenceSession(str(NEW), providers=["CPUExecutionProvider"])
    print(f"对比：现役 vs {NEW.name}")
    print(f"{'帧':6s} {'现役线数':>6s} {'新线数':>6s} {'目标被覆盖':>10s} "
          f"{'新线位置(x范围,内容域)':>28s}")
    for tag, fp, box in FRAMES:
        if not fp.exists():
            continue
        bgr = cv2.imread(str(fp))
        lo, _ = lines_of(so, bgr)
        ln, prob = lines_of(sn, bgr)
        hit = any(covers(c, box) for c in ln)
        spans = []
        for c in ln:
            ys = np.arange(int(c["y0"]) // 2, int(c["y1"]) // 2, 4)
            if len(ys) == 0:
                continue
            xs = (c["s"] * (ys * 2) + c["a"]) / 2.0
            spans.append((int(xs.min()), int(xs.max()), c["band"], c["color"]))
        print(f"{tag:6s} {len(lo):>6d} {len(ln):>6d} {str(hit):>10s}  {spans}")
        # 可视化：只画几何门后的线
        vis = bgr.copy()
        for c in ln:
            ys = np.arange(int(c["y0"]), int(c["y1"]) + 1, 4)
            for y in ys:
                x = int((c["s"] * y + c["a"]) / 2.0)
                yy = y // 2
                if 0 <= x < 640 and 0 <= yy < 360:
                    cv2.circle(vis, (x * 2, yy * 2), 3, (0, 255, 0), -1)
        if box:
            x0, x1, y0, y1 = box
            cv2.rectangle(vis, (x0 * 2, y0 * 2), (x1 * 2, y1 * 2), (0, 0, 255), 2)
        cv2.putText(vis, f"{tag} gate-passed lines={len(ln)} target={'YES' if hit else 'NO'}",
                    (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        out = HERE / "first_look_out" / "gate"
        out.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out / f"{tag}_gate.jpg"), vis, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print("产物:", HERE / "first_look_out" / "gate")


if __name__ == "__main__":
    main()
