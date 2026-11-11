# -*- coding: utf-8 -*-
"""IO 形状探针：枚举 weights/ 下各候选 ONNX 的输入/输出名称、形状、类型。

自包含（不 import maaracing_master）；形状发现用 CPU provider（会话初始化快、
元数据与 provider 无关），延迟测量在 bakeoff 主脚本用 DML。
"""
from __future__ import annotations

import onnxruntime as ort
from pathlib import Path

ROOT = Path(__file__).parent / "weights"

MODELS = [
    "lite_mono_base_640x192/encoder.onnx",
    "lite_mono_base_640x192/decoder.onnx",
    "lite_mono_base_1024x320/encoder.onnx",
    "lite_mono_base_1024x320/decoder.onnx",
    "yolo26_depth/yolo26n-depth.onnx",
    "moge2_vits/model_fp16.onnx",
    "depthart/relative_mnv4s_448_default.onnx",
    "depthart/relative_mnv4m_448_default.onnx",
    "depthart/relative_b_448_default.onnx",
    "depthart/metric_s_448_kv.onnx",
    "depthart/metric_s_448_fused.onnx",
    "zipdepth/zipdepth_fp16_opset17.onnx",
    "midas/model-small.onnx",
]


def _shape_str(t) -> str:
    dims = ["?" if d is None or isinstance(d, str) else d for d in t.shape]
    return f"{t.name} {dims} {t.type}"


def main() -> None:
    for rel in MODELS:
        p = ROOT / rel
        if not p.exists():
            print(f"== {rel}: MISSING")
            continue
        print(f"== {rel}")
        try:
            so = ort.SessionOptions()
            so.log_severity_level = 3
            sess = ort.InferenceSession(str(p), sess_options=so,
                                        providers=["CPUExecutionProvider"])
            for i in sess.get_inputs():
                print("  IN ", _shape_str(i))
            for o in sess.get_outputs():
                print("  OUT", _shape_str(o))
        except Exception as e:  # noqa: BLE001
            print("  ERROR:", str(e)[:200])


if __name__ == "__main__":
    main()
