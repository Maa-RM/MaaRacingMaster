# -*- coding: utf-8 -*-
"""车头区局部重映射：只改位置，看现役模型能否检出近场贴脸大虚线。

背景（2026-11-12 复核 f0204）：同一帧里，近场中央竖直粗白虚线的原生响应
约 0.0028，而右侧斜白虚线响应约 0.9993。另：旧点测用的「中心大虚线」框
（全幅 830-935 × 530-718）内其实是空沥青，那个零响应是正确输出，不能当
盲区证据；首版探针的车头区块也定偏了（没覆盖目标线），其读数已作废。

本探针固定像素内容，只把「车头区」整块搬到画面别处（纯平移；不做透视、
不做整图缩放、不做逆映射、不做概率融合、不重训），量同一条线在目标位置的
响应。三组对照缺一不可：

1. 双向：把右侧已能检出的同构型线搬到车头区，看是否归零。
2. 空白：把同一块抹掉白线后同样搬运，排除接缝/搬运动作造出的假响应。
3. 原生：原帧同位置响应。

坐标系一律用内容域 640x360（生产 letterbox 前的尺度）。自包含，不 import
业务码。用法：.venv/Scripts/python.exe probe_local_remap.py
产物：first_look_out/remap/summary.md、overlay_*.jpg
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

# 车头区（含近场中央竖直粗白虚线）。坐标由逐列高亮峰定位：线在内容域
# x 258-282 / y 265-360（全幅 516-564 / 530-720），原生 max 0.0028。
SRC = (200, 360, 240, 360)             # 160x120, x0,x1,y0,y1
LINE_IN_SRC = (58, 82, 25, 120)        # 块内相对 x0,x1,y0,y1
# 右侧近场（含同帧已检出的斜白虚线，内容域 x 500-635 / y 270-325，max 0.9993）
DST = (480, 640, 240, 360)
LINE_IN_DST = (20, 155, 30, 85)

TARGETS = {
    "right_near": (480, 640, 240, 360),
    "mid_center": (240, 400, 145, 265),
    "far_center": (240, 400, 180, 300),
    "upper_left": (40, 200, 90, 210),
}


def lane_prob(sess, content_bgr: np.ndarray) -> np.ndarray:
    im = cv2.copyMakeBorder(content_bgr, PAD, PAD, 0, 0,
                            cv2.BORDER_CONSTANT, value=GRAY)
    x = np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
    lo = sess.run([KEY], {sess.get_inputs()[0].name: x})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    return (e / e.sum(0))[1][PAD:-PAD]     # 360x640


def stat(prob, box) -> tuple[float, float, int]:
    x0, x1, y0, y1 = box
    sub = prob[max(0, y0):min(360, y1), max(0, x0):min(640, x1)]
    if sub.size == 0:
        return 0.0, 0.0, 0
    return float(sub.max()), float(sub.mean()), int((sub > 0.30).sum())


def abs_box(block, rel):
    return (block[0] + rel[0], block[0] + rel[1],
            block[2] + rel[2], block[2] + rel[3])


def grab(canvas, box):
    return canvas[box[2]:box[3], box[0]:box[1]].copy()


def put(canvas, box, tile):
    canvas[box[2]:box[3], box[0]:box[1]] = tile


def fill(canvas, box):
    canvas[box[2]:box[3], box[0]:box[1]] = GRAY


def blank_tile(tile, rel):
    """抹掉块内白线：用线左侧同宽沥青平移覆盖（不引入新边界）。"""
    out = tile.copy()
    x0, x1, y0, y1 = rel
    w = x1 - x0
    out[y0:y1, x0:x1] = tile[y0:y1, max(0, x0 - w):max(0, x0 - w) + w]
    return out


def overlay(name, frame_bgr, prob, mark_box, note):
    vis = frame_bgr.copy()
    x0, x1, y0, y1 = mark_box
    cv2.rectangle(vis, (x0 * 2, y0 * 2), (x1 * 2, y1 * 2), (0, 0, 255), 2)
    heat = np.zeros((360, 640, 3), np.uint8)
    m = prob > 0.30
    heat[m] = (0, 255, 0)
    heat_big = cv2.resize(heat, (1280, 720), interpolation=cv2.INTER_NEAREST)
    blend = cv2.addWeighted(cv2.resize(frame_bgr, (1280, 720)), 0.75, heat_big, 0.45, 0)
    cv2.rectangle(blend, (x0 * 2, y0 * 2), (x1 * 2, y1 * 2), (0, 0, 255), 2)
    cv2.putText(blend, note, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    cv2.imwrite(str(OUT / f"overlay_{name}.jpg"), blend,
                [cv2.IMWRITE_JPEG_QUALITY, 90])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    global KEY
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    KEY = next(n for n in [o.name for o in sess.get_outputs()] if "ll" in n)
    full = cv2.imread(str(RAW))
    if full is None:
        raise SystemExit(f"缺帧：{RAW}")
    base = cv2.resize(full, (640, 360), interpolation=cv2.INTER_AREA)

    rows = ["# 车头区局部重映射读数（现役 plus_small，只改位置）", "",
            "判据：同一条线的像素搬到目标位置后，框内 max 是否从 ~0 抬到可用；"
            "空白对照必须仍接近 0。max/mean 是「在不在」的事实读数，不是优化指标。", "",
            "| 情形 | 线所在框 | max | mean | >0.3 像素 | 全图 >0.3 像素 |",
            "|---|---|---|---|---|---|"]

    def record(label, frame, box, shot=None, note=""):
        p = lane_prob(sess, frame)
        mx, mean, n = stat(p, box)
        total = int((p > 0.30).sum())
        rows.append(f"| {label} | {box} | {mx:.4f} | {mean:.4f} | {n} | {total} |")
        if shot:
            overlay(shot, frame, p, box, note or label)
        return mx, n, p

    # 原生对照：车头区的线（盲）、右侧同构型线（满响应）
    native_src = abs_box(SRC, LINE_IN_SRC)
    native_dst = abs_box(DST, LINE_IN_DST)
    record("原生·车头区大虚线", base, native_src, "native_src", "native: car-hood line (BLIND)")
    record("原生·右侧同构型大虚线", base, native_dst, "native_dst", "native: right near line (DETECTED)")

    # 1) 双向交换：只交换两块，画面统计不变
    sw = base.copy()
    a, b = grab(sw, SRC), grab(sw, DST)
    put(sw, SRC, b); put(sw, DST, a)
    record("交换·车头区线→右侧近场", sw, abs_box(DST, LINE_IN_SRC),
           "swap_hood_to_right", "swap: hood line moved RIGHT")
    record("交换·右侧线→车头区", sw, abs_box(SRC, LINE_IN_DST),
           "swap_right_to_hood", "swap: right line moved to HOOD")

    # 2) 只搬不换：车头块贴到各位置，原位填灰
    for name, T in TARGETS.items():
        f = base.copy()
        put(f, T, grab(base, SRC)); fill(f, SRC)
        record(f"平移·车头区→{name}", f, abs_box(T, LINE_IN_SRC),
               f"move_{name}", f"move hood tile to {name}")

    # 3) 空白对照：抹掉白线后同样搬运（排除接缝/搬运假响应）
    for name in ("right_near", "far_center"):
        T = TARGETS[name]
        f = base.copy()
        put(f, T, blank_tile(grab(base, SRC), LINE_IN_SRC)); fill(f, SRC)
        record(f"空白对照·→{name}", f, abs_box(T, LINE_IN_SRC),
               f"blank_{name}", f"blank tile moved to {name}")

    # 4) 近带整带上移（生产最省事的一种：坐标只需加回 N 行）
    for n in (60, 100, 140):
        f = base.copy()
        band = f[240:360, 0:640].copy()
        fill(f, (0, 640, 240, 360))
        f[240 - n:360 - n, 0:640] = band
        box = (native_src[0], native_src[1], native_src[2] - n, native_src[3] - n)
        record(f"近带整带上移 {n} 行", f, box,
               f"band_up_{n}", f"whole near band shifted up {n} rows")

    # 5) 尺寸变量：缩小一半后放远带（区分「位置」与「尺寸」）
    T = (280, 360, 200, 260)
    f = base.copy()
    small = cv2.resize(grab(base, SRC), (80, 60), interpolation=cv2.INTER_AREA)
    put(f, T, small); fill(f, SRC)
    record("缩小一半·→远带", f, T, "shrink_far", "hood tile shrunk to half, moved far")

    (OUT / "summary.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))
    print("产物：", OUT)


if __name__ == "__main__":
    main()
