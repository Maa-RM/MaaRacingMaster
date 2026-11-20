"""检测距离/分辨率 A/B（speedrush，离线）。

**问什么**：整车 1280×720 送进 640×640 的模型等于先缩一半，远处的车只剩
7~10 像素。把画面切成左右两半分别推理（等效 1:1），能不能把远处的车检出来？
这决定「来车出现太晚」是模型输入分辨率造成的，还是物体本身太小、没办法。

口径：

- 两个臂用**同一个**生产权重与同一置信度（与 perception 一致），只改输入几何。
- A 臂 = 整帧 letterbox 到 640（生产现状）；B 臂 = 左右两半各自 letterbox 到 640。
- 两臂的框都映射回全帧像素坐标再做对比；`h` 为全帧框高。
- 只比「有没有车」与最小框高，不比置信度分布。

用法::

    .venv/Scripts/python.exe -X utf8 probe_detect_range.py <帧目录> [取样数]
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(REPO))

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402

MODEL = REPO / "maaracing_master/plugins/_archive/speedrush/resources/models/perception/model.onnx"


def _cars(det, img, ox=0, oy=0):
    _, dets, _ = det(img)
    return [(d["box"][0] + ox, d["box"][1] + oy,
             d["box"][2] + ox, d["box"][3] + oy, d["confidence"])
            for d in dets if d["class_name"] == "car"]


def _h(b):
    return b[3] - b[1]


def _key(b):
    return (round((b[0] + b[2]) / 2 / 10), round((b[1] + b[3]) / 2 / 10))


def _buckets(rows):
    n = [0, 0, 0, 0]
    for b in rows:
        h = _h(b)
        n[0 if h < 16 else 1 if h < 24 else 2 if h < 40 else 3] += 1
    return n


def main(frames_dir: str, n: int = 40) -> None:
    files = sorted(Path(frames_dir).glob("*.jpg"))
    if not files:
        print("没有帧")
        return
    step = max(1, len(files) // n)
    pick = files[::step][:n]
    det = YOLODetector(str(MODEL), conf=0.35, iou=0.45)
    print(f"模型 {MODEL.name}  input {det.input_size}  provider {det.provider}"
          f"  样本 {len(pick)}/{len(files)} 帧")
    tot_a = tot_b = tot_q = 0
    only_b = only_q = 0
    bk_a = [0, 0, 0, 0]
    bk_b = [0, 0, 0, 0]
    bk_q = [0, 0, 0, 0]
    sample = []
    for f in pick:
        img = cv2.imread(str(f))
        if img is None:
            continue
        h, w = img.shape[:2]
        a = _cars(det, img)
        b = []
        for ox, sub in ((0, img[:, :w // 2]), (w // 2, img[:, w // 2:])):
            b.extend(_cars(det, np.ascontiguousarray(sub), ox=ox))
        q = []
        for oy in (0, h // 2):
            for ox in (0, w // 2):
                sub = img[oy:oy + h // 2, ox:ox + w // 2]
                q.extend(_cars(det, np.ascontiguousarray(sub), ox=ox, oy=oy))
        tot_a += len(a)
        tot_b += len(b)
        tot_q += len(q)
        only_b += len(_only(b, a))
        only_q += len(_only(q, a))
        bk_a = [x + y for x, y in zip(bk_a, _buckets(a))]
        bk_b = [x + y for x, y in zip(bk_b, _buckets(b))]
        bk_q = [x + y for x, y in zip(bk_q, _buckets(q))]
        if len(_only(q, a)) >= 2 and len(sample) < 2:
            sample.append((f.name, img.copy(), a, q))
    print(f"整帧 {tot_a}  左右两半 {tot_b}（整帧没有的 {only_b}）"
          f"  四宫格 {tot_q}（整帧没有的 {only_q}）")
    print(f"框高分布([<16, <24, <40, ≥40])")
    print(f"  整帧   {bk_a}")
    print(f"  两半   {bk_b}")
    print(f"  四宫格 {bk_q}")
    out = Path(frames_dir).parent / "detect_range_out"
    out.mkdir(exist_ok=True)
    for name, img, a, q in sample:
        vis = img.copy()
        for x1, y1, x2, y2, c in a:
            cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 200, 0), 1)
        for x1, y1, x2, y2, c in _only(q, a):
            cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 2)
        cv2.imwrite(str(out / f"{name}_cmp.jpg"), vis)
    print(f"对照图输出 {out}")


def _only(xs, ys):
    """xs 里找不到与 ys 中心距 ≤15px 对应物的框。"""
    out = []
    for x in xs:
        cx, cy = (x[0] + x[2]) / 2, (x[1] + x[3]) / 2
        if not any(abs((y[0] + y[2]) / 2 - cx) <= 15
                   and abs((y[1] + y[3]) / 2 - cy) <= 15 for y in ys):
            out.append(x)
    return out


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40)
