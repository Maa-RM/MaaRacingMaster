# -*- coding: utf-8 -*-
"""自由空间左右边界 vs 生产 MoGe 左右缘读数（同帧、同车道单位）。

**要回答**：把 DepthART 的自由空间横向边界接到走廊之前，它与生产 MoGe 现在
输出的 ``left_edge_lane / right_edge_lane`` 差多少、是否同号同量级、弃权是否
同型。两套只做数值对照，不改任何生产代码。

- MoGe：生产 ``infer_points(native=True)`` + ``reading_from_points``（Z 分箱
  离地穿越、最近箱被次近箱确认、车道单位）。
- DepthART：slim 相对深度 → 地面模型 → 贴地判据 `|V−V_g| ≤ τ` → 逐 z 带在
  相机轴两侧向外扩，取包含轴线的连续地面段外沿 → `x = (px−cx)·z/fx` → 车道单位。
  带选择与 MoGe 同口径（最近箱 + 0.8m 邻箱确认）。

输出 `corridor_sides_out/corridor_sides.json`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).parent
OUT = ROOT / "corridor_sides_out"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[2]))
import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402
import probe_freespace_chain as fs  # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo  # noqa: E402

FY = 630.0
CAM_H = 1.25
YH0 = 360.5
LANE_W = 3.5
VPX = 640.7
TAU = 0.05
ZBIN = ((3.0, 5.0), (5.0, 7.0), (7.0, 9.0), (9.0, 12.0), (12.0, 16.0))
EDGE_MATCH_M = 0.8
N_SAMPLE = 40
DET_W = ROOT / "weights"
DA_W = "depthart/relative_mnv4m_slim_spf_448_default.onnx"
CAL = SimpleNamespace(lane_w_m=LANE_W)


def sample_frames() -> list[Path]:
    root = sorted((ROOT / "frames").glob("*_raw.jpg"))
    rest = []
    for d in sorted((ROOT / "frames").iterdir()):
        if d.is_dir():
            rest.extend(sorted(d.glob("*_raw.jpg")))
    step = max(1, len(rest) // max(1, N_SAMPLE - len(root)))
    return root + rest[::step]


def band_edges(mask_row, center, z, fx):
    """该行地面段的外沿（米，左负右正）；找不到轴线所在段返回 None。"""
    idx = np.flatnonzero(mask_row)
    if len(idx) == 0:
        return None
    c = int(round(center))
    j = int(np.argmin(np.abs(idx - c)))
    if abs(idx[j] - c) > 30:
        return None
    lo = hi = j
    while lo > 0 and idx[lo - 1] == idx[lo] - 1:
        lo -= 1
    while hi < len(idx) - 1 and idx[hi + 1] == idx[hi] + 1:
        hi += 1
    x_lo = ((idx[lo] + 0.5) - VPX) * z / fx
    x_hi = ((idx[hi] + 0.5) - VPX) * z / fx
    return float(x_lo), float(x_hi)


def pick_side(bands, side):
    smp = sorted((zc, (lo if side < 0 else hi))
                 for zc, lo, hi in bands if zc is not None)
    if not smp:
        return None
    pick = None
    for i in range(len(smp) - 1):
        if abs(smp[i][1] - smp[i + 1][1]) <= EDGE_MATCH_M:
            pick = smp[i][1]
            break
    if pick is None:
        pick = float(np.median([x for _, x in smp]))
    return pick


def depthart_sides(rgb, da_sess, yh, box):
    out = da_sess.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
    v = sim.resize_ac(out.astype(np.float64), 720, 1280)
    fit = fs.ground_fit_ex(v, yh, box)
    if fit is None:
        return {}, "fit"
    coef, mad = fit
    m, a, A = float(coef[0]), float(coef[1]), float(coef[2])
    h, w = v.shape
    rr = np.arange(h, dtype=np.float64)[:, None]
    uu = (np.arange(w, dtype=np.float64)[None, :] + 0.5) / w
    vg = A + m * (rr - yh) + a * uu
    ground = (np.abs(v - vg) <= TAU) & (rr >= 405) & (rr <= fs.ROW_HI)
    k = FY * CAM_H
    bands = []
    for z0, z1 in ZBIN:
        r0 = int(round(yh + k / z1))
        r1 = int(round(yh + k / z0))
        r0, r1 = max(405, min(r0, r1)), min(fs.ROW_HI, max(r0, r1))
        zc = (z0 + z1) / 2.0
        lo_l, hi_l = [], []
        for r in range(r0, r1 + 1, 2):
            z = k / max(1.0, r - yh)
            e = band_edges(ground[r], VPX, z, FY)
            if e is None:
                continue
            lo_l.append(e[0])
            hi_l.append(e[1])
        if len(lo_l) < 3:
            continue
        bands.append((zc, float(np.median(lo_l)), float(np.median(hi_l))))
    out = {"bands": [[round(b[0], 1), round(b[1], 2), round(b[2], 2)]
                     for b in bands], "mad": mad}
    for side, key in ((-1, "left"), (1, "right")):
        x = pick_side(bands, side)
        out[key + "_m"] = None if x is None else round(x, 2)
        out[key + "_lane"] = None if x is None else round(x / LANE_W, 2)
    return out, None


def main() -> None:
    OUT.mkdir(exist_ok=True)
    da = sim.session(DA_W)
    lane = sim.session("lane/plus_small_384x640.onnx")
    det = sim.session("prod_perception/model.onnx")
    moge = depth_geo.load_session(DET_W / "prod_moge_q4f16.onnx")
    frames = sample_frames()
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
            rd = depth_geo.reading_from_points(
                pts, fx, CAL, object_mask=None, fy=fy,
                ds=1280.0 / pts.shape[1])
            rec["moge"] = {"fx": round(fx, 1), "left": rd.left_edge_lane,
                           "right": rd.right_edge_lane, "sides": rd.sides,
                           "rejects": list(rd.rejects)}
        except Exception as e:  # noqa: BLE001
            rec["moge"] = {"err": f"{type(e).__name__}: {e}"}
        try:
            da_rec, err = depthart_sides(rgb, da, yh, box)
            rec["da"] = da_rec if not err else {"err": err}
        except Exception as e:  # noqa: BLE001
            rec["da"] = {"err": f"{type(e).__name__}: {e}"}
        recs.append(rec)
        if (i + 1) % 20 == 0:
            print(f"{i + 1}/{len(frames)}")

    pairs = []
    for r in recs:
        mg, da_r = r.get("moge", {}), r.get("da", {})
        for side in ("left", "right"):
            a, b = mg.get(side), da_r.get(side + "_lane")
            if a is not None and b is not None and not (0 <= b <= 0):
                pairs.append((side, float(a), float(b)))
    summary = {"n": len(recs), "n_pairs": len(pairs)}
    for side in ("left", "right"):
        sp = [(a, b) for s, a, b in pairs if s == side]
        if not sp:
            continue
        d = np.array([b - a for a, b in sp])
        summary[side] = {
            "n": len(sp),
            "moge_med": float(np.median([a for a, _ in sp])),
            "da_med": float(np.median([b for _, b in sp])),
            "diff_med": float(np.median(d)),
            "abs_diff_med": float(np.median(np.abs(d))),
            "abs_diff_p90": float(np.percentile(np.abs(d), 90)),
            "same_sign": float(np.mean(np.sign([a for a, _ in sp])
                                       == np.sign([b for _, b in sp]))),
        }
    summary["moge_sides"] = {
        "2": sum(1 for r in recs if r.get("moge", {}).get("sides") == 2),
        "1": sum(1 for r in recs if r.get("moge", {}).get("sides") == 1),
        "0": sum(1 for r in recs if r.get("moge", {}).get("sides") == 0),
    }
    summary["da_sides"] = {
        "2": sum(1 for r in recs if r.get("da", {}).get("left_lane") is not None
                 and r.get("da", {}).get("right_lane") is not None),
        "1": sum(1 for r in recs if (r.get("da", {}).get("left_lane") is None)
                 != (r.get("da", {}).get("right_lane") is None)),
        "0": sum(1 for r in recs if r.get("da", {}).get("left_lane") is None
                 and r.get("da", {}).get("right_lane") is None),
    }
    summary["da_fit_bad"] = sum(1 for r in recs
                                if (r.get("da", {}).get("mad") or 0) > 0.06)
    (OUT / "corridor_sides.json").write_text(
        json.dumps({"summary": summary, "frames": recs},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
