# -*- coding: utf-8 -*-
"""邻道车连续接近：框底行几何 vs 深度距离 vs 框高，逐帧对照。

**要回答**（2026-11-14）：
1. 同一辆邻道车从远到近的连续片段里，框底行几何（当前生产主路）是否单调、
   帧间抖动多大；
2. 深度模型距离在哪些距离段可用、是否更稳，近场是否真的「可信到能接管」；
3. 两套距离在规划阈值（0.35 s 迎向、2 s 视界、6 m 静态近车、5 m 碰撞窗）
   上是否给出不同结论——即深度是否值得参与控制。

**口径**：
- 片段从实机会话原始帧挖：YOLO 逐帧检测 + 贪心多目标跟踪，取「邻道 + 远到近」
  的连续轨道；只做离线筛选，不依赖生产 trace 的粗距。
- 几何 `z = fy·cam_h/(y_bot−y_h)`，`fy=630`、`cam_h=1.25`，`y_h` 取车道线
  灭点行 5 帧中位、缺值回退 361.0（生产 v5 同口径）。
- 深度 `z = m·k/(V−A−a·u)`，地面模型 `V=A+m·(r−y_h)+a·u` 挖框拟合，MAD>0.06
  弃帧；`k=fy·cam_h`。与 `probe_contact_chain` 的 `z_con` 同式，这次直接叫深度距离。
- 框高 `z_h = 1.5·fy/h_px` 只作第三证据（目标高未知，1.5 m 是标称）。
- 邻道判定：相机系横向 `|x|>1.2 m` 或框被画面横向截断（生产锚点门的同一口径）。
- 闭速由几何序列局部斜率估计（邻道车生产里没有锚点闭速），阈值判定只回答
  「同一闭速下两套距离是否落进不同决策带」。

输出 `adjacent_seq_out/adjacent_seq.json`、逐段叠加图与一份控制台摘要。
"""
from __future__ import annotations

import json
import os
import sys
from collections import deque
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "adjacent_seq_out"
APPDATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
DEBUG = APPDATA / "MaaRacingMaster" / "debug"

FY = 630.0
CAM_H = 1.25
K = FY * CAM_H
VPX = 640.7
YH_FALLBACK = 361.0
YH_WIN = 5
CAR_H = 1.5
ADJ_X_M = 1.2
GROUND_MAD_MAX = 0.06
Z_LO, Z_HI = 2.0, 90.0

# 规划阈值（decision.json，2026-11-14 取值）
T_LATE = 0.35
T_HORIZON = 2.0
NEAR_Z = 6.0
COLLISION_Z = 5.0

SESSIONS = [
    ("20261113_224406", ["20261113_224456_p1", "20261113_224456_p2"]),
    ("20261113_120841", ["20261113_120909_p1", "20261113_120909_p2"]),
    ("20261113_105948", ["20261113_110314_p1", "20261113_110314_p2"]),
    ("20261113_154207", ["20261113_154216_p1", "20261113_154216_p2"]),
]
MIN_LEN = 18
MAX_GAP = 3
MAX_SEQS = 12


# ---------- 帧清单 ----------
def frames_of(drive: Path) -> list[dict]:
    recs = []
    with open(drive / "frames.jsonl", "r", encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                recs.append(json.loads(ln))
    recs.sort(key=lambda r: r["ts_ns"])
    return recs


# ---------- 几何 ----------
def geom_z(box, yh: float) -> float | None:
    dz = float(box[3]) - float(yh)
    if dz <= 2.0:
        return None
    z = K / dz
    return z if Z_LO <= z <= Z_HI else None


def cam_x(box, z: float) -> float:
    return (0.5 * (box[0] + box[2]) - VPX) * z / FY


def clipped(box) -> bool:
    return box[0] <= 2 or box[2] >= 1278 or box[3] >= 714


# ---------- 车道线：逐帧灭点行 ----------
def lane_yv(lane, rgb) -> float | None:
    prob = lg.lane_prob(lane, rgb)
    fits = [f for f in (lg.fit_line(t) for t in lg.tracks(prob)) if f]
    if len(fits) < 2:
        return None
    return lg.vanish_row(fits)


# ---------- 多目标贪心跟踪 ----------
def mine_tracks(boxes_by_frame: dict[int, list]) -> list[dict]:
    tracks: list[dict] = []
    for k in sorted(boxes_by_frame):
        dets = boxes_by_frame[k]
        pairs = []
        for ti, tr in enumerate(tracks):
            gap = k - tr["last_k"]
            if gap > MAX_GAP:
                continue
            prev = tr["last"]
            pcx, pcy = 0.5 * (prev[0] + prev[2]), 0.5 * (prev[1] + prev[3])
            wp, hp = prev[2] - prev[0], prev[3] - prev[1]
            grow = 1.0 + 0.6 * gap
            for di, b in enumerate(dets):
                bcx, bcy = 0.5 * (b[0] + b[2]), 0.5 * (b[1] + b[3])
                h = b[3] - b[1]
                dx, dy = abs(bcx - pcx), abs(bcy - pcy)
                if dx > max(50.0, 0.9 * wp) * grow:
                    continue
                if dy > max(36.0, 0.9 * hp) * grow:
                    continue
                if not (0.45 * hp <= h <= 2.1 * hp * grow):
                    continue
                pairs.append((dx + 2.0 * dy, ti, di))
        pairs.sort()
        used_t, used_d = set(), set()
        for _, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            tr = tracks[ti]
            tr["boxes"].append((k, dets[di]))
            tr["last"], tr["last_k"] = dets[di], k
            used_t.add(ti)
            used_d.add(di)
        for di, b in enumerate(dets):
            if di not in used_d:
                tracks.append({"boxes": [(k, b)], "last": b, "last_k": k})
    return tracks


def track_profile(track: dict, yv_by_frame: dict[int, float | None]) -> dict | None:
    """用固定口径给整条轨道算横向/纵深，判断是否是「邻道 + 远到近」。"""
    zs, xs, hs, bs = [], [], [], []
    for k, b in track["boxes"]:
        yh = yv_by_frame.get(k) or YH_FALLBACK
        z = geom_z(b, yh)
        if z is None:
            continue
        zs.append(z)
        xs.append(cam_x(b, z))
        hs.append(b[3] - b[1])
        bs.append(b)
    if len(zs) < MIN_LEN or not hs:
        return None
    ks = [k for k, b in track["boxes"]
          if geom_z(b, yv_by_frame.get(k) or YH_FALLBACK) is not None]
    zs_a = np.asarray(zs)
    span = float(zs_a.max() - zs_a.min())
    dec = float(np.mean(np.diff(zs_a) < 0.0)) if len(zs_a) > 1 else 0.0
    adjacent = sum(1 for x, b in zip(xs, bs)
                   if abs(x) > ADJ_X_M or clipped(b))
    adj_mid = sum(1 for z, x in zip(zs, xs)
                  if 10.0 <= z <= 45.0 and abs(x) > ADJ_X_M)
    if span < 15.0 or zs_a.max() < 25.0 or zs_a.min() > 12.0:
        return None
    if dec < 0.7 or adjacent < 5 or adj_mid < 3:
        return None
    if float(np.median(hs)) < 10.0:
        return None
    # 同一辆车：横向帧间跳变小、纵深不回跳、框高单帧不突变
    dx, dz, dh = [], [], []
    for i in range(1, len(zs)):
        if ks[i] - ks[i - 1] > 2:
            continue
        dx.append(abs(xs[i] - xs[i - 1]))
        dz.append(zs[i] - zs[i - 1])
        dh.append(abs(hs[i] - hs[i - 1]) / max(1.0, hs[i - 1]))
    if len(dx) < 8:
        return None
    dx_med = float(np.median(dx))
    dx_p90 = float(np.percentile(dx, 90))
    back = sum(1 for d in dz if d > 6.0)
    h_jump = sum(1 for d in dh if d > 0.35)
    if dx_med > 0.7 or dx_p90 > 3.0:
        return None
    if back > max(2, len(dz) // 8) or h_jump > max(2, len(dh) // 6):
        return None
    near_x = [abs(x) for z, x in zip(zs, xs) if 8.0 < z < 25.0]
    if len(near_x) < 4:
        return None
    xn = float(np.median(near_x))
    if not 1.2 <= xn <= 6.5:
        return None
    return {"n": len(zs), "span": span, "dec": dec,
            "adjacent": adjacent, "adj_mid": adj_mid,
            "z_min": float(zs_a.min()), "z_max": float(zs_a.max()),
            "dx_med": dx_med, "dx_p90": dx_p90, "back": back,
            "h_jump": h_jump, "x_near": xn}


# ---------- 深度 ----------
def ground_fit_mad(v: np.ndarray, yh: float,
                   excl_boxes) -> tuple | None:
    """地面拟合。excl_boxes 是**全部**检测框（生产口径：物体掩码整体挖除），
    只挖目标框会把旁车车身留进路面样本、抬 MAD。"""
    h, w = v.shape
    rows = np.arange(h)[:, None]
    mask = np.broadcast_to((rows >= 380) & (rows <= 690), (h, w)).copy()
    for x0, y0, x1, y1 in excl_boxes:
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
    keep = np.ones(len(y), bool)
    for _ in range(4):
        resid = A @ coef - y
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        keep = np.abs(resid - np.median(resid)) < 2.5 * mad
        coef = np.linalg.lstsq(A[keep], y[keep], rcond=None)[0]
    resid = A[keep] @ coef - y[keep]
    mad = float(np.median(np.abs(resid - np.median(resid))) * 1.4826)
    return float(coef[0]), float(coef[1]), float(coef[2]), mad


def band_pixels(v: np.ndarray, box) -> tuple[np.ndarray, float]:
    gh, gw = v.shape[:2]
    x0 = max(0, int(box[0] * gw / 1280))
    y0 = max(0, int(box[1] * gh / 720))
    x1 = min(gw, int(box[2] * gw / 1280))
    y1 = min(gh, int(box[3] * gh / 720))
    yb = y0 + (y1 - y0) * 3 // 4
    zv = v[yb:y1, x0:x1].ravel()
    zv = zv[np.isfinite(zv)]
    return zv, 0.5 * (x0 + x1) / gw


def depth_z(vv: float, m: float, a: float, A: float, u: float) -> float | None:
    den = vv - A - a * u
    if den <= 1e-6:
        return None
    z = m * K / den
    return z if 0.5 <= z <= 400.0 else None


def depth_measure(rgb, box, yh: float, da, all_boxes=()) -> dict:
    out = da.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
    v = sim.resize_ac(out.astype(np.float64), 720, 1280)
    fit = ground_fit_mad(v, yh, tuple(all_boxes) or (box,))
    rec = {"ok": False, "mad": None, "z": None}
    if fit is None:
        return rec
    m, a, A, mad = fit
    rec["mad"] = mad
    if mad > GROUND_MAD_MAX:
        rec["skip"] = "mad"
        return rec
    zv, u = band_pixels(v, box)
    if zv.size < 5:
        return rec
    vmed = float(np.median(zv))
    z = depth_z(vmed, m, a, A, u)
    if z is None:
        return rec
    z_near = depth_z(float(np.percentile(zv, 90)), m, a, A, u)
    z_far = depth_z(float(np.percentile(zv, 10)), m, a, A, u)
    yb = float(box[3])
    corr = (vmed - (A + m * (yb - yh) + a * u)) / m if abs(m) > 1e-6 else 0.0
    rec.update({"ok": True, "z": z, "z_near": z_near, "z_far": z_far,
                "corr_px": float(corr), "den": float(vmed - A - a * u)})
    return rec


# ---------- 闭速与决策带 ----------
def decisions(rows: list[dict]) -> None:
    n = len(rows)
    ts = np.asarray([r["ts_s"] for r in rows])
    zg = np.asarray([r["z_geo"] if r["z_geo"] is not None else np.nan for r in rows])
    for i, r in enumerate(rows):
        lo, hi = max(0, i - 2), min(n, i + 3)
        mask = np.isfinite(zg[lo:hi])
        if mask.sum() < 3:
            continue
        t = ts[lo:hi][mask]
        z = zg[lo:hi][mask]
        if np.ptp(t) <= 0:
            continue
        slope = float(np.polyfit(t - t.mean(), z, 1)[0])
        v = -slope
        if not 0.5 <= v <= 60.0:
            continue
        r["v_close"] = v
        for key in ("z_geo", "z_dep"):
            zz = r[key]
            r[f"t_{key[2:]}"] = (zz / v) if zz is not None else None
        r["band_geo"] = tband(r["t_geo"])
        r["band_dep"] = tband(r["t_dep"])
        r["near_geo"] = r["z_geo"] is not None and r["z_geo"] <= NEAR_Z
        r["near_dep"] = r["z_dep"] is not None and r["z_dep"] <= NEAR_Z


def tband(t: float | None) -> str:
    if t is None:
        return "none"
    if t < T_LATE:
        return "late"
    if t <= T_HORIZON:
        return "window"
    return "far"


def med(vals):
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


def p90(vals):
    vals = [v for v in vals if v is not None]
    return float(np.percentile(vals, 90)) if vals else None


def summarize(rows: list[dict]) -> dict:
    bins = [(50, 999, ">50"), (30, 50, "30-50"), (20, 30, "20-30"),
            (12, 20, "12-20"), (6, 12, "6-12"), (0, 6, "<6")]
    avail = {}
    for lo, hi, name in bins:
        sel = [r for r in rows if r["z_geo"] is not None and lo <= r["z_geo"] < hi]
        avail[name] = {"n": len(sel),
                       "dep_ok": sum(1 for r in sel if r["z_dep"] is not None),
                       "mad": sum(1 for r in sel if r["dep_skip"] == "mad"),
                       "dep_med": med([r["z_dep"] for r in sel]),
                       "geo_med": med([r["z_geo"] for r in sel])}
    dep_err = [abs(r["z_dep"] - r["z_geo"]) / r["z_geo"]
               for r in rows if r["z_dep"] is not None and r["z_geo"]]
    dep_bias = [(r["z_dep"] - r["z_geo"]) / r["z_geo"]
                for r in rows if r["z_dep"] is not None and r["z_geo"]]
    h_err = [abs(r["z_geo"] - r["z_h"]) / r["z_h"]
             for r in rows if r["z_geo"] and r["z_h"] and r["h"] >= 25]
    jit_geo = [abs(b["z_geo"] - a["z_geo"]) / a["z_geo"]
               for a, b in zip(rows, rows[1:])
               if a["z_geo"] and b["z_geo"] and b["k"] - a["k"] <= 2]
    jit_dep = [abs(b["z_dep"] - a["z_dep"]) / a["z_dep"]
               for a, b in zip(rows, rows[1:])
               if a["z_dep"] and b["z_dep"] and b["k"] - a["k"] <= 2]
    both = [r for r in rows
            if r.get("t_geo") is not None and r.get("t_dep") is not None]
    flips = [r for r in both if r["band_geo"] != r["band_dep"]]
    near_flips = [r for r in both if r["near_geo"] != r["near_dep"]]
    return {
        "n": len(rows),
        "dep_ok": sum(1 for r in rows if r["z_dep"] is not None),
        "availability": avail,
        "dep_vs_geo": {"n": len(dep_err), "err_med": med(dep_err),
                       "bias_med": med(dep_bias),
                       "err_p90": p90(dep_err)},
        "geo_vs_h": {"n": len(h_err), "err_med": med(h_err),
                     "err_p90": p90(h_err)},
        "jitter": {"geo_med": med(jit_geo), "geo_p90": p90(jit_geo),
                   "dep_med": med(jit_dep), "dep_p90": p90(jit_dep)},
        "decision": {"both": len(both), "band_flips": len(flips),
                     "near_flips": len(near_flips),
                     "flip_rows": [{"k": r["k"], "z_geo": r["z_geo"],
                                    "z_dep": r["z_dep"],
                                    "t_geo": r["t_geo"], "t_dep": r["t_dep"],
                                    "band_geo": r["band_geo"],
                                    "band_dep": r["band_dep"]}
                                   for r in flips]},
    }


# ---------- 主流程 ----------
def scan_drives(det, lane) -> tuple[list[dict], dict]:
    scan = {}
    for sess, drives in SESSIONS:
        for name in drives:
            drive = DEBUG / sess / "speedrush" / "drive" / name
            if not (drive / "frames.jsonl").exists():
                continue
            recs = frames_of(drive)
            boxes, yvs = {}, {}
            for k, rec in enumerate(recs):
                img = cv2.imread(str(drive / rec["file"]))
                if img is None:
                    continue
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                boxes[k] = sim.detect_cars(det, rgb)
                yvs[k] = lane_yv(lane, rgb)
            scan[name] = {"drive": drive, "recs": recs,
                          "boxes": boxes, "yvs": yvs}
            print(f"scan {sess}/{name}: {len(recs)} frames", flush=True)
    return scan


def save_scan(scan: dict) -> None:
    data = {name: {
        "recs": d["recs"],
        "boxes": [[k, [list(b) for b in bs]]
                  for k, bs in sorted(d["boxes"].items())],
        "yvs": [[k, v] for k, v in sorted(d["yvs"].items())],
    } for name, d in scan.items()}
    (OUT / "scan_cache.json").write_text(
        json.dumps(data), encoding="utf-8")


def load_scan() -> dict:
    paths = {name: DEBUG / sess / "speedrush" / "drive" / name
             for sess, drives in SESSIONS for name in drives}
    data = json.loads((OUT / "scan_cache.json").read_text(encoding="utf-8"))
    scan = {}
    for name, d in data.items():
        if name not in paths:
            continue
        scan[name] = {
            "drive": paths[name],
            "recs": d["recs"],
            "boxes": {int(k): [tuple(b) for b in bs]
                      for k, bs in d["boxes"]},
            "yvs": {int(k): v for k, v in d["yvs"]},
        }
    return scan


def pick_sequences(scan: dict) -> list[dict]:
    cands = []
    for name, d in scan.items():
        tracks = mine_tracks(d["boxes"])
        for ti, tr in enumerate(tracks):
            prof = track_profile(tr, d["yvs"])
            if prof is None:
                continue
            score = prof["n"] + 4.0 * prof["adj_mid"] + 0.5 * prof["span"]
            cands.append({"drive": name, "track_idx": ti, "track": tr,
                          "prof": prof, "score": score})
    cands.sort(key=lambda c: -c["score"])
    picked, per_drive = [], {}
    for c in cands:
        if per_drive.get(c["drive"], 0) >= 2:
            continue
        picked.append(c)
        per_drive[c["drive"]] = per_drive.get(c["drive"], 0) + 1
        if len(picked) >= MAX_SEQS:
            break
    return picked


def process_sequence(seq: dict, scan: dict, da) -> dict:
    d = scan[seq["drive"]]
    recs, drive = d["recs"], d["drive"]
    boxes = dict(seq["track"]["boxes"])
    yv_hist = deque(maxlen=YH_WIN)
    rows = []
    for k in sorted(boxes):
        rec = recs[k]
        img = cv2.imread(str(drive / rec["file"]))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        yv = d["yvs"].get(k)
        if yv is not None:
            yv_hist.append(yv)
        yh = float(np.median(yv_hist)) if yv_hist else YH_FALLBACK
        yh_src = "lane" if yv_hist else "fallback"
        box = boxes[k]
        z_geo = geom_z(box, yh)
        hp = box[3] - box[1]
        row = {
            "k": k, "file": rec["file"], "ts_s": rec["ts_ns"] / 1e9,
            "box": list(box), "y_h": yh, "y_h_src": yh_src,
            "h": hp, "w": box[2] - box[0],
            "x_cam": None if z_geo is None else cam_x(box, z_geo),
            "z_geo": z_geo,
            "z_h": (CAR_H * FY / hp) if hp >= 4 else None,
            "clipped": clipped(box),
            "z_dep": None,
        }
        dm = depth_measure(rgb, box, yh, da, d["boxes"].get(k, ()))
        row.update({"mad": dm.get("mad"), "dep_skip": dm.get("skip"),
                    "z_dep": dm.get("z"), "z_dep_near": dm.get("z_near"),
                    "z_dep_far": dm.get("z_far"),
                    "corr_px": dm.get("corr_px")})
        rows.append(row)
    decisions(rows)
    summary = summarize(rows)
    return {"drive": seq["drive"], "track_idx": seq["track_idx"],
            "profile": seq["prof"], "rows": rows, "summary": summary}


def overlay(seq_res: dict, scan: dict) -> None:
    d = scan[seq_res["drive"]]
    rows = seq_res["rows"]
    if not rows:
        return
    step = max(1, len(rows) // 5)
    for idx, r in enumerate(rows):
        if idx % step and idx != len(rows) - 1:
            continue
        img = cv2.imread(str(d["drive"] / r["file"]))
        if img is None:
            continue
        x0, y0, x1, y1 = r["box"]
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 255, 0), 2)
        yh = r["y_h"]
        cv2.line(img, (0, int(yh)), (1279, int(yh)), (0, 0, 255), 1)
        yb = y0 + (y1 - y0) * 3 // 4
        cv2.rectangle(img, (x0, yb), (x1, y1), (255, 0, 255), 1)
        cv2.putText(img, f"z_geo={r['z_geo']:.1f}"
                    if r["z_geo"] else "z_geo=-", (12, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(img, "z_dep=-" if r["z_dep"] is None
                    else f"z_dep={r['z_dep']:.1f}", (12, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        name = (f"{seq_res['drive']}_t{seq_res['track_idx']}_{r['k']:04d}.jpg")
        cv2.imwrite(str(OUT / name), img)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    cache = OUT / "scan_cache.json"
    if cache.exists():
        scan = load_scan()
        print(f"scan cache: {len(scan)} drives")
    else:
        det = sim.session("prod_perception/model.onnx")
        lane = sim.session("lane/plus_small_384x640.onnx")
        scan = scan_drives(det, lane)
        save_scan(scan)
    picked = pick_sequences(scan)
    print(f"picked {len(picked)} sequences")
    for p in picked:
        print(" ", p["drive"], p["prof"])
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    seqs = []
    for i, p in enumerate(picked):
        res = process_sequence(p, scan, da)
        res["summary"].update({k: res[k] for k in ("drive", "track_idx")})
        seqs.append(res)
        print(f"[{i + 1}/{len(picked)}] {res['drive']} t{p['track_idx']} "
              f"n={res['summary']['n']} dep_ok={res['summary']['dep_ok']}",
              flush=True)
        overlay(res, scan)
    out = {"params": {"fy": FY, "cam_h": CAM_H, "y_h_window": YH_WIN,
                      "adj_x_m": ADJ_X_M, "mad_max": GROUND_MAD_MAX,
                      "t_late": T_LATE, "t_horizon": T_HORIZON,
                      "near_z": NEAR_Z, "collision_z": COLLISION_Z},
           "sessions": [s for s, _ in SESSIONS], "sequences": seqs}
    (OUT / "adjacent_seq.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    for s in seqs:
        sm = s["summary"]
        print("\n==", s["drive"], "t", s["track_idx"], "==")
        print(json.dumps({k: sm[k] for k in
                          ("n", "dep_ok", "dep_vs_geo", "geo_vs_h",
                           "jitter", "decision")},
                         ensure_ascii=False, indent=1)[:1500])
        print("availability:", json.dumps(sm["availability"],
                                          ensure_ascii=False)[:900])
    print("\nsaved:", OUT / "adjacent_seq.json")


if __name__ == "__main__":
    main()
