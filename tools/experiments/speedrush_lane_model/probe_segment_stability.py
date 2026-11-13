# -*- coding: utf-8 -*-
"""连帧稳定性验证：贴脸盲区线在一段连续帧里是否稳定成线、会不会闪断或凭空造线。

为什么需要这一层（2026-11-12）：
单帧验收只能证明「这一帧能出线」。生产是逐帧跑的，真正要回答的是三件事：
  1. 真有线的帧能不能稳定检出（检出率 / 闪断）；
  2. 没线的帧会不会在目标带凭空造线（假阳性）；
  3. 检出时线的横向位置在帧间是否平滑（跳变 = 抖动，会毁掉时序平滑）。

独立基准：亮度真值（`make_target_labels.extract`）——纯像素亮带，不碰模型，
只用于回答「这一帧画面里到底有没有那条近贴脸的粗白线」。

判据（与生产同口径）：
- 目标带 = 内容域 x∈[200,340]，近场 y∈[265,360]；
- 「检出」= 生产线提取（行扫描→轨迹→颜色/残差/线角门）后，存在一条 near 线
  在 y=310 处的 x 落在目标带内；
- 闪断 = 相邻帧都应有线（真值有）但中间某帧没检出；
- 跳变 = 相邻两帧都检出时，x@y310 差值超过 JUMP_PX。

用法（GPU 环境或普通环境均可）：
  python probe_segment_stability.py [新权重文件名] [起帧] [止帧]
产物：first_look_out/segment/stability.csv、stability.jpg
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fine_tune"))

import probe_first_look as P          # noqa: E402
import make_target_labels as mtl      # noqa: E402

OLD = (HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
       / "plus_small_384x640.onnx")
NEW = HERE / "weights" / (sys.argv[1] if len(sys.argv) > 1
                          else "plus_small_paperloss_nopcaa_384x640.onnx")
F0 = int(sys.argv[2]) if len(sys.argv) > 2 else 180
F1 = int(sys.argv[3]) if len(sys.argv) > 3 else 260

RAW = (Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
       / "20261109_104823_manual_p2" / "raw")
OUT = HERE / "first_look_out" / "segment"

BAND_X = (200, 340)     # 目标带横向范围（内容域）
BAND_Y = (265, 360)     # 目标带纵向范围
PROBE_Y = 310           # 读线位置的行
JUMP_PX = 8.0           # 相邻帧位置跳变阈值


def line_x_at(line, y_content):
    """线在内容域某行的 x（line 用全幅坐标，故乘 2）。"""
    return (line["s"] * (y_content * 2) + line["a"]) / 2.0


def target_line(lines):
    """目标带内、覆盖近场的线；多条取 x 最靠带中心的一条。"""
    cand = []
    for c in lines:
        # 不要求 band=="near"：生产的 accepted 判定只看 rms/theta，近段拟合失败
        # 时用远段（f0239 就是 33 点、rms 0.8、x=309 的合格线，却被 band 卡掉）。
        # 只要求线的 y 范围覆盖读点所在行。
        if not (c["y0"] <= PROBE_Y * 2 <= c["y1"]):
            continue
        x = line_x_at(c, PROBE_Y)
        if BAND_X[0] - 6 <= x <= BAND_X[1] + 6:
            cand.append((abs(x - (BAND_X[0] + BAND_X[1]) / 2), x, c))
    if not cand:
        return None, None
    cand.sort(key=lambda t: t[0])
    return cand[0][1], cand[0][2]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    so = ort.InferenceSession(str(OLD), providers=["CPUExecutionProvider"])
    sn = ort.InferenceSession(str(NEW), providers=["CPUExecutionProvider"])
    print(f"对比：现役 vs {NEW.name}，帧 {F0}~{F1}，目标带 x{BAND_X} y{BAND_Y}")

    rows = []
    for fid in range(F0, F1 + 1):
        fp = RAW / f"{fid:04d}_raw.jpg"
        if not fp.exists():
            continue
        bgr = cv2.imread(str(fp))
        if bgr is None:
            continue
        content = cv2.resize(bgr, (640, 360), interpolation=cv2.INTER_AREA)

        # 独立基准：亮度真值
        mask, info = mtl.extract(content)
        gt = info is not None
        gt_x = None
        if gt:
            ys, xs = np.nonzero(mask > 0)
            sel = np.abs(ys - PROBE_Y) <= 3
            if sel.any():
                gt_x = float(xs[sel].mean())

        row = dict(fid=fid, gt=int(gt),
                   gt_x=None if gt_x is None else round(gt_x, 1))
        for tag, sess in (("old", so), ("new", sn)):
            prob = P.lane_prob(sess, bgr, padded=True)
            lines = P.extract_lines(bgr, prob)
            x, ln = target_line(lines)
            row[f"{tag}_n"] = len(lines)
            row[f"{tag}_x"] = None if x is None else round(float(x), 1)
            row[f"{tag}_hit"] = int(x is not None)
        rows.append(row)

    # 汇总
    def summarize(tag):
        tp = sum(1 for r in rows if r["gt"] and r[f"{tag}_hit"])
        fn = sum(1 for r in rows if r["gt"] and not r[f"{tag}_hit"])
        fp = sum(1 for r in rows if not r["gt"] and r[f"{tag}_hit"])
        tn = sum(1 for r in rows if not r["gt"] and not r[f"{tag}_hit"])
        # 闪断：真值连续为有的区段里出现未检出
        breaks = 0
        for a, b in zip(rows, rows[1:]):
            if a["gt"] and b["gt"] and a[f"{tag}_hit"] and not b[f"{tag}_hit"]:
                breaks += 1
        # 位置跳变：相邻都检出
        jumps = []
        for a, b in zip(rows, rows[1:]):
            if a[f"{tag}_hit"] and b[f"{tag}_hit"]:
                d = abs(a[f"{tag}_x"] - b[f"{tag}_x"])
                jumps.append(d)
        return dict(tp=tp, fn=fn, fp=fp, tn=tn, breaks=breaks,
                    jump_p50=round(float(np.median(jumps)), 2) if jumps else None,
                    jump_max=round(float(max(jumps)), 2) if jumps else None,
                    jump_gt=sum(1 for d in jumps if d > JUMP_PX))

    s_old, s_new = summarize("old"), summarize("new")
    print(f"\n{'':6s} {'TP':>3s} {'FN':>3s} {'FP':>3s} {'TN':>3s} "
          f"{'闪断':>4s} {'跳变中位':>8s} {'跳变max':>7s} {'>8px次数':>8s}")
    for name, s in (("现役", s_old), ("新版", s_new)):
        print(f"{name:6s} {s['tp']:3d} {s['fn']:3d} {s['fp']:3d} {s['tn']:3d} "
              f"{s['breaks']:4d} {str(s['jump_p50']):>8s} {str(s['jump_max']):>7s} "
              f"{s['jump_gt']:8d}")

    with (OUT / "stability.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # 可视化：逐帧线位置条带（上=现役，下=新版），空心=真值
    H = 40
    img = np.full((H * 2 + 60, len(rows) * 10 + 60, 3), 255, np.uint8)
    for k, (tag, yoff, color) in enumerate((("old", 30, (150, 150, 150)),
                                            ("new", 30 + H, (40, 90, 200)))):
        for i, r in enumerate(rows):
            x0 = 50 + i * 10
            if r["gt_x"] is not None:
                cv2.circle(img, (x0 + 4, yoff + H - 6), 3, (0, 180, 0), 1)
            if r[f"{tag}_x"] is not None:
                cv2.line(img, (x0 + 4, yoff + H - 4),
                         (x0 + 4, yoff + 4), color, 2)
    cv2.putText(img, "top: native   bottom: paper-loss   green circle: brightness GT",
                (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1)
    cv2.putText(img, f"frames {F0}-{F1}", (8, H * 2 + 52),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    cv2.imwrite(str(OUT / "stability.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"\n产物：{OUT / 'stability.csv'}、{OUT / 'stability.jpg'}")


if __name__ == "__main__":
    main()
