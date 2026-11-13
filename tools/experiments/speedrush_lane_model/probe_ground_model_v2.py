#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地面模型拟合域改造的离线验证（只读，不改生产）。

旧：全幅行 380~690 一次拟合，MAD 在全幅上算。
新：① 中央列带（自车正前方=必然路面）粗拟 → ② 全宽用粗平面筛贴地点重拟
    → ③ 质量门仍在中央列上算（不让「筛完当然小」自证）。

验证三件事：
  1. 合格率（MAD ≤ 0.06）在 p1/p2 上的变化；
  2. 正常帧（旧方案已合格）新旧平面参数是否接近（不能把标定漂走）；
  3. p2 被救回的帧，新平面在「中央列验证面」上的残差是否真的小。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

APPDATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
MODEL = ROOT / "maaracing_master/plugins/speedrush/resources/models/depth" \
               "/relative_mnv4m_slim_spf_448_default.onnx"

COLS_LO, COLS_HI = 0.30, 0.70   # 中央列带（粗拟 + 质量门）
REFIT_K = 3.0                   # 全宽筛贴地点的容差倍数（× 粗拟 mad）


def _solve3(A, y):
    return np.linalg.solve(A.T @ A, A.T @ y)


def _fit(v, y_v, mask, box, iters=4):
    """在 mask 指定的像素上做 4 轮 MAD 剔除拟合。返回 (m,a,A,mad) 或 None。"""
    h, w = v.shape
    m2 = mask.copy()
    if box is not None:
        x0, y0, x1, y1 = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
        m2[max(0, y0 - dg.GROUND_EXCL_PAD):min(h, y1 + dg.GROUND_EXCL_PAD),
           max(0, x0 - dg.GROUND_EXCL_PAD):min(w, x1 + dg.GROUND_EXCL_PAD)] = False
    m2 = m2[::dg.GROUND_STRIDE, ::dg.GROUND_STRIDE]
    ys, xs = np.nonzero(m2)
    ys, xs = ys * dg.GROUND_STRIDE, xs * dg.GROUND_STRIDE
    if ys.size < dg.GROUND_MIN_PTS:
        return None
    A = np.stack([ys + 0.5 - y_v, (xs + 0.5) / w, np.ones(ys.size)], 1)
    yy = v[ys, xs].astype(np.float64)
    coef = _solve3(A, yy)
    mad = 0.0
    for _ in range(iters):
        r = A @ coef - yy
        med = np.median(r)
        mad = float(np.median(np.abs(r - med)) * 1.4826)
        keep = np.abs(r - med) < 2.5 * (mad + 1e-9)
        if keep.sum() < dg.GROUND_MIN_PTS // 2:
            break
        coef = _solve3(A[keep], yy[keep])
    return float(coef[0]), float(coef[1]), float(coef[2]), mad, A, yy, coef


def _band_mask(v):
    h, w = v.shape
    rows = np.arange(h)[:, None]
    return np.broadcast_to((rows >= dg.GROUND_ROW_LO) & (rows <= dg.GROUND_ROW_HI),
                           (h, w)).copy()


def _cols_mask(v, lo, hi):
    h, w = v.shape
    m = _band_mask(v)
    m[:, :int(w * lo)] = False
    m[:, int(w * hi):] = False
    return m


def old_model(v, y_v, box=None):
    r = _fit(v, y_v, _band_mask(v), box)
    return None if r is None else r[:4]


def new_model(v, y_v, box=None):
    """中央粗拟 → 全宽筛贴地 → 重拟；MAD 取中央列（独立于筛选）。"""
    core = _fit(v, y_v, _cols_mask(v, COLS_LO, COLS_HI), box)
    if core is None:
        return None
    m0, a0, A0, mad0 = core[:4]
    h, w = v.shape
    rows = np.arange(h)[:, None]
    u = (np.arange(w)[None, :] + 0.5) / w
    pred = A0 + m0 * (rows + 0.5 - y_v) + a0 * u
    resid = v - pred
    # 全宽贴地筛：只留粗平面解释得住的像素（护栏/人行道高出来，被剔掉）
    tau = max(dg.GROUND_TAU, REFIT_K * mad0)
    band = _band_mask(v)
    keep_mask = band & (np.abs(resid - np.median(resid[band])) <= tau)
    refit = _fit(v, y_v, keep_mask, box)
    if refit is None:
        return core[:4]
    m, a, A = refit[:3]
    # 质量门：中央列上算（中央列是物理先验路面，不参与「筛完自证」）
    cm = _cols_mask(v, COLS_LO, COLS_HI)
    cm[::dg.GROUND_STRIDE, ::dg.GROUND_STRIDE] = cm[::dg.GROUND_STRIDE, ::dg.GROUND_STRIDE]
    ys, xs = np.nonzero(cm[::dg.GROUND_STRIDE, ::dg.GROUND_STRIDE])
    ys, xs = ys * dg.GROUND_STRIDE, xs * dg.GROUND_STRIDE
    if ys.size < 200:
        return m, a, A, mad0
    rr = (A + m * (ys + 0.5 - y_v) + a * (xs + 0.5) / w) - v[ys, xs]
    med = np.median(rr)
    mad = float(np.median(np.abs(rr - med)) * 1.4826)
    return m, a, A, mad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="20261112_203641")
    ap.add_argument("--stride", type=int, default=3)
    args = ap.parse_args()

    import onnxruntime as ort
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    base = APPDATA / "MaaRacingMaster/debug" / args.session / "speedrush/drive"
    yv: dict[int, float] = {}
    for line in (APPDATA / "MaaRacingMaster/logs" / args.session / "trace/speedrush.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("type") is None:
            lo = d.get("lane_obs") or {}
            if lo.get("y_v") is not None:
                yv[int(d["fid"])] = float(lo["y_v"])
    frames = [json.loads(l) for l in (base / "frames.jsonl").open(encoding="utf-8")]

    gate = dg.GROUND_MAD_MAX
    for ph in (1, 2):
        fs = [f for f in frames if int(f.get("phase") or 0) == ph][::args.stride]
        stat = {"old_ok": 0, "new_ok": 0, "rescued": 0, "broken": 0,
                "dparam": [], "n": 0}
        print(f"\n=== phase{ph}  n={len(fs)}")
        for f in fs:
            img = cv2.imread(str(base / f["file"]))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            out = sess.run(None, {sess.get_inputs()[0].name: dg._da_preprocess(rgb)})[0][0, 0]
            v = dg._resize_align_corners(out, dg.FRAME_H, dg.FRAME_W)
            y = yv.get(int(f["fid"]), dg.FALLBACK_Y_V)
            o = old_model(v, y)
            nw = new_model(v, y)
            if o is None or nw is None:
                continue
            stat["n"] += 1
            ook, nok = o[3] <= gate, nw[3] <= gate
            stat["old_ok"] += ook
            stat["new_ok"] += nok
            stat["rescued"] += (not ook and nok)
            stat["broken"] += (ook and not nok)
            if ook:
                # 正常帧：平面参数不能漂（m 斜率/A 截距）
                stat["dparam"].append((abs(nw[0] - o[0]), abs(nw[2] - o[2])))
        n = stat["n"] or 1
        dp = np.asarray(stat["dparam"]) if stat["dparam"] else np.zeros((1, 2))
        print(f"  旧合格 {stat['old_ok']}/{n}  新合格 {stat['new_ok']}/{n}"
              f"  救回 {stat['rescued']}  误放/新坏 {stat['broken']}")
        print(f"  正常帧参数漂移（旧合格帧上）：|Δm| p50={np.median(dp[:,0]):.5f}"
              f" p95={np.percentile(dp[:,0],95):.5f} |  |ΔA| p50={np.median(dp[:,1]):.4f}"
              f" p95={np.percentile(dp[:,1],95):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
