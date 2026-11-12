# -*- coding: utf-8 -*-
"""验收：现役 plus_small vs 定向微调权重，在留出帧与常规帧上对比。

读数只看两件事（维护者口径，不追泛化指标）：
1. 目标线（近场中央粗白虚线）出不出得来；
2. 原本能检出的右侧斜白线、黄线走廊还在不在。
另输出原始分辨率三联叠图（现役 / 微调 / 差分：红=新增、蓝=消失），
供「有无近带野线」的目检验收——缩略图看不出野线，必须原始分辨率。

自包含，不 import 业务码。
用法：.venv/Scripts/python.exe probe_verify_targeted.py [新权重文件名]
产物：fine_tune_out/target_labels/verify_*.jpg
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
OLD = (HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
       / "plus_small_384x640.onnx")
NEW = HERE / "weights" / (sys.argv[1] if len(sys.argv) > 1
                          else "plus_small_targeted_v2_384x640.onnx")
RAW = (Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
       / "20261109_104823_manual_p2/raw")
OUT = HERE / "fine_tune_out" / "target_labels"

TARGET = (258, 282, 265, 360)
RIGHT = (500, 635, 270, 325)
YELLOW = (15, 240, 215, 359)
FRAMES = (204, 210, 212, 300, 500)


def prob(sess, key, full):
    c = cv2.resize(full, (640, 360), interpolation=cv2.INTER_AREA)
    pad = cv2.copyMakeBorder(c, 12, 12, 0, 0,
                             cv2.BORDER_CONSTANT, value=(114, 114, 114))
    x = np.ascontiguousarray((pad.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
    lo = sess.run([key], {sess.get_inputs()[0].name: x})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    return (e / e.sum(0))[1][12:-12]


def stat(p, box):
    q = p[box[2]:box[3], box[0]:box[1]]
    return round(float(q.max()), 4), int((q > 0.30).sum())


def panel(title, full, p, diff=None):
    vis = full.copy()
    big = cv2.resize((p > 0.30).astype(np.uint8) * 255, (1280, 720),
                     interpolation=cv2.INTER_NEAREST) > 0
    vis[big] = (0.3 * vis[big] + 0.7 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
    if diff is not None:
        add, lost = diff
        A = cv2.resize(add.astype(np.uint8) * 255, (1280, 720),
                       interpolation=cv2.INTER_NEAREST) > 0
        L = cv2.resize(lost.astype(np.uint8) * 255, (1280, 720),
                       interpolation=cv2.INTER_NEAREST) > 0
        vis[A] = (0.25 * vis[A] + 0.75 * np.array([0, 0, 255], np.float32)).astype(np.uint8)
        vis[L] = (0.25 * vis[L] + 0.75 * np.array([255, 0, 0], np.float32)).astype(np.uint8)
    cv2.putText(vis, title, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    return cv2.resize(vis, (960, 540))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    so = ort.InferenceSession(str(OLD), providers=["CPUExecutionProvider"])
    sn = ort.InferenceSession(str(NEW), providers=["CPUExecutionProvider"])
    ko = next(n for n in [o.name for o in so.get_outputs()] if "ll" in n)
    kn = next(n for n in [o.name for o in sn.get_outputs()] if "ll" in n)
    print(f"对照：{OLD.name}  →  {NEW.name}")
    for fid in FRAMES:
        fp = RAW / f"{fid:04d}_raw.jpg"
        if not fp.exists():
            continue
        full = cv2.imread(str(fp))
        po, pn = prob(so, ko, full), prob(sn, kn, full)
        add = (pn > 0.30) & (po < 0.10)
        lost = (po > 0.30) & (pn < 0.10)
        outside = int(add.sum()) - int(add[TARGET[2]:TARGET[3], TARGET[0]:TARGET[1]].sum())
        print(f"f{fid:04d}: 目标 {stat(po, TARGET)}→{stat(pn, TARGET)} | "
              f"右侧 {stat(po, RIGHT)}→{stat(pn, RIGHT)} | "
              f"黄线 {stat(po, YELLOW)}→{stat(pn, YELLOW)} | "
              f"全图 {int((po > 0.3).sum())}→{int((pn > 0.3).sum())} | "
              f"目标框外新增 {outside}")
        tiles = [panel("BEFORE native", full, po),
                 panel("AFTER targeted", full, pn),
                 panel("DIFF red=new blue=lost", full, pn, (add, lost))]
        sheet = np.vstack([np.hstack(tiles[:2]),
                           np.hstack([tiles[2], np.zeros_like(tiles[2])])])
        cv2.imwrite(str(OUT / f"verify_{fid:04d}.jpg"), sheet,
                    [cv2.IMWRITE_JPEG_QUALITY, 90])
    print("叠图：", OUT)


if __name__ == "__main__":
    main()
