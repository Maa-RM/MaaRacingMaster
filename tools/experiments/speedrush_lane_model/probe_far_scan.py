"""远线恢复可行性：把行扫描起点上移，看 20~55 米的车道线是否稳定可用。

背景（2026-11-13）：生产行扫描从概率图第 206 行（≈原图 412 行、约 20 米）
开始，远于此的模型响应从未被读取；而同一模型在 54 米处仍有 0.98~1.0 的
线响应。裁切起点是早期步长 3 时代的产物，步长改 1 后没有回收。

本探针只读：同一帧概率图上分别按生产口径与上移口径提取，比较
  1. 接受线数量与横位（上移不得改变近场线）；
  2. 远段拟合覆盖（y0 对应距离）、点数、残差；
  3. 相邻采样帧的横位稳定性（同一物理线跨帧漂移）；
  4. 「只有上移才出现的远端线」数量——假线风险的第一道观察面。

判据与生产同口径：无颜色门、rms<=3.0、|theta|<=0.35。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_far_scan.py [会话 ...] [--step N] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
from maaracing_master.plugins.speedrush.lane_obs import (  # noqa: E402
    PROB_THRESH, _build_tracks, _fit, _letterbox, _prob_from_out)

ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster"
MODEL = (REPO / "maaracing_master/plugins/speedrush/resources/models/lane/"
         "plus_small_384x640.onnx")
OUT = HERE / "first_look_out" / "far_scan"

CAM_H, FOCAL, Y_V = 1.283, 807.7, 361.2
RMS_MAX, THETA_MAX = 3.0, 0.35
VARIANTS = {
    "prod": dict(start=206, far=(440.0, 545.0)),
    "s195": dict(start=195, far=(380.0, 545.0)),
    "s185": dict(start=185, far=(380.0, 545.0)),
}


def _row_scan(prob, start):
    rows = []
    for yc in range(start, 360):
        row = prob[yc]
        idx = np.flatnonzero(row > PROB_THRESH)
        if len(idx) == 0:
            continue
        splits = np.flatnonzero(np.diff(idx) > 1) + 1
        segs = []
        for seg in np.split(idx, splits):
            if len(seg) == 0:
                continue
            p = row[seg]
            segs.append({"yc": yc,
                         "xc": float((seg * p).sum() / p.sum()),
                         "p": float(p.mean())})
        if segs:
            rows.append((yc, segs))
    return rows


def _extract(prob, cfg):
    out = []
    for pts in _build_tracks(_row_scan(prob, cfg["start"])):
        near = _fit(pts, CAM_H, 560.0, 720.0, FOCAL, Y_V)
        far = _fit(pts, CAM_H, *cfg["far"], FOCAL, Y_V)
        f = near or far
        if f is None or f["rms"] > RMS_MAX:
            continue
        if f["theta"] is not None and abs(f["theta"]) > THETA_MAX:
            continue
        out.append({"L": f["L"], "y0": f["y0"], "n": f["n"],
                    "rms": f["rms"],
                    "far_y0": None if far is None else far["y0"],
                    "far_n": 0 if far is None else far["n"],
                    "near": near is not None})
    return out


def _dist(y):
    d = y - Y_V
    return None if d <= 2 else FOCAL * CAM_H / d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*", default=["20261113_163858"])
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--limit", type=int, default=120)
    args = ap.parse_args()
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0].name
    OUT.mkdir(parents=True, exist_ok=True)
    for name in args.sessions:
        frames_p = (ROOT / "debug" / name / "speedrush/drive/frames.jsonl")
        if not frames_p.is_file():
            print(f"[跳过] {name}: 无留帧")
            continue
        frames = [json.loads(s) for s in frames_p.open(encoding="utf8")
                  if s.strip()][:: args.step][: args.limit]
        base = ROOT / "debug" / name / "speedrush/drive"
        acc = {k: [] for k in VARIANTS}
        jitter = {k: [] for k in VARIANTS}
        extra = {k: [] for k in VARIANTS}
        far_cover = {k: [] for k in VARIANTS}
        for fr in frames:
            img = cv2.imread(str(base / fr["file"]))
            if img is None:
                continue
            prob = _prob_from_out(sess.run(
                None, {inp: _letterbox(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))}))
            for key, cfg in VARIANTS.items():
                lines = _extract(prob, cfg)
                acc[key].append(len(lines))
                ys = [l["far_y0"] for l in lines if l["far_y0"] is not None]
                if ys:
                    far_cover[key].append(min(_dist(y) or 0 for y in ys))
                if key == "prod":
                    continue
                # 上移口径：与生产口径逐线匹配，统计漂移与新出现的线
                prod = _extract(prob, VARIANTS["prod"])
                for l in lines:
                    d = [abs(l["L"] - p["L"]) for p in prod]
                    if d and min(d) <= 0.2:
                        jitter[key].append(min(d))
                    else:
                        extra[key].append((l["L"], l["far_y0"],
                                           None if l["far_y0"] is None
                                           else _dist(l["far_y0"])))
        print(f"\n== {name}  采样 {len(frames)} 帧 ==")
        for key in VARIANTS:
            n = acc[key]
            cover = far_cover[key]
            print(f"  [{key}] 接受线/帧 p50={statistics.median(n):.1f} "
                  f"min={min(n)} max={max(n)}"
                  + (f" | 远覆盖 p50={statistics.median(cover):.1f}m "
                     f"max={max(cover):.1f}m" if cover else ""))
            if key != "prod" and jitter[key]:
                j = sorted(jitter[key])
                print(f"        与生产线匹配的横位漂移 p50={statistics.median(j):.3f} "
                      f"p90={j[int(0.9 * len(j))]:.3f} 道；"
                      f"只在上移口径出现的线 {len(extra[key])} 条")
                if extra[key]:
                    ex = sorted(extra[key], key=lambda t: -(t[2] or 0))[:5]
                    print("        样例(L, y0, 距离): "
                          + ", ".join(f"({l:.2f},{y:.0f},"
                                      f"{'--' if z is None else round(z)}m)"
                                      for l, y, z in ex))


if __name__ == "__main__":
    main()
