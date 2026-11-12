# -*- coding: utf-8 -*-
"""微调权重 → ONNX（生产口径），含 torch↔onnxruntime 对齐校验。

- 输入 images [1,3,384,640]（/255 由调用方做，与现役模型一致），输出 da/ll logits；
- opset 跟随现役模型（从其模型文件解析），无动态轴；
- 用真实帧对比导出前后 softmax 输出（<1e-4 视为无损）。

用法：python export_onnx.py [checkpoint]
  默认 checkpoint = fine_tune_out/ft_small_best.pth
  产物：weights/plus_small_ft_384x640.onnx
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UP = ROOT / "upstream"
CKPT = ROOT / "fine_tune_out" / (sys.argv[1] if len(sys.argv) > 1 else "ft_small_best.pth")
OUT_ONNX = ROOT / "weights" / (sys.argv[2] if len(sys.argv) > 2
                               else "plus_small_ft_384x640.onnx")
OLD_ONNX = HERE.parents[3] / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx"
RAW_ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"

_mpl = types.ModuleType("matplotlib")
_mpl.pyplot = types.ModuleType("matplotlib.pyplot")
sys.modules.setdefault("matplotlib", _mpl)
sys.modules.setdefault("matplotlib.pyplot", _mpl.pyplot)
sys.path.insert(0, str(UP))

import cv2  # noqa: E402
import onnxruntime as ort  # noqa: E402
import torch  # noqa: E402

from model.model import TwinLiteNetPlus  # noqa: E402
from transplant import parse_initializers, parse_nodes, _fields  # noqa: E402


def prod_opset(path: Path) -> int:
    model = path.read_bytes()
    graph = None
    opset = 13
    for f, _, v in _fields(model):
        if f == 7 and isinstance(v, bytes):
            graph = v
        if f == 8 and isinstance(v, bytes):   # opset_import {1:domain, 2:version}
            dom = ver = None
            for sf, _, sv in _fields(v):
                if sf == 1:
                    dom = sv.decode()
                elif sf == 2:
                    ver = sf and sv
            if dom in ("", None) and ver:
                opset = int(ver)
    assert graph is not None
    return opset


def letterbox(rgb: np.ndarray) -> np.ndarray:
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, 12, 12, 0, 0, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def softmax1(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(0, keepdims=True))
    return (e / e.sum(0, keepdims=True))[1]


def main():
    model = TwinLiteNetPlus(types.SimpleNamespace(config="small"))
    ck = torch.load(CKPT, map_location="cpu", weights_only=True)
    model.load_state_dict(ck["state_dict"])
    model.eval()
    print(f"checkpoint: {CKPT.name} (epoch {ck.get('epoch')}, val_IoU={ck.get('val_IoU'):.4f})")

    opset = prod_opset(OLD_ONNX)
    dummy = torch.zeros(1, 3, 384, 640)
    OUT_ONNX.parent.mkdir(exist_ok=True)
    torch.onnx.export(model, dummy, str(OUT_ONNX), input_names=["images"],
                      output_names=["da", "ll"], opset_version=opset,
                      do_constant_folding=True, dynamo=False)
    print(f"导出: {OUT_ONNX.name}（opset {opset}，与现役一致）")

    sess = ort.InferenceSession(str(OUT_ONNX), providers=["CPUExecutionProvider"])
    names = [o.name for o in sess.get_outputs()]
    assert names == ["da", "ll"], names
    worst = 0.0
    for skey in ("20261109_095756_manual_p2",):
        for fp in sorted((RAW_ROOT / skey / "raw").glob("*.jpg"))[::200][:6]:
            rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
            x = letterbox(rgb)
            ref = dict(zip(names, sess.run(names, {"images": x})))
            with torch.no_grad():
                da_t, ll_t = model(torch.from_numpy(x))
            for refv, tt in ((ref["da"], da_t), (ref["ll"], ll_t)):
                worst = max(worst, float(np.abs(softmax1(refv[0])
                                                - softmax1(tt[0].numpy())).max()))
    print(f"对齐校验（softmax 后最大绝对差）: {worst:.3e}")
    print("导出判定:", "PASS" if worst < 1e-4 else "FAIL")


if __name__ == "__main__":
    main()
