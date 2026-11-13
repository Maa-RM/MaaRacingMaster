# -*- coding: utf-8 -*-
"""热点微基准：同一份数据上对比「现有实现」与「换实现方式」的耗时。

回答的问题：深度后处理慢是 Python 解释器慢，还是当前做法（fp64、全幅临时数组、
四轮 SVD 最小二乘）本身的成本？输出 micro_bench_out/micro_bench.json。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import depth_geo  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "micro_bench_out"
MODELS = ROOT / "maaracing_master" / "plugins" / "speedrush" / "resources" / "models"
N = 30


def p50(xs):
    return round(float(statistics.median(xs)), 2)


def bench(fn, n=N):
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000.0)
    return p50(xs)


def fit_lstsq(A, y, iters=4):
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(iters):
        resid = A @ coef - y
        med = np.median(resid)
        mad = float(np.median(np.abs(resid - med)) * 1.4826)
        keep = np.abs(resid - med) < 2.5 * (mad + 1e-9)
        coef = np.linalg.lstsq(A[keep], y[keep], rcond=None)[0]
    return coef


def fit_neq(A, y, iters=4):
    coef = np.linalg.solve(A.T @ A, A.T @ y)
    for _ in range(iters):
        resid = A @ coef - y
        med = np.median(resid)
        mad = float(np.median(np.abs(resid - med)) * 1.4826)
        keep = np.abs(resid - med) < 2.5 * (mad + 1e-9)
        Aw, yw = A[keep], y[keep]
        coef = np.linalg.solve(Aw.T @ Aw, Aw.T @ yw)
    return coef


def synth32(v, y_v, gm):
    m, a, A = gm[0], gm[1], gm[2]
    rows = np.arange(0, v.shape[0], 2)
    cols = np.arange(0, v.shape[1], 2)
    vv = v[rows][:, cols]
    rc = (rows[:, None] + 0.5).astype(np.float32)
    uu = ((cols[None, :] + 0.5) / v.shape[1]).astype(np.float32)
    vg = A + m * (rc - y_v) + a * uu
    k = np.float32(depth_geo.FOCAL_PX * depth_geo.CAM_H_M)
    zf = rc - y_v
    zg = k / np.maximum(np.float32(1.0), zf)
    ground = np.abs(vv - vg) <= depth_geo.GROUND_TAU
    den = vv - A - a * uu
    zo = np.where(np.abs(den) < 1e-6, np.nan, m * k / den)
    Z = np.where(ground, zg, zo)
    valid = (zf > 2.0) & np.isfinite(Z) & (Z > depth_geo.POINT_Z_MIN) & (Z < depth_geo.POINT_Z_MAX)
    if m <= 0:
        valid &= ground
    X = (cols[None, :] + 0.5 - depth_geo.PT_CX) * Z / depth_geo.FOCAL_PX
    Y = zf * Z / depth_geo.FOCAL_PX
    pts = np.stack([X, Y, Z], -1).astype(np.float32)
    pts[~valid] = np.nan


def main():
    from maaracing_master.plugins.speedrush import depth_geo as dg
    sess = dg.load_session(
        MODELS / "depth" / "relative_mnv4m_slim_spf_448_default.onnx")
    frame = cv2.imread(str(sorted((HERE / "frames" /
        "20261109_201359_p1_stable41").glob("*_raw.jpg"))[0]))
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    raw = sess.run(None, {sess.get_inputs()[0].name: dg._da_preprocess(rgb)})[0][0, 0]
    raw = np.asarray(raw, np.float32)
    v64 = dg._resize_align_corners(raw.astype(np.float64), 720, 1280)
    v32 = dg._resize_align_corners(raw.astype(np.float32), 720, 1280)
    gm = dg._ground_model(v64, 354.2, None)

    out = {"resize": {
        "current_fp64": bench(lambda: dg._resize_align_corners(
            raw.astype(np.float64), 720, 1280)),
        "current_fp32": bench(lambda: dg._resize_align_corners(
            raw.astype(np.float32), 720, 1280)),
        "cv2_fp32_linear": bench(lambda: cv2.resize(
            raw, (1280, 720), interpolation=cv2.INTER_LINEAR)),
    }}

    h, w = v64.shape
    rows = np.arange(h)[:, None]
    mask = np.broadcast_to(
        (rows >= dg.GROUND_ROW_LO) & (rows <= dg.GROUND_ROW_HI), (h, w)).copy()
    m2 = mask[::2, ::2]
    ys, xs = np.nonzero(m2)
    ys, xs = ys * 2, xs * 2
    A2 = np.stack([ys + 0.5 - 354.2, (xs + 0.5) / w, np.ones(ys.size)], 1)
    y2 = v64[ys, xs]
    m4 = mask[::4, ::4]
    ys4, xs4 = np.nonzero(m4)
    ys4, xs4 = ys4 * 4, xs4 * 4
    A4 = np.stack([ys4 + 0.5 - 354.2, (xs4 + 0.5) / w, np.ones(ys4.size)], 1)
    y4 = v64[ys4, xs4]
    out["ground_fit"] = {
        "points_stride2": int(ys.size),
        "points_stride4": int(ys4.size),
        "lstsq_stride2": bench(lambda: fit_lstsq(A2, y2)),
        "lstsq_stride4": bench(lambda: fit_lstsq(A4, y4)),
        "normal_eq_stride2": bench(lambda: fit_neq(A2, y2)),
        "normal_eq_stride4": bench(lambda: fit_neq(A4, y4)),
        "current_ground_model": bench(lambda: dg._ground_model(v64, 354.2, None)),
    }

    gmf = tuple(float(x) for x in gm[:3])
    out["synth"] = {
        "current": bench(lambda: dg._synth_points(v64, 354.2, gmf)),
        "float32_merged": bench(lambda: synth32(v32, 354.2, gmf)),
    }

    pts = dg._synth_points(v64, 354.2, gmf)[0]
    out["grid"] = {"current_grid": bench(lambda: dg.drivable_grid_from_points(
        pts, 630.0, 630.0, None, coef=None))}

    # —— 语义对照：改造前后是否等价 ——
    def old_resize(img, h, w):
        sh, sw = img.shape[:2]
        ys = np.linspace(0, sh - 1, h)
        xs = np.linspace(0, sw - 1, w)
        y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
        y1, x1 = np.minimum(y0 + 1, sh - 1), np.minimum(x0 + 1, sw - 1)
        wy, wx = (ys - y0)[:, None], (xs - x0)[None, :]
        a = img[y0][:, x0] * (1 - wx) + img[y0][:, x1] * wx
        b = img[y1][:, x0] * (1 - wx) + img[y1][:, x1] * wx
        return a * (1 - wy) + b * wy

    def old_ground(v, y_v, box):
        h, w = v.shape
        rows = np.arange(h)[:, None]
        mask = np.broadcast_to(
            (rows >= dg.GROUND_ROW_LO) & (rows <= dg.GROUND_ROW_HI),
            (h, w)).copy()
        mask = mask[::2, ::2]
        ys, xs = np.nonzero(mask)
        ys, xs = ys * 2, xs * 2
        A = np.stack([ys + 0.5 - y_v, (xs + 0.5) / w, np.ones(ys.size)], 1)
        y = v[ys, xs]
        return fit_lstsq(A, y)

    new_v = dg._resize_align_corners(raw, 720, 1280)
    old_v = old_resize(raw.astype(np.float64), 720, 1280)
    new_gm = dg._ground_model(new_v, 354.2, None)
    old_gm = old_ground(old_v, 354.2, None)
    out["semantics"] = {
        "resize_max_abs_diff": float(np.abs(new_v - old_v).max()),
        "ground_coef_abs_diff": [float(abs(a - b)) for a, b in
                                 zip(new_gm[:3], old_gm[:3])],
    }

    OUT.mkdir(exist_ok=True)
    (OUT / "micro_bench.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for group, d in out.items():
        if group.startswith("_"):
            continue
        print(group)
        for k, v in d.items():
            print(f"  {k:<22} {v}")


if __name__ == "__main__":
    main()
