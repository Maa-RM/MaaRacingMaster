# -*- coding: utf-8 -*-
"""人工局极限超车事件：按「预测过车时刻」选目标，量内侧边表面间隙。

**目标选择**：横幅只给事件时刻，不给目标。这里在事件前 2.5 s 内跟踪所有车，
对每条轨道用最后几个纵深点线性外推「掉到身后」（z=0）的时刻，选与横幅时刻
最接近的那条（容差 0.8 s：横幅 0.5 s 采样 + 过车平面偏移）。

**横向量法**：取目标框朝自车那一侧的边（外侧被裁不影响），用 A1 2D 尺子
`x_lane=(x−vpx)/(row−y_h)·a_x` 换算米，减自车半宽 0.95 m = 表面间隙。
极限带按规则为 `0 < gap ≤ 0.8 m`。

输出 `overtake_gap_out/summary.json` 与每个事件一张叠加图。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_overtake_scene as S  # noqa: E402

P, ND, sim, lg = S.P, S.ND, S.sim, S.lg
OUT = Path(__file__).parent / "overtake_gap_out"
SESSIONS = ["20261113_203839_manual_p1", "20261113_203839_manual_p2",
            "20261113_204052_manual_p1", "20261113_204052_manual_p2"]
WINDOW_S = 2.5
CROSS_TOL_S = 0.8


def frames_of(sess: Path):
    with open(sess / "frames.jsonl", encoding="utf-8") as fh:
        recs = [json.loads(ln) for ln in fh if ln.strip()]
    recs.sort(key=lambda r: r["ts_ns"])
    return recs


def pick_target(event_ts: int, recs: list[dict], det, lane):
    lo = event_ts - WINDOW_S * 1e9
    frames = [r for r in recs if lo <= r["ts_ns"] <= event_ts + 0.2e9]
    if len(frames) < 5:
        return None
    boxes_by, yh_by, ts_by, img_by, prob_by = {}, {}, {}, {}, {}
    for i, r in enumerate(frames):
        img = cv2.imread(str(r["_dir"] / r["file"]))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        prob = lg.lane_prob(lane, rgb)
        fits = [f for f in (lg.fit_line(t) for t in lg.tracks(prob)) if f]
        yv = lg.vanish_row(fits) if len(fits) >= 2 else None
        yh = yv if yv is not None else 361.0
        keep = []
        for b in ND.detect_cars_full(det, rgb):
            z = P.geom_z(b, yh)
            if z is None or not (0.5 < z < 20.0):
                continue
            if not (0.6 < abs(P.cam_x(b, z)) < 8.0):
                continue
            keep.append(b)
        if keep:
            boxes_by[i] = keep
            yh_by[i] = yh
            ts_by[i] = r["ts_ns"] / 1e9
            img_by[i] = img
            prob_by[i] = prob
    if not boxes_by:
        return None
    tracks = P.mine_tracks(boxes_by)
    event_s = event_ts / 1e9
    cands = []
    for tr in tracks:
        pts = []
        for i, b in tr["boxes"]:
            if i not in yh_by:
                continue
            z = P.geom_z(b, yh_by[i])
            if z is not None:
                pts.append((ts_by[i], z, i, b))
        if len(pts) < 3:
            continue
        pts.sort(key=lambda p: p[0])
        # 被超车必须已在身后：事件后仍可见且在前方的不是目标
        if pts[-1][0] > event_s + 0.25 or pts[-1][1] > 12.0:
            continue
        lin = [p for p in pts if 3.0 < p[1] < 20.0] or pts
        use = lin[-5:]
        dt = use[-1][0] - use[0][0]
        if dt <= 0.05:
            continue
        v = (use[0][1] - use[-1][1]) / dt
        if v <= 0.5:
            continue
        t_cross = pts[-1][0] + pts[-1][1] / v
        err = abs(t_cross - event_s)
        if err > CROSS_TOL_S:
            continue
        tm = track_metrics(pts)
        if len(tm) < 3:
            continue
        use_tm = tm[-8:]
        med_gap = float(np.median([m["gap"] for m in use_tm]))
        last = use_tm[-1]
        x_cam = P.cam_x(last["box"], last["z"])
        if not (1.0 < abs(x_cam) < 6.0):
            continue
        cands.append({"err": err, "t_cross": t_cross, "v": v,
                      "points": pts, "track": tr, "pick": last,
                      "gap": med_gap, "center2d": last["center"],
                      "x_cam": x_cam})
    if not cands:
        return None
    band = [c for c in cands if 0.0 <= c["gap"] <= 1.0]
    best = (min(band, key=lambda c: c["gap"]) if band
            else min(cands, key=lambda c: c["err"]))
    best["frames"] = frames
    return best


def track_metrics(pts):
    """轨道末段每帧的内侧边间隙（内侧边未贴边、纵深可用）。"""
    cal = S.load_calib()
    out = []
    for ts, z, i, b in pts:
        cx = 0.5 * (b[0] + b[2])
        inner = b[2] if cx < cal.vpx else b[0]
        if inner >= 1276 or inner <= 3 or z < 1.2:
            continue
        gap, center, _, _ = S.lateral_metrics(b, cal)
        out.append({"ts": ts, "z": z, "i": i, "box": b,
                    "gap": gap, "center": center})
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    det = sim.session("prod_perception/model.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")
    records = []
    for name in SESSIONS:
        sess = S.DEBUG / name
        if not (sess / "hud" / "hud.jsonl").exists():
            print("skip", name)
            continue
        (OUT / name).mkdir(parents=True, exist_ok=True)
        (S.OUT / name).mkdir(parents=True, exist_ok=True)
        recs = frames_of(sess)
        for r in recs:
            r["_dir"] = sess
        evs = S.banner_events(sess)
        print(name, "events", len(evs), flush=True)
        for ts in evs:
            best = pick_target(ts, recs, det, lane)
            if not best:
                print("  no track", ts)
                continue
            if best["err"] > CROSS_TOL_S:
                print(f"  no match err={best['err']:.2f}s", ts)
                continue
            pick = best["pick"]
            i, box, z = pick["i"], pick["box"], pick["z"]
            r = best["frames"][i]
            img = cv2.imread(str(sess / r["file"]))
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            prob = lg.lane_prob(lane, rgb)
            gap, center2d, _, _ = S.lateral_metrics(box)
            b = {"img": img, "prob": prob, "box": box, "z": z,
                 "x": P.cam_x(box, z), "gap": (ts - r["ts_ns"]) / 1e9,
                 "h": box[3] - box[1]}
            seq = {"ts": ts, "rows": [b]}
            info = S.draw_one(seq, b, 0, sess)
            rec = {"session": name, "event": ts, "err_s": best["err"],
                   "v": best["v"], "z_last": z, "gap_m": gap,
                   "center2d_m": center2d, "t_last_s": b["gap"],
                   "box": list(box), "file": info["file"]}
            records.append(rec)
            print(f"  {name} {ts} err={best['err']:.2f} z={z:.1f} "
                  f"gap={gap:+.2f} center={center2d:.2f}", flush=True)
    gaps = [r["gap_m"] for r in records]
    in_band = [g for g in gaps if 0.0 < g <= 0.8]
    summary = {
        "n": len(records),
        "in_band": len(in_band),
        "below0": sum(1 for g in gaps if g <= 0.0),
        "above": sum(1 for g in gaps if g > 0.8),
        "gap_median": float(np.median(gaps)) if gaps else None,
        "gap_p10": float(np.percentile(gaps, 10)) if gaps else None,
        "gap_p90": float(np.percentile(gaps, 90)) if gaps else None,
        "err_median": float(np.median([r["err_s"] for r in records]))
        if records else None,
    }
    (OUT / "summary.json").write_text(
        json.dumps({"summary": summary, "records": records},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
