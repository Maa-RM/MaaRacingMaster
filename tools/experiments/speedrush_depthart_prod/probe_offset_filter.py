# -*- coding: utf-8 -*-
"""偏移 A 的稳健化：车道约束路面掩码 + 固定比例 + 时间中位。

**背景**：slim 的比例 `m` 稳定（撞车段 IQR 2.7%），但偏移 `A` 逐帧漂 0.19~0.46，
是 20~25% 距离抖动的主源。本探针只改 `A` 的估计，不改模型/尺度：

1. 路面掩码：车道线夹出的自车道区域（行 480~690），剔除车辆检测框与线漆邻域；
2. 比例 `m` 固定为干净段中位，只解 `A`（和横向项 `a`）；
3. 时间中位：3 帧窗口（居中/因果各一版）；
4. 对照：现行全带鲁棒拟合、无平滑。

焦距用相机配置解出的保险杠竖轴中间值：`fy=630`（`k=787.5`）；同时列 `fy=670` 作连续对照。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "offset_filter_out"
CAM_H = 1.25
CAR_H = 1.5
YH_FIXED = 361.12
FOCALS = (630.0, 670.7)
M0 = 0.015367
ROWS = (480, 690)


def lane_bounds(rgb, lane_sess):
    prob = lg.lane_prob(lane_sess, rgb)
    fits = [f for f in (lg.fit_line(q) for q in lg.tracks(prob)) if f]
    left = right = None
    for s, a, rms, n in fits:
        x = s * 600 + a
        if x < 640:
            left = max(left, x) if left is not None else x
        else:
            right = min(right, x) if right is not None else x
    return left, right, fits


def road_mask(shape, boxes, left, right, rows=None):
    h, w = shape
    r0, r1 = rows or ROWS
    m = np.zeros((h, w), np.uint8)
    x0 = int(left + 18) if left is not None else 200
    x1 = int(right - 18) if right is not None else 1080
    x0, x1 = max(0, x0), min(w, x1)
    if x1 - x0 < 120:
        x0, x1 = 200, 1080
    m[r0:r1, x0:x1] = 1
    for b in boxes:
        bx0, by0, bx1, by1 = b
        cv2.rectangle(m, (max(0, bx0 - 10), max(0, by0 - 10)),
                      (min(w - 1, bx1 + 10), min(h - 1, by1 + 10)), 0, -1)
    return m.astype(bool)


def fit_A(v, yh, mask, m=M0):
    ys, xs = np.nonzero(mask)
    if len(ys) < 2000:
        return None
    u = (xs + 0.5) / v.shape[1]
    target = v[ys, xs] - m * (ys - yh)
    A = np.stack([u, np.ones(len(ys))], 1)
    coef = np.linalg.lstsq(A, target, rcond=None)[0]
    for _ in range(4):
        resid = A @ coef - target
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        keep = np.abs(resid - np.median(resid)) < 2.5 * mad
        coef = np.linalg.lstsq(A[keep], target[keep], rcond=None)[0]
    return float(coef[0]), float(coef[1])   # a, A


def main() -> None:
    OUT.mkdir(exist_ok=True)
    d = json.loads((ROOT / "prod_sim_out" / "prod_sim.json").read_text(encoding="utf-8"))
    rows = [r for r in d["rows"] if r.get("box") and r.get("moge_z")]
    boxes = {r["file"]: r["box"] for r in rows}
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    det = sim.session("prod_perception/model.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")

    recs = []
    for r in rows:
        p = ROOT / "frames" / r["file"]
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        out = da.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
        v = sim.resize_ac(out.astype(np.float64), 720, 1280)
        yh = r["yh_used"]
        left, right, _ = lane_bounds(rgb, lane)
        dets = sim.detect_cars(det, rgb)
        mask = road_mask(v.shape, dets, left, right)
        fit = fit_A(v, yh, mask)
        box = boxes[r["file"]]
        y1p = int(box[3])
        fit_l = fit_A(v, yh, road_mask(v.shape, dets, left, right,
                                       rows=(max(0, y1p - 26), min(720, y1p + 18))))
        vmed, unorm = sim.band_median(v, box)
        g = r["da_ground"]
        rec = {"file": r["file"], "moge": r["moge_z"], "vmed": vmed, "u": unorm,
               "A_baseline": g["A"], "a_baseline": g["a"], "m_baseline": g["m"],
               "box_w448": (box[2] - box[0]) * 448.0 / 1280.0,
               "z_geom_630": 630.0 * CAM_H / (y1p - yh) if y1p > yh + 2 else None,
               "z_geomfix_630": (630.0 * CAM_H / (y1p - YH_FIXED)
                                 if y1p > YH_FIXED + 2 else None),
               "z_h_630": (CAR_H * 630.0 / (box[3] - box[1])
                           if box[3] - box[1] >= 4 else None),
               "n_mask": int(mask.sum())}
        if fit is not None and vmed is not None:
            rec["a_mask"], rec["A_mask"] = fit
        if fit_l is not None and vmed is not None:
            rec["a_local"], rec["A_local"] = fit_l
        recs.append(rec)

    def smooth(key, win=3):
        vals = [x.get(key) for x in recs]
        out = []
        for i in range(len(vals)):
            lo, hi = max(0, i - win // 2), min(len(vals), i + win // 2 + 1)
            vv = [vals[j] for j in range(lo, hi) if vals[j] is not None]
            out.append(float(np.median(vv)) if vv else None)
        return out

    recs_A = smooth("A_mask")
    recs_a = smooth("a_mask")
    recs_Al = smooth("A_local")
    recs_al = smooth("a_local")

    def z_of(x, A, a, fy):
        if x["vmed"] is None or A is None:
            return None
        den = x["vmed"] - A - (a or 0.0) * x["u"]
        return (M0 * fy * CAM_H / den) if den > 1e-6 else None

    for x, A, a, Al, al in zip(recs, recs_A, recs_a, recs_Al, recs_al):
        for fy in FOCALS:
            x[f"z_base_{int(fy)}"] = z_of(x, x["A_baseline"], x["a_baseline"], fy)
            x[f"z_mask_{int(fy)}"] = z_of(x, x.get("A_mask"), x.get("a_mask"), fy)
            x[f"z_smooth_{int(fy)}"] = z_of(x, A, a, fy)
            x[f"z_local_{int(fy)}"] = z_of(x, x.get("A_local"), x.get("a_local"), fy)
            x[f"z_localsm_{int(fy)}"] = z_of(x, Al, al, fy)

    def stats(key):
        zs = [x.get(key) for x in recs if x.get(key)]
        if len(zs) < 3:
            return None
        errs = [abs(x[key] - x["moge"]) / x["moge"] * 100
                for x in recs if x.get(key) and x.get("moge")]
        jumps = [abs(zs[i + 1] - zs[i]) for i in range(len(zs) - 1)]
        return {"n": len(zs), "err_median_pct": float(np.median(errs)),
                "err_p90_pct": float(np.percentile(errs, 90)),
                "jump_median_m": float(np.median(jumps)),
                "jump_p90_m": float(np.percentile(jumps, 90))}

    summary = {k: stats(k) for k in
               ("z_base_630", "z_mask_630", "z_smooth_630",
                "z_local_630", "z_localsm_630", "z_geom_630",
                "z_geomfix_630", "z_h_630",
                "z_base_670", "z_mask_670", "z_smooth_670",
                "z_local_670", "z_localsm_670")}
    for x in recs:
        print(x["file"], "moge", round(x["moge"], 1),
              "w448", round(x["box_w448"], 1),
              "geom", None if x["z_geom_630"] is None else round(x["z_geom_630"], 1),
              "gfix", None if x["z_geomfix_630"] is None else round(x["z_geomfix_630"], 1),
              "hbox", None if x["z_h_630"] is None else round(x["z_h_630"], 1),
              "base", None if x["z_base_630"] is None else round(x["z_base_630"], 1),
              "mask", None if x["z_mask_630"] is None else round(x["z_mask_630"], 1),
              "sm", None if x["z_smooth_630"] is None else round(x["z_smooth_630"], 1),
              "loc", None if x["z_local_630"] is None else round(x["z_local_630"], 1),
              "| A", round(x["A_baseline"], 3),
              "Am", None if x.get("A_mask") is None else round(x["A_mask"], 3),
              "Al", None if x.get("A_local") is None else round(x["A_local"], 3))
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    (OUT / "offset_filter.json").write_text(
        json.dumps({"summary": summary, "rows": recs}, indent=1, ensure_ascii=False),
        encoding="utf-8")


if __name__ == "__main__":
    main()
