# -*- coding: utf-8 -*-
"""伪生产链的距离层：框底行几何 + 深度接触修正 + 框高校核。

**链路**：
1. 车道线 → 地平线行 `y_h`（失败退固定 361.1）；
2. 深度模型 → 近场地面模型 `V = A + m·(r−y_h) + a·u`（拟合时挖掉目标框）；
3. 框底行几何 `z_geo = fy·1.25/(y_bot − y_h)`；
4. **接触修正**：框底条带深度 `V_obj` 与同行地面 `V_g` 的差 `dV` 只依赖同帧地面
   拟合，**不需要跨帧校准偏移 A**；修正行 `r* = y_bot + dV/m`，`z_con = k/(r*−y_h)`；
5. 框高校核 `z_h = H·fy/h_px`（H 在线估）。

**要回答**：近车/侧视/框底含阴影时，框底行是否还准；深度修正能否把框底误差拉回，
且不引入新的跳变。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "contact_chain_out"
APPDATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
POOL = APPDATA / "MaaRacingMaster" / "debug"
FY = 630.0
CAM_H = 1.25
CAR_H = 1.5
YH0 = 361.12
PER_DRIVE = 16


def sample() -> list[Path]:
    out: list[Path] = []
    for d in sorted(POOL.glob("*/speedrush/drive/*")):
        fs = sorted((d / "raw").glob("*_raw.jpg"))
        if not fs:
            continue
        idx = np.linspace(0, len(fs) - 1, min(PER_DRIVE, len(fs))).astype(int)
        out.extend(fs[i] for i in sorted(set(int(v) for v in idx)))
    return out


def ground_fit_ex(v, yh, excl):
    h, w = v.shape
    rows = np.arange(h)[:, None]
    mask = np.broadcast_to((rows >= 380) & (rows <= 690), (h, w)).copy()
    if excl:
        x0, y0, x1, y1 = excl
        mask[max(0, y0 - 8):min(h, y1 + 8),
             max(0, x0 - 8):min(w, x1 + 8)] = False
    mask = mask[::2, ::2]
    ys, xs = np.nonzero(mask)
    ys = ys * 2
    xs = xs * 2
    if len(ys) < 4000:
        return None
    u = (xs + 0.5) / w
    A = np.stack([ys - yh, u, np.ones(len(ys))], 1)
    y = v[ys, xs]
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(4):
        resid = A @ coef - y
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        keep = np.abs(resid - np.median(resid)) < 2.5 * mad
        coef = np.linalg.lstsq(A[keep], y[keep], rcond=None)[0]
    return coef


def chain(rgb, box, yh, da_sess):
    out = da_sess.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
    v = sim.resize_ac(out.astype(np.float64), 720, 1280)
    coef = ground_fit_ex(v, yh, box)
    if coef is None:
        return None
    m, a, A = float(coef[0]), float(coef[1]), float(coef[2])
    vmed, u = sim.band_median(v, box)
    if vmed is None:
        return None
    yb = float(box[3])
    dV = vmed - (A + m * (yb - yh) + a * u)
    corr_px = dV / m if abs(m) > 1e-6 else 0.0
    r_star = yb + corr_px
    k = FY * CAM_H
    z_geo = k / (yb - yh) if yb > yh + 2 else None
    z_con = k / (r_star - yh) if r_star > yh + 2 else None
    hp = box[3] - box[1]
    z_h = CAR_H * FY / hp if hp >= 4 else None
    x_lat = ((u * 1280.0 - 640.7) * z_geo / FY) if z_geo else None
    lat_clip = bool(box[0] <= 2 or box[2] >= 1278)
    cls = "adjacent" if (lat_clip or (x_lat is not None and abs(x_lat) > 1.2)) else "ahead"
    return {"box": list(box), "yh": yh, "m": m, "a": a, "A": A,
            "vmed": float(vmed), "dV": float(dV), "corr_px": float(corr_px),
            "z_geo": z_geo, "z_con": z_con, "z_h": z_h,
            "w": box[2] - box[0], "h": hp,
            "aspect": (box[2] - box[0]) / max(1, hp),
            "x_lat": x_lat, "lat_clip": lat_clip, "cls": cls,
            "trunc": bool(box[3] >= 714 or box[1] <= 2)}


def summarize(rows):
    def med(v):
        v = [x for x in v if x is not None]
        return float(np.median(v)) if v else None

    buckets = {}
    for r in rows:
        a = r["aspect"]
        ab = "rear(<=1.4)" if a <= 1.4 else ("mid(1.4~1.8)" if a <= 1.8 else "side(>1.8)")
        buckets.setdefault(f"{r['cls']}|{ab}", []).append(r)
    out = {}
    for k, rs in sorted(buckets.items()):
        err_h = [abs(r["z_con"] - r["z_h"]) / r["z_h"] * 100
                 for r in rs if r["z_con"] and r["z_h"]]
        err_h0 = [abs(r["z_geo"] - r["z_h"]) / r["z_h"] * 100
                  for r in rs if r["z_geo"] and r["z_h"]]
        corr = [abs(r["corr_px"]) for r in rs]
        out[k] = {"n": len(rs),
                  "z_con_med": med([r["z_con"] for r in rs]),
                  "z_geo_med": med([r["z_geo"] for r in rs]),
                  "err_vs_h_med_before": med(err_h0),
                  "err_vs_h_med_after": med(err_h),
                  "corr_px_med": med(corr),
                  "corr_px_p90": float(np.percentile(corr, 90)),
                  "frac_corr_gt10": float(np.mean([c > 10 for c in corr]))}
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    det = sim.session("prod_perception/model.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")

    # 1) 撞出租车段（有跟踪框）
    d = json.loads((ROOT / "prod_sim_out" / "prod_sim.json").read_text(encoding="utf-8"))
    taxi = []
    for r in d["rows"]:
        if not r.get("box"):
            continue
        p = ROOT / "frames" / r["file"]
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        yh = r["yh_used"] if r["yh_used"] else YH0
        c = chain(rgb, tuple(r["box"]), yh, da)
        if c:
            c["file"] = r["file"]
            c["moge"] = r["moge_z"]
            taxi.append(c)
    print("taxi", len(taxi))
    for c in taxi:
        print(c["file"], "moge", round(c["moge"], 1) if c["moge"] else None,
              "geo", None if c["z_geo"] is None else round(c["z_geo"], 1),
              "con", None if c["z_con"] is None else round(c["z_con"], 1),
              "h", None if c["z_h"] is None else round(c["z_h"], 1),
              "corr", round(c["corr_px"], 1), "asp", round(c["aspect"], 2),
              "xlat", None if c["x_lat"] is None else round(c["x_lat"], 2),
              c["cls"])

    # 2) 历史池：每帧取最大的车框
    frames = sample()
    print("pool", len(frames))
    pool = []
    for i, p in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        dets = sim.detect_cars(det, rgb)
        if not dets:
            continue
        box = max(dets, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        if box[3] - box[1] < 40:
            continue
        g = lg.geometry(lg.lane_prob(lane, rgb))
        yh = g["y_v"] if g["y_v"] is not None else YH0
        c = chain(rgb, box, yh, da)
        if c:
            c["file"] = str(p.relative_to(POOL))
            pool.append(c)
        if (i + 1) % 80 == 0:
            print(f"{i + 1}/{len(frames)} pool={len(pool)}")

    summary = {"taxi": summarize(taxi), "pool": summarize(pool),
               "n_taxi": len(taxi), "n_pool": len(pool)}
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    (OUT / "contact_chain.json").write_text(
        json.dumps({"summary": summary, "taxi": taxi, "pool": pool},
                   indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
