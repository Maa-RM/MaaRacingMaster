# -*- coding: utf-8 -*-
"""合成点图验证：车道线几何 + DepthART 相对深度 → 点图 → **生产消费方原样跑**。

**要回答**：能否只替换「点云生产者」而不动消费方。合成口径：

- 逐像素判「是否贴地」`|V − V_g| ≤ τ`（V_g 为当帧拟合地面）；
- 贴地像素的米制距离由**车道线几何**给：`Z = fy·cam_h/(r − y_v)`（与深度模型无关）；
- 地面以上像素用相对深度：`Z = m·k/(V − A − a·u)`，`k = fy·cam_h`；
- 反投影 `X = (c − cx)·Z/fx`、`Y = (r − y_v)·Z/fy`（cy 取灭点行，使平路 Y ≡ cam_h）。

然后把点图喂给生产 ``reading_from_points``（平面自拟合、近箱离地穿越、最近一致
边界全部原样），与同一帧 MoGe 点云的读数对照。

**这不是生产改动**，只为判断「只换生产者」是否可行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).parent
OUT = ROOT / "synth_cloud_out"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[2]))
import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402
import probe_freespace_chain as fs  # noqa: E402
import probe_corridor_sides as cs  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo  # noqa: E402

FY = 630.0
CAM_H = 1.25
YH0 = 360.5
CX = 640.5
TAU = 0.05
LANE_W = 3.5
CAL = SimpleNamespace(lane_w_m=LANE_W)
W, H = 1280, 720


def synth_pts(rgb, da, yh, box):
    out = da.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
    v = sim.resize_ac(out.astype(np.float64), H, W)
    fit = fs.ground_fit_ex(v, yh, box)
    if fit is None:
        return None, "地面拟合失败", None
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
    valid = ((rr > yh + 2) & np.isfinite(Z) & (Z > 1.2) & (Z < 220.0))
    if m <= 0:
        valid &= ground
    X = (uu * W + 0.5 - CX) * Z / FY
    Y = (rr + 0.5 - yh) * Z / FY
    pts = np.stack([X, Y, Z], -1).astype(np.float32)
    pts[~valid] = np.nan
    return pts, None, {"m": m, "a": a, "A": A, "mad": mad,
                       "ground_frac": float(ground[valid].mean())}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")
    det = sim.session("prod_perception/model.onnx")
    moge = depth_geo.load_session(cs.DET_W / "prod_moge_q4f16.onnx")
    frames = cs.sample_frames()
    print("sample", len(frames))
    recs = []
    for i, p in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        g = lg.geometry(lg.lane_prob(lane, rgb))
        yh = g["y_v"] if g["y_v"] is not None else YH0
        dets = sim.detect_cars(det, rgb)
        box = max(dets, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if dets else None
        rec = {"file": str(p.relative_to(ROOT)), "yh": round(yh, 1)}
        try:
            pts, fx, fy = depth_geo.infer_points(moge, rgb, native=True)
            rd = depth_geo.reading_from_points(pts, fx, CAL, fy=fy,
                                               ds=1280.0 / pts.shape[1])
            rec["moge"] = {"left": rd.left_edge_lane, "right": rd.right_edge_lane,
                           "sides": rd.sides, "rejects": list(rd.rejects),
                           "coef": None if rd.coef is None
                           else [round(x, 4) for x in rd.coef]}
        except Exception as e:  # noqa: BLE001
            rec["moge"] = {"err": f"{type(e).__name__}: {e}"}
        try:
            sp, err, meta = synth_pts(rgb, da, yh, box)
            rec["synth_meta"] = meta
            if sp is None:
                rec["synth"] = {"err": err}
            else:
                rd = depth_geo.reading_from_points(sp, FY, CAL, fy=FY, ds=1.0)
                rec["synth"] = {"left": rd.left_edge_lane,
                                "right": rd.right_edge_lane,
                                "sides": rd.sides, "rejects": list(rd.rejects),
                                "degraded": bool(rd.degraded),
                                "coef": None if rd.coef is None
                                else [round(x, 4) for x in rd.coef]}
        except Exception as e:  # noqa: BLE001
            rec["synth"] = {"err": f"{type(e).__name__}: {e}"}
        recs.append(rec)
        if (i + 1) % 20 == 0:
            print(f"{i + 1}/{len(frames)}")

    summary = {"n": len(recs)}
    for key in ("moge", "synth"):
        summary[key + "_sides"] = {
            "2": sum(1 for r in recs if r.get(key, {}).get("sides") == 2),
            "1": sum(1 for r in recs if r.get(key, {}).get("sides") == 1),
            "0": sum(1 for r in recs if r.get(key, {}).get("sides") == 0),
        }
        summary[key + "_err"] = sum(1 for r in recs if r.get(key, {}).get("err"))
        summary[key + "_rejects"] = {}
        for r in recs:
            for rj in r.get(key, {}).get("rejects") or ():
                summary[key + "_rejects"][rj] = summary[key + "_rejects"].get(rj, 0) + 1
    for side in ("left", "right"):
        sp = [(float(r["moge"][side]), float(r["synth"][side]))
              for r in recs
              if r.get("moge", {}).get(side) is not None
              and r.get("synth", {}).get(side) is not None]
        if not sp:
            continue
        d = np.array([b - a for a, b in sp])
        summary[side] = {
            "n": len(sp),
            "moge_med": float(np.median([a for a, _ in sp])),
            "synth_med": float(np.median([b for _, b in sp])),
            "diff_med": float(np.median(d)),
            "abs_diff_med": float(np.median(np.abs(d))),
            "abs_diff_p90": float(np.percentile(np.abs(d), 90)),
            "same_sign": float(np.mean(np.sign([a for a, _ in sp])
                                       == np.sign([b for _, b in sp]))),
        }
    (OUT / "synth_cloud.json").write_text(
        json.dumps({"summary": summary, "frames": recs},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
