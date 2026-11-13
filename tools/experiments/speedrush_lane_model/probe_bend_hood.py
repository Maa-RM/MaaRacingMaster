# -*- coding: utf-8 -*-
"""把画面「弯」出一个车头盖：连续非线性重映射，单次推理。

维护者提案：模型在真车数据里习惯「画面底部中央是引擎盖」，游戏帧这块却是
路面加一条贴脸粗白线，可能因此拒绝响应。用一次连续重映射在输入侧伪造一个
拱形车头盖，并把被盖住的近场内容沿弧线压到盖子上方的可见区（粗白线随之
变短变细），看现役模型能否**一次推理**就把那条线吐出来。

与既有探针的区别：`probe_local_remap.py` / `probe_local_reshape.py` 都是切块
搬运或线性仿射（有接缝、上下文不动）；这里是**连续无接缝的非线性重映射**，
且不做任何模型融合、不跑第二次推理。

判据：目标白线经同一映射落到变形域后的响应；「抹掉白线」的空白对照必须仍低，
否则高响应是变形接缝造的假线。

自包含，不 import 业务码。用法：.venv/Scripts/python.exe probe_bend_hood.py
产物：first_look_out/bend/summary.md、overlay_*.jpg
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
MODEL = (HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
         / "plus_small_384x640.onnx")
FP = (Path.home() / "AppData/Roaming/MaaRacingMaster/debug/20261110_204716"
      / "speedrush/drive/20261110_205224_p1/raw/0202_raw.jpg")
OUT = HERE / "first_look_out" / "bend"

H, W = 360, 640
YH, VPX = 177.1, 320.35
HOOD_COLOR = (50, 52, 56)          # 中性车头盖（BGR）
TARGET = (310, 339, 253, 360)      # 0202 帧贴脸粗白线，内容域 x0,x1,y0,y1

VARIANTS = [
    ("native", "native", 245, False),
    ("hood_mask_only", "mask_only", 245, False),
    ("bend_y_top210", "bend_y", 210, False),
    ("bend_y_top245", "bend_y", 245, False),
    ("bend_y_top280", "bend_y", 280, False),
    ("bend_radial_top245", "bend_radial", 245, False),
    ("bend_radial_top280", "bend_radial", 280, False),
    ("bend_y_blank", "bend_y", 245, True),
    ("bend_radial_blank", "bend_radial", 245, True),
]


def make_maps(kind: str, top: float):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    # 拱形盖沿：中央最高（top），两端降到画面底边
    hood = np.clip(top + (H - top) * ((xx - VPX) / (W / 2)) ** 2, YH + 15, H)
    scale = (hood - YH) / (H - YH)          # 每列纵向压缩系数 0.2~1.0
    ys = YH + (yy - YH) / scale             # dst→src 反解
    xs = (xx.copy() if kind == "bend_y"
          else VPX + (xx - VPX) / scale)     # radial：沿地面射线同步压窄
    cover = yy <= hood
    return xs, ys, cover


def build(kind, content, top, blank):
    if kind == "native":
        mark = np.zeros((H, W), np.uint8)
        mark[TARGET[2]:TARGET[3], TARGET[0]:TARGET[1]] = 255
        return content.copy(), mark
    src = content.copy()
    if blank:
        x0, x1, y0, y1 = TARGET
        w = x1 - x0
        src[y0:y1, x0:x1] = src[y0:y1, max(0, x0 - w):max(0, x0 - w) + w]
    mark_src = np.zeros((H, W), np.uint8)
    mark_src[TARGET[2]:TARGET[3], TARGET[0]:TARGET[1]] = 255
    if kind == "mask_only":
        yy, xx = np.indices((H, W))
        hood = np.clip(top + (H - top) * ((xx - VPX) / (W / 2)) ** 2, YH + 15, H)
        cover = yy <= hood
        out = src.copy()
        out[~cover] = HOOD_COLOR
        mark = mark_src.copy()
        mark[~cover] = 0
        return out, mark
    xs, ys, cover = make_maps(kind, top)
    out = cv2.remap(src, xs, ys, interpolation=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE)
    out[~cover] = HOOD_COLOR
    mark = cv2.remap(mark_src, xs, ys, interpolation=cv2.INTER_NEAREST,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    mark[~cover] = 0
    return out, mark


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    im = cv2.imread(str(FP))
    if im is None:
        raise SystemExit(f"缺帧：{FP}")
    content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    key = next(n for n in [o.name for o in sess.get_outputs()] if "ll" in n)

    def prob(c):
        pad = cv2.copyMakeBorder(c, 12, 12, 0, 0,
                                 cv2.BORDER_CONSTANT, value=(114, 114, 114))
        x = np.ascontiguousarray((pad.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
        lo = sess.run([key], {sess.get_inputs()[0].name: x})[0][0]
        e = np.exp(lo - lo.max(0, keepdims=True))
        return (e / e.sum(0))[1][12:-12]

    rows = ["# 「弯」出车头盖：连续非线性重映射，单次推理", "",
            "目标线为 0202 帧近场中央粗白虚线（内容域 x310-339 / y253-360）。"
            "读数取该线经同一映射落到变形域后的区域；空白对照=抹掉白线后同样变形。", "",
            "| 变体 | 目标区 max | 目标区 >0.3 像素 | 全图 >0.3 像素 |",
            "|---|---|---|---|"]
    for name, kind, top, blank in VARIANTS:
        frame, mark = build(kind, content, top, blank)
        p = prob(frame)
        q = p[mark > 0]
        mx = float(q.max()) if q.size else 0.0
        n = int((q > 0.30).sum()) if q.size else 0
        total = int((p > 0.30).sum())
        rows.append(f"| {name} | {mx:.4f} | {n} | {total} |")
        vis = cv2.resize(frame, (1280, 720))
        heat = np.zeros((H, W, 3), np.uint8)
        heat[p > 0.30] = (0, 255, 0)
        blend = cv2.addWeighted(vis, 0.7,
                                cv2.resize(heat, (1280, 720),
                                           interpolation=cv2.INTER_NEAREST), 0.5, 0)
        mb = cv2.resize((mark > 0).astype(np.uint8) * 255, (1280, 720),
                        interpolation=cv2.INTER_NEAREST) > 0
        blend[mb] = (0, 0, 255)
        cv2.putText(blend, name, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (255, 255, 255), 2)
        cv2.imwrite(str(OUT / f"overlay_{name}.jpg"), blend,
                    [cv2.IMWRITE_JPEG_QUALITY, 90])
    (OUT / "summary.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))
    print("产物：", OUT)


if __name__ == "__main__":
    main()
