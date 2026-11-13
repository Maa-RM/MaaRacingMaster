# -*- coding: utf-8 -*-
"""伪生产链的另一半：DepthART 自由空间/路面区域 + 障碍占用。

**链路**：slim 相对深度 → 近场地面模型 `V = A + m·(r−y_h) + a·u`（挖掉车框）
→ 逐像素「贴地」判据 `|V − V_g| ≤ τ` → 取包含自车道中心的地面连通段 = 自由空间。

**要回答**：
1. 车道线像素是否落在自由空间内（线画在路面上，应几乎全中）；
2. 自车道多边形内的地面占比（自由空间完整度）与障碍占比；
3. 撞车段出租车进入自车道时，障碍占比是否随之上升（占用信号可用性）；
4. 地面横向边界（换算成车道单位）与最外侧车道线的一致性。

输出 `freespace_out/freespace.json`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "freespace_out"
FY = 630.0
CAM_H = 1.25
YH0 = 360.5
LANE_W = 3.5
VPX = 640.7
BANDS = (15.0, 25.0, 40.0)
ROW_LO = 420
ROW_HI = 700
N_STABLE = 40


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
    ys, xs = ys * 2, xs * 2
    if len(ys) < 4000:
        return None
    u = (xs + 0.5) / w
    A = np.stack([ys - yh, u, np.ones(len(ys))], 1)
    y = v[ys, xs]
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    mad = 0.0
    for _ in range(4):
        resid = A @ coef - y
        mad = float(np.median(np.abs(resid - np.median(resid))) * 1.4826)
        keep = np.abs(resid - np.median(resid)) < 2.5 * (mad + 1e-9)
        coef = np.linalg.lstsq(A[keep], y[keep], rcond=None)[0]
    return coef, mad


def ego_pair(fits, y=620.0):
    xs = sorted((s * y + a, i) for i, (s, a, _, _) in enumerate(fits))
    for (x0, i0), (x1, i1) in zip(xs, xs[1:]):
        if x0 <= VPX <= x1 and x1 - x0 >= 80.0:
            return i0, i1
    return None


def run_edges(mask_row, center):
    """包含 center 的连续 True 段 (x_lo, x_hi)；找不到返回 None。"""
    idx = np.flatnonzero(mask_row)
    if len(idx) == 0:
        return None
    c = int(round(center))
    # 找与 center 最近的 True 像素，再向两侧扩到断点
    j = int(np.argmin(np.abs(idx - c)))
    if abs(idx[j] - c) > 40:
        return None
    lo = hi = j
    while lo > 0 and idx[lo - 1] == idx[lo] - 1:
        lo -= 1
    while hi < len(idx) - 1 and idx[hi + 1] == idx[hi] + 1:
        hi += 1
    return int(idx[lo]), int(idx[hi])


def analyze(rgb, box, lane_sess, da_sess):
    g = lg.geometry(lg.lane_prob(lane_sess, rgb))
    yh = g["y_v"] if g["y_v"] is not None else YH0
    out = da_sess.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
    v = sim.resize_ac(out.astype(np.float64), 720, 1280)
    fit = ground_fit_ex(v, yh, box)
    if fit is None:
        return {"lines": g["lines"], "yh": yh, "err": "fit"}
    coef, mad = fit
    m, a, A = float(coef[0]), float(coef[1]), float(coef[2])
    h, w = v.shape
    rr = np.arange(h, dtype=np.float64)[:, None]
    uu = (np.arange(w, dtype=np.float64)[None, :] + 0.5) / w
    vg = A + m * (rr - yh) + a * uu
    # τ 的标定（0090~0104 实测）：近场带（行 450~690）地面残差 p50 0.010~0.027、
    # p90 0.025~0.055——绝对残差不是 0.005（那是归一化口径）。取 0.05 覆盖 ~90%
    # 近场地面；目标在 <20m 处与同行地面的差约 0.15~0.45，仍可分。40m 外信号
    # 与残差同量级，故自由空间只算到行 405（约 27m）。
    tau = 0.05
    ground = (np.abs(v - vg) <= tau) & (rr >= 405) & (rr <= ROW_HI)
    # 不做框挖除：车体相对路面是明显的非地面，占用信号正是从这里来；
    # 框只在拟合地面模型时挖掉（ground_fit_ex 的 excl）。

    fits = [f for f in (lg.fit_line(p) for p in lg.tracks(lg.lane_prob(lane_sess, rgb))) if f]
    rec = {"lines": g["lines"], "yh": float(yh), "tau": float(tau), "mad": mad,
           "fit_ok": bool(mad <= 0.06),
           "m": m, "a": a, "A": A,
           "ground_frac": float(ground[ROW_LO:ROW_HI].mean())}
    if box and box[3] > yh + 2:
        bz = FY * CAM_H / (box[3] - yh)
        rec["box_z"] = bz
        rec["box_x"] = (0.5 * (box[0] + box[2]) - VPX) * bz / FY
    # 车道线像素贴地率
    hit = tot = 0
    for (s, al, _, _) in fits:
        for y in range(440, 701, 4):
            x = int(round(s * y + al))
            if 0 <= x < w:
                tot += 1
                hit += int(ground[y, x])
    rec["lane_inside_frac"] = (hit / tot) if tot else None
    # 自车道多边形的地面/障碍占比
    k = FY * CAM_H
    pair = ego_pair(fits)
    if pair:
        i0, i1 = pair
        s0, a0 = fits[i0][0], fits[i0][1]
        s1, a1 = fits[i1][0], fits[i1][1]
        ys = np.arange(int(round(yh + k / 15.0)), 701, 2)
        cnt = obs = 0
        for y in ys:
            x0 = int(round(s0 * y + a0)) + 3
            x1 = int(round(s1 * y + a1)) - 3
            if x1 <= x0:
                continue
            x0, x1 = max(0, x0), min(w - 1, x1)
            cnt += x1 - x0 + 1
            obs += int((~ground[y, x0:x1 + 1]).sum())
        rec["ego_ground_frac"] = 1.0 - obs / max(1, cnt)
        rec["ego_obs_frac"] = obs / max(1, cnt)
        # 最近障碍：只在 z 5~15m 扫（更远地面模型外推失真、448² 边界糊）。
        # 连续 4 个采样行自车道非地面占比 >0.5 视为障碍；边界行给自由空间
        # 边界距离，同处非地面像素的深度中位给目标本体距离。
        run = 0
        obs_row = None
        for y in range(int(round(yh + k / 5.0)), int(round(yh + k / 15.0)), -2):
            x0 = int(round(s0 * y + a0)) + 3
            x1 = int(round(s1 * y + a1)) - 3
            if x1 <= x0:
                break
            x0, x1 = max(0, x0), min(w - 1, x1)
            frac = float((~ground[y, x0:x1 + 1]).mean())
            run = run + 1 if frac > 0.5 else 0
            if run >= 4:
                obs_row = y
                break
        rec["obs_row"] = obs_row
        rec["obs_z"] = (k / (obs_row - yh)) if obs_row else None
        rec["obs_z_depth"] = None
        if obs_row:
            r0, r1 = max(0, obs_row - 8), obs_row + 1
            x0 = max(0, int(round(s0 * obs_row + a0)) + 3)
            x1 = min(w - 1, int(round(s1 * obs_row + a1)) - 3)
            if x1 > x0:
                cell = v[r0:r1, x0:x1 + 1]
                ng = ~ground[r0:r1, x0:x1 + 1]
                vals = cell[ng]
                if vals.size >= 20:
                    vmed = float(np.median(vals))
                    uc = 0.5 * (x0 + x1) / w
                    den = vmed - A - a * uc
                    if den > 1e-6:
                        rec["obs_z_depth"] = float(m * k / den)
    else:
        rec["ego_ground_frac"] = rec["ego_obs_frac"] = None
    # 地面横向边界（车道单位）与最外线对照
    bands = {}
    for z in BANDS:
        r = int(round(yh + k / z))
        if not (ROW_LO <= r <= ROW_HI):
            continue
        center = 0.5 * (fits[pair[0]][0] * r + fits[pair[0]][1]
                        + fits[pair[1]][0] * r + fits[pair[1]][1]) if pair else VPX
        ed = run_edges(ground[r], center)
        if ed is None:
            continue
        xl, xr = ed
        zr = k / max(1.0, r - yh)
        to_lane = lambda x: (x - VPX) * zr / FY / LANE_W
        line_x = [s * r + al for s, al, _, _ in fits]
        bands[f"z{z:.0f}"] = {
            "row": r,
            "left_lane": to_lane(xl), "right_lane": to_lane(xr),
            "line_min_lane": to_lane(min(line_x)) if line_x else None,
            "line_max_lane": to_lane(max(line_x)) if line_x else None,
        }
    rec["bands"] = bands
    return rec


def main() -> None:
    OUT.mkdir(exist_ok=True)
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    det = sim.session("prod_perception/model.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")

    # 撞车段（有跟踪框）
    d = json.loads((ROOT / "prod_sim_out" / "prod_sim.json").read_text(encoding="utf-8"))
    taxi = []
    for r in d["rows"]:
        if not r.get("box"):
            continue
        rgb = cv2.cvtColor(cv2.imread(str(ROOT / "frames" / r["file"])),
                           cv2.COLOR_BGR2RGB)
        rec = analyze(rgb, tuple(r["box"]), lane, da)
        rec.update({"file": r["file"], "moge": r["moge_z"]})
        taxi.append(rec)
    print("taxi", len(taxi))
    for r in taxi:
        print(r["file"], "yh", round(r["yh"], 1),
              "fit", r.get("fit_ok"),
              "lane_in", None if r["lane_inside_frac"] is None
              else round(r["lane_inside_frac"], 3),
              "ego_g", None if r["ego_ground_frac"] is None
              else round(r["ego_ground_frac"], 3),
              "obs_z", None if r.get("obs_z") is None else round(r["obs_z"], 1),
              "obs_zd", None if r.get("obs_z_depth") is None
              else round(r["obs_z_depth"], 1),
              "box_z", None if r.get("box_z") is None else round(r["box_z"], 1))

    # 稳定段抽样
    allf = lg.list_frames()
    idx = np.linspace(0, len(allf) - 1, min(N_STABLE, len(allf))).astype(int)
    stable = []
    for i in sorted(set(int(v) for v in idx)):
        p = allf[i]
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        dets = sim.detect_cars(det, rgb)
        box = max(dets, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if dets else None
        rec = analyze(rgb, box, lane, da)
        rec["file"] = str(p.relative_to(ROOT / "frames"))
        stable.append(rec)
    print("stable", len(stable))

    def agg(rows, key):
        v = [r[key] for r in rows if r.get(key) is not None]
        return None if not v else {"n": len(v), "med": float(np.median(v)),
                                   "p10": float(np.percentile(v, 10)),
                                   "p90": float(np.percentile(v, 90))}

    summary = {
        "taxi_lane_inside": agg(taxi, "lane_inside_frac"),
        "taxi_ego_ground": agg(taxi, "ego_ground_frac"),
        "stable_lane_inside": agg(stable, "lane_inside_frac"),
        "stable_ego_ground": agg(stable, "ego_ground_frac"),
        "taxi_tau": agg(taxi, "tau"), "stable_tau": agg(stable, "tau"),
    }
    near = [r for r in stable
            if r.get("box_z") is not None and r["box_z"] < 20.0
            and abs(r.get("box_x", 9.0)) < 1.2]
    summary["stable_near_ahead"] = {
        "n": len(near),
        "z_med": None if not near else float(np.median([r["box_z"] for r in near])),
        "ego_ground": agg(near, "ego_ground_frac"),
        "ego_obs": agg(near, "ego_obs_frac"),
        "obs_z_med": (float(np.median([r["obs_z"] for r in near if r.get("obs_z")]))
                      if any(r.get("obs_z") for r in near) else None)}
    ta = [r for r in taxi if r.get("obs_z") is not None]
    summary["taxi_obs"] = {"n": len(ta),
                           "pairs": [[r["file"], round(r["obs_z"], 1),
                                      None if r.get("obs_z_depth") is None
                                      else round(r["obs_z_depth"], 1),
                                      None if r.get("box_z") is None
                                      else round(r["box_z"], 1)] for r in ta]}
    ok = [r for r in stable if r.get("fit_ok")]
    ob = [r for r in ok if r.get("obs_z") is not None]
    summary["stable_obs"] = {
        "n_ok": len(ok), "n_obs": len(ob),
        "ego_ground_ok": agg(ok, "ego_ground_frac"),
        "obs_z": agg(ob, "obs_z"),
        "obs_pairs": [[r["file"].split("\\")[-1],
                       round(r["obs_z"], 1),
                       None if r.get("box_z") is None else round(r["box_z"], 1),
                       None if r.get("box_x") is None else round(r["box_x"], 1)]
                      for r in ob]}
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    (OUT / "freespace.json").write_text(
        json.dumps({"summary": summary, "taxi": taxi, "stable": stable},
                   indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
