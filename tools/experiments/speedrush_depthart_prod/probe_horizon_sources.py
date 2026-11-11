# -*- coding: utf-8 -*-
"""地平线来源对比：车道线逐帧灭点行 vs 标定常数（回答「深度能不能给地平线」）。

**要回答**：
1. 相对深度是视差向仿射不变量，地面模型 `V = A + m·(r−y_h) + a·u` 里 y_h 与 A
   共线——扫 y_h 重拟合，残差是否恒定（恒定 = 深度本身不可辨识地平线）。
2. 在同一批帧上分别用「车道逐帧 y_v」「gate0 固定 354.2」「全池中位常数」生成
   合成点图，喂生产 `reading_from_points`，读数差多少、平面自拟合 c 差多少。

输出 `horizon_sources_out/horizon_sources.json`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).parent
OUT = ROOT / "horizon_sources_out"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[2]))
import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402
import probe_freespace_chain as fs  # noqa: E402
import probe_corridor_sides as cs  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo  # noqa: E402

FY = 630.0
CAM_H = 1.25
CX = 640.5
TAU = 0.05
YH_GATE0 = 354.2
YH_FALLBACK = 360.5
CAL = SimpleNamespace(lane_w_m=3.5)
W, H = 1280, 720


def fit_ground(v, yh, excl):
    """与 fs.ground_fit_ex 同口径，但返回残差 RMS（可辨识性用）。"""
    h, w = v.shape
    rows = np.arange(h)[:, None]
    mask = np.broadcast_to((rows >= 380) & (rows <= 690), (h, w)).copy()
    if excl:
        x0, y0, x1, y1 = excl
        mask[max(0, y0 - 8):min(h, y1 + 8),
             max(0, x0 - 8):min(w, x1 + 8)] = False
    mask = mask[::2, ::2]
    ys, xs = np.nonzero(mask)
    ys, xs = ys * 2, xs * 2
    if len(ys) < 4000:
        return None
    u = (xs + 0.5) / w
    Areg = np.stack([ys - yh, u, np.ones(len(ys))], 1)
    y = v[ys, xs]
    coef = np.linalg.lstsq(Areg, y, rcond=None)[0]
    mad = 0.0
    for _ in range(4):
        resid = Areg @ coef - y
        mad = float(np.median(np.abs(resid - np.median(resid))) * 1.4826)
        keep = np.abs(resid - np.median(resid)) < 2.5 * (mad + 1e-9)
        coef = np.linalg.lstsq(Areg[keep], y[keep], rcond=None)[0]
    resid = Areg @ coef - y
    return coef, float(np.sqrt(np.mean(resid ** 2))), mad


def synth_from_v(v, yh, box):
    fit = fs.ground_fit_ex(v, yh, box)
    if fit is None:
        return None, "fit"
    coef, mad = fit
    m, a, A = float(coef[0]), float(coef[1]), float(coef[2])
    rr = np.arange(H, dtype=np.float64)[:, None]
    uu = (np.arange(W, dtype=np.float64)[None, :] + 0.5) / W
    vg = A + m * (rr - yh) + a * uu
    k = FY * CAM_H
    zg = k / np.maximum(1.0, rr - yh)
    ground = np.abs(v - vg) <= TAU
    den = v - A - a * uu
    with np.errstate(divide="ignore", invalid="ignore"):
        zo = m * k / np.where(np.abs(den) < 1e-6, np.nan, den)
    Z = np.where(ground, zg, zo)
    valid = (rr > yh + 2) & np.isfinite(Z) & (Z > 1.2) & (Z < 220.0)
    if m <= 0:
        valid &= ground
    X = (np.arange(W, dtype=np.float64)[None, :] + 0.5 - CX) * Z / FY
    Y = (rr + 0.5 - yh) * Z / FY
    pts = np.stack([X, Y, Z], -1).astype(np.float32)
    pts[~valid] = np.nan
    return pts, None, {"m": m, "a": a, "A": A, "mad": mad,
                       "ground_frac": float(ground[valid].mean())}


def read(pts, ds=1.0):
    rd = depth_geo.reading_from_points(pts, FY, CAL, fy=FY, ds=ds)
    return {"left": rd.left_edge_lane, "right": rd.right_edge_lane,
            "sides": rd.sides, "rejects": list(rd.rejects),
            "coef": None if rd.coef is None
            else [round(float(x), 4) for x in rd.coef]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")
    det = sim.session("prod_perception/model.onnx")
    frames = cs.sample_frames()
    print("sample", len(frames))
    recs = []
    yvs = []
    for i, p in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        g = lg.geometry(lg.lane_prob(lane, rgb))
        yv = g["y_v"]
        dets = sim.detect_cars(det, rgb)
        box = max(dets, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if dets else None
        out = da.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
        v = sim.resize_ac(out.astype(np.float64), H, W)
        rec = {"file": str(p.relative_to(ROOT)), "lane_yv": yv,
               "lane_cam_h": g["cam_h"], "lines": g["lines"],
               "fit_mad": {}, "read": {}}
        if yv is not None:
            yvs.append(float(yv))
        sources = {"lane": (yv if yv is not None else YH_FALLBACK),
                   "gate0": YH_GATE0,
                   "const361": 361.0}
        for name, yh in sources.items():
            pts, err, meta = synth_from_v(v, yh, box)
            if pts is None:
                rec["read"][name] = {"err": err}
                rec["fit_mad"][name] = None
                continue
            rec["fit_mad"][name] = round(meta["mad"], 4)
            rec["read"][name] = read(pts)
            if name == "lane":
                # 半幅（每 2 像素取 1）与全幅的读数差：生产按半幅生成时是否可接受
                rec["read"]["lane_half"] = read(pts[::2, ::2], ds=2.0)
        recs.append(rec)
        if (i + 1) % 10 == 0:
            print(f"{i + 1}/{len(frames)}")

    # 可辨识性：前 5 个有框帧扫 yh
    sweep = []
    for p in frames[:5]:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        dets = sim.detect_cars(det, rgb)
        box = max(dets, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if dets else None
        out = da.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
        v = sim.resize_ac(out.astype(np.float64), H, W)
        curve = []
        for yh in range(340, 391, 2):
            fit = fit_ground(v, float(yh), box)
            curve.append(None if fit is None else
                         {"yh": yh, "rms": round(fit[1], 6),
                          "mad": round(fit[2], 4),
                          "m": round(float(fit[0][0]), 6)})
        sweep.append({"file": str(p.name), "curve": curve})

    def _stats(name):
        xs = [r for r in recs if r["read"].get(name, {}).get("left") is not None
              or r["read"].get(name, {}).get("right") is not None]
        return {"n": len(xs),
                "sides2": sum(1 for r in xs if r["read"][name].get("sides") == 2),
                "fit": sum(1 for r in xs if r["read"][name].get("coef") is not None)}

    def _pair(a, b):
        dif = {"left": [], "right": []}
        dc = []
        for r in recs:
            ra, rb = r["read"].get(a, {}), r["read"].get(b, {})
            for side in ("left", "right"):
                if ra.get(side) is not None and rb.get(side) is not None:
                    dif[side].append(abs(float(ra[side]) - float(rb[side])))
            if ra.get("coef") and rb.get("coef"):
                dc.append(abs(ra["coef"][2] - rb["coef"][2]))
        out = {}
        for side, arr in dif.items():
            out[side] = {"n": len(arr),
                         "p50": None if not arr else round(float(np.median(arr)), 3),
                         "p90": None if not arr else round(float(np.percentile(arr, 90)), 3)}
        out["coef_c"] = {"n": len(dc),
                         "p50": None if not dc else round(float(np.median(dc)), 4),
                         "max": None if not dc else round(float(max(dc)), 4)}
        return out

    summary = {
        "n_frames": len(recs),
        "lane_yv": {"n": len(yvs),
                    "median": None if not yvs else round(float(np.median(yvs)), 2),
                    "p25": None if not yvs else round(float(np.percentile(yvs, 25)), 2),
                    "p75": None if not yvs else round(float(np.percentile(yvs, 75)), 2)},
        "coverage": {name: _stats(name)
                     for name in ("lane", "gate0", "const361", "lane_half")},
        "diff_vs_lane": {name: _pair("lane", name)
                         for name in ("gate0", "const361", "lane_half")},
        "identifiability_sweep": sweep,
    }
    (OUT / "horizon_sources.json").write_text(
        json.dumps({"summary": summary, "frames": recs}, indent=1,
                   ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
