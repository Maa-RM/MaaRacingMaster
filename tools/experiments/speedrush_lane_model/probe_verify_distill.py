# -*- coding: utf-8 -*-
"""教师蒸馏权重的原始分辨率验收：现役 / 蒸馏 / 差分三联 + 逐帧机检。

维护者验收口径（2026-11-12）：目标贴脸线必须出线；不得有近带 / 天空 /
空沥青野线；原有车道线不漂移。机检只是代理，位置正确性以叠图目检裁决。

用法：.venv/Scripts/python.exe probe_verify_distill.py
产物：fine_tune_out/teacher_labels/verify_distill_*.jpg、verify_summary.md
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
                          else "plus_small_distill_384x640.onnx")
TAG = sys.argv[2] if len(sys.argv) > 2 else "distill"
OUT = HERE / "fine_tune_out" / "teacher_labels"
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
REG = {"right": (500, 635, 270, 325), "yellow": (15, 240, 215, 359)}
SKY = (0, 640, 0, 165)


def prob(sess, key, im):
    c = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
    pad = cv2.copyMakeBorder(c, 12, 12, 0, 0, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    x = np.ascontiguousarray((pad.astype(np.float32) / 255).transpose(2, 0, 1)[None])
    lo = sess.run([key], {sess.get_inputs()[0].name: x})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    return (e / e.sum(0))[1][12:-12]


def st(p, b):
    if b is None:
        return None
    q = p[b[2]:b[3], b[0]:b[1]]
    return round(float(q.max()), 4), int((q > .3).sum())


def panel(im, p, title, extra=None, box=None):
    vis = im.copy()
    big = cv2.resize((p > .3).astype(np.uint8) * 255, (1280, 720),
                     interpolation=cv2.INTER_NEAREST) > 0
    vis[big] = (0.3 * vis[big] + 0.7 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
    if extra:
        add, lost = extra
        A = cv2.resize(add.astype(np.uint8) * 255, (1280, 720),
                       interpolation=cv2.INTER_NEAREST) > 0
        L = cv2.resize(lost.astype(np.uint8) * 255, (1280, 720),
                       interpolation=cv2.INTER_NEAREST) > 0
        vis[A] = (0.25 * vis[A] + 0.75 * np.array([0, 0, 255], np.float32)).astype(np.uint8)
        vis[L] = (0.25 * vis[L] + 0.75 * np.array([255, 0, 0], np.float32)).astype(np.uint8)
    if box:
        x0, x1, y0, y1 = box
        cv2.rectangle(vis, (x0 * 2, y0 * 2), (x1 * 2, y1 * 2), (0, 0, 255), 2)
    cv2.putText(vis, title, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.95, (255, 255, 255), 2)
    return cv2.resize(vis, (960, 540))


def main():
    so = ort.InferenceSession(str(OLD), providers=["CPUExecutionProvider"])
    sn = ort.InferenceSession(str(NEW), providers=["CPUExecutionProvider"])
    ko = next(o.name for o in so.get_outputs() if "ll" in o.name)
    kn = next(o.name for o in sn.get_outputs() if "ll" in o.name)
    lines = ["# 教师蒸馏权重验收（原始分辨率目检 + 机检）", "",
             "红=新增响应，蓝=消失响应；绿=>0.3 概率。", "",
             "| 帧 | 目标线 原生→蒸馏 | 右侧线 | 黄线 | 天空野线 | 全图>0.3 |",
             "|---|---|---|---|---|---|"]
    for tag, fp, box in FRAMES:
        if not fp.exists():
            continue
        im = cv2.imread(str(fp))
        po, pn = prob(so, ko, im), prob(sn, kn, im)
        add, lost = (pn > .3) & (po < .1), (po > .3) & (pn < .1)
        sky_n = int((pn[SKY[2]:SKY[3]] > .3).sum())
        sky_o = int((po[SKY[2]:SKY[3]] > .3).sum())
        lines.append(
            f"| {tag} | {st(po, box)} → {st(pn, box)} | {st(po, REG['right'])} → "
            f"{st(pn, REG['right'])} | {st(po, REG['yellow'])} → {st(pn, REG['yellow'])} | "
            f"{sky_o} → {sky_n} | {int((po > .3).sum())} → {int((pn > .3).sum())} |")
        print(f"{tag}: 目标 {st(po, box)} → {st(pn, box)} | 天空 {sky_o}→{sky_n} | "
              f"目标框外新增 {int(add.sum()) - int(add[box[2]:box[3], box[0]:box[1]].sum()) if box else int(add.sum())}")
        p1 = panel(im, po, "BEFORE native", box=box)
        p2 = panel(im, pn, "AFTER teacher-distill", box=box)
        p3 = panel(im, pn, "DIFF red=new blue=lost", (add, lost), box)
        cv2.imwrite(str(OUT / f"verify_{TAG}_{tag}.jpg"),
                    np.vstack([np.hstack([p1, p2]),
                               np.hstack([p3, np.zeros_like(p3)])]),
                    [cv2.IMWRITE_JPEG_QUALITY, 90])
    (OUT / f"verify_{TAG}_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("产物:", OUT)


if __name__ == "__main__":
    main()
