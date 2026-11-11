# -*- coding: utf-8 -*-
"""深度图结构自检渲染：各模型对同一帧的 V（近=小）伪彩 + 原图对照。

判据核对用（标尺独立）：目检确认输出是真深度图（路面平滑、近处小、出租车
轮廓可辨）而非退化平滑图/常数图。渲染到 bakeoff_out/。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402

OUT = Path(__file__).parent / "bakeoff_out"
FRAME = 103  # 撞近段：出租车应清晰可辨


def main() -> None:
    fr = cv2.cvtColor(cv2.imread(str(pb.FRAMES_DIR / f"0103_raw.jpg")),
                      cv2.COLOR_BGR2RGB)

    specs = [
        ("yolo26n", "yolo26_depth/yolo26n-depth.onnx"),
        ("yolo26m", "yolo26_depth/yolo26m-depth.onnx"),
        ("moge2_vits_fp32", "moge2_vits/model_fp32.onnx"),
        ("depthart_mnv4s_448", "depthart/relative_mnv4s_448_default.onnx"),
        ("depthart_mnv4m_448", "depthart/relative_mnv4m_448_default.onnx"),
        ("zipdepth_fp16", "zipdepth/zipdepth_fp16_opset17.onnx"),
        ("midas_small", "midas/model-small.onnx"),
    ]
    lm640 = (pb.make_session("lite_mono_base_640x192/encoder.onnx"),
             pb.make_session("lite_mono_base_640x192/decoder.onnx"))
    lm1024 = (pb.make_session("lite_mono_base_1024x320/encoder.onnx"),
              pb.make_session("lite_mono_base_1024x320/decoder.onnx"))

    panels = []
    panels.append(("source", cv2.resize(fr, (640, 360))))

    def add(name, v):
        v = pb.to_near_small(v)
        v = pb.resize_to(v, (720, 1280))
        lo, hi = np.nanpercentile(v, [2, 98])
        norm = np.clip((v - lo) / (hi - lo + 1e-9), 0, 1)
        # 近=小 → 反色让近处亮（可读性：亮=近）
        heat = cv2.applyColorMap(((1 - norm) * 255).astype(np.uint8),
                                 cv2.COLORMAP_TURBO)
        panels.append((name, cv2.resize(heat, (640, 360))))
        print(f"{name}: v range [{np.nanmin(v):.3f}, {np.nanmax(v):.3f}] "
              f"std {np.nanstd(v):.4f}")

    v, _ = pb.run_lite_mono(lm640[0], lm640[1], fr, (192, 640))
    add("lite_mono_640x192", v)
    v, _ = pb.run_lite_mono(lm1024[0], lm1024[1], fr, (320, 1024))
    add("lite_mono_1024x320", v)
    for name, rel in specs:
        sess = pb.make_session(rel, free_dims={"batch_size": 1, "height": 720,
                                               "width": 1280}
                               if name.startswith("moge2") else None)
        if name.startswith("yolo26"):
            v, _ = pb.run_yolo26(sess, fr)
        elif name.startswith("moge2"):
            v, _ = pb.run_moge(sess, fr)
        elif name.startswith("depthart"):
            v, _ = pb.run_depthart(sess, fr, False)
        elif name.startswith("zipdepth"):
            v, _ = pb.run_zipdepth(sess, fr)
        else:
            v, _ = pb.run_midas(sess, fr)
        add(name, v)

    rows = []
    for i in range(0, len(panels), 2):
        pair = [p[1] for p in panels[i:i + 2]]
        while len(pair) < 2:
            pair.append(np.zeros_like(pair[0]))
        rows.append(np.hstack(pair))
    canvas = np.vstack(rows)
    cv2.imwrite(str(OUT / f"sanity_frame{FRAME:04d}.jpg"), canvas)
    print("saved:", OUT / f"sanity_frame{FRAME:04d}.jpg")


if __name__ == "__main__":
    main()
