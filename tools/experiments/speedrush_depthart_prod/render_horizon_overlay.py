"""Render horizon (published vs raw) overlays and montages from local session data.

Reads %APPDATA%/MaaRacingMaster logs + saved drive frames; writes horizon_overlay_out/
(gitignored). The goal is visual attribution of the published-vs-raw horizon gap:
draw the actual lane fits and their pairwise intersections on the frame, then mark the
raw estimate, the published value, and the physical gate.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(os.environ["APPDATA"]) / "MaaRacingMaster"
HERE = Path(__file__).resolve().parent
OUT = HERE / "horizon_overlay_out"

GATE_LO, GATE_HI = 341.0, 381.0
SPAN_LO, SPAN_HI = 60.0, 900.0
PROBE_Y = 520.0
FALLBACK = 361.0
W, H = 1280, 720

SESSIONS = ("20261113_203823", "20261113_212838", "20261113_224406")


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


MANUAL_DIRS = {
    "20261113_203823": (
        "20261113_203839_manual_p1",
        "20261113_203839_manual_p2",
        "20261113_204052_manual_p1",
        "20261113_204052_manual_p2",
    ),
}


def _split_runs(rows: list[dict]) -> list[list[dict]]:
    runs, cur = [], []
    prev = None
    for r in rows:
        st = r.get("stage_id")
        if prev is not None and st != prev:
            runs.append(cur)
            cur = []
        cur.append(r)
        prev = st
    if cur:
        runs.append(cur)
    return runs


def load_manual(name: str, rows: list[dict]) -> list[dict]:
    """203823 drove without the per-session drive recorder; the manual debug dirs
    store one raw frame per tick and their counts match the four stage runs."""
    dirs = MANUAL_DIRS.get(name)
    if not dirs:
        return []
    runs = _split_runs(rows)
    out = []
    for run, dname in zip(runs, dirs):
        base = ROOT / "debug" / "speedrush" / dname
        idx = base / "frames.jsonl"
        if not idx.exists():
            continue
        entries = list(read_jsonl(idx))
        if len(entries) != len(run):
            print(f"  {dname}: frames {len(entries)} != ticks {len(run)}, skip")
            continue
        for r, e in zip(run, entries):
            out.append({"fid": r["fid"], "ts_mono_ns": r.get("ts_mono_ns"),
                        "path": str(base / e["file"]),
                        "hud": str(base / e["hud"]) if e.get("hud") else None})
    return out


def load_session(name: str):
    trace = list(read_jsonl(ROOT / "logs" / name / "trace" / "speedrush.jsonl"))
    rows = [r for r in trace
            if r.get("fid") is not None and isinstance(r.get("lane_obs"), dict)]
    rows.sort(key=lambda r: r["fid"])
    frames = []
    idx = ROOT / "debug" / name / "speedrush" / "drive" / "frames.jsonl"
    if idx.exists():
        frames = [r for r in read_jsonl(idx) if r.get("fid") is not None]
        frames.sort(key=lambda r: r["fid"])
    if not frames:
        full = [r for r in trace if r.get("fid") is not None]
        frames = load_manual(name, full)
        frames.sort(key=lambda r: r["fid"])
    return rows, frames


def row_at_fid(rows: list[dict], fid: int, max_d: int = 2) -> dict | None:
    if not rows:
        return None
    best = min(rows, key=lambda r: abs(r["fid"] - fid))
    return best if abs(best["fid"] - fid) <= max_d else None


def frame_at_fid(frames: list[dict], fid: int) -> dict | None:
    if not frames:
        return None
    best = min(frames, key=lambda r: abs(r["fid"] - fid))
    return best if abs(best["fid"] - fid) <= 6 else None


def line_fits(row: dict) -> list[dict]:
    out = []
    for ln in (row.get("lane_obs") or {}).get("lines") or []:
        fts = ln.get("fits") or []
        if not fts:
            continue
        ft = fts[0]
        s, a = ft.get("s"), ft.get("a")
        if s is None or a is None:
            continue
        out.append({"s": float(s), "a": float(a),
                    "y0": float(ft.get("y0", 0)), "y1": float(ft.get("y1", H)),
                    "accepted": bool(ln.get("accepted")), "k": ln.get("k")})
    return out


def intersections(fits: list[dict]):
    acc, rej = [], []
    for i in range(len(fits)):
        for j in range(i + 1, len(fits)):
            s1, a1 = fits[i]["s"], fits[i]["a"]
            s2, a2 = fits[j]["s"], fits[j]["a"]
            if abs(s1 - s2) < 5e-3:
                continue
            yv = (a2 - a1) / (s1 - s2)
            x = s1 * yv + a1
            d = abs((s1 - s2) * PROBE_Y + (a1 - a2))
            ok = GATE_LO <= yv <= GATE_HI and SPAN_LO <= d <= SPAN_HI
            (acc if ok else rej).append((x, yv))
    return acc, rej


def _hline(img, y, color, label, dash=False, thick=1):
    if y is None:
        return
    yi = int(round(float(y)))
    if not 0 <= yi < H:
        return
    if dash:
        for x in range(0, W, 26):
            cv2.line(img, (x, yi), (min(W, x + 13), yi), color, thick, cv2.LINE_AA)
    else:
        cv2.line(img, (0, yi), (W, yi), color, thick, cv2.LINE_AA)
    cv2.putText(img, label, (6, max(12, yi - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, color, 1, cv2.LINE_AA)


def draw_lane_geometry(img, row, caption: str) -> np.ndarray:
    fits = line_fits(row)
    for f in fits:
        color = (255, 150, 0) if f["accepted"] else (145, 145, 145)
        y0, y1 = int(max(0, f["y0"])), int(min(H - 1, f["y1"]))
        if y1 <= y0:
            continue
        p1 = (int(f["s"] * y0 + f["a"]), y0)
        p2 = (int(f["s"] * y1 + f["a"]), y1)
        cv2.line(img, p1, p2, color, 2, cv2.LINE_AA)
        if f["k"] is not None:
            cv2.putText(img, f"L{f['k']}", (max(2, min(W - 38, p2[0])),
                                            max(12, min(H - 4, p2[1]))),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    acc, rej = intersections(fits)
    for x, y in rej:
        if 0 <= y < H and -60 <= x < W + 60:
            cv2.circle(img, (int(x), int(y)), 3, (0, 0, 220), -1, cv2.LINE_AA)
    for x, y in acc:
        if 0 <= y < H and -60 <= x < W + 60:
            cv2.circle(img, (int(x), int(y)), 5, (0, 220, 0), -1, cv2.LINE_AA)

    lo = row.get("lane_obs") or {}
    pub = lo.get("y_v")
    raw = lo.get("y_v_raw")
    dgeo = row.get("dgeo_yv")
    _hline(img, GATE_LO, (0, 0, 200), "gate 341", True)
    _hline(img, GATE_HI, (0, 0, 200), "gate 381", True)
    _hline(img, FALLBACK, (165, 165, 165), "fallback 361", True)
    _hline(img, raw, (0, 150, 255), "raw", True)
    _hline(img, pub, (0, 255, 255), "pub", False, 2)
    if dgeo is not None and pub is not None and abs(float(dgeo) - float(pub)) > 0.5:
        _hline(img, dgeo, (0, 255, 255), "dgeo", True)

    def fmt(v):
        return "None" if v is None else f"{float(v):.1f}"

    gap = None if pub is None or raw is None else float(pub) - float(raw)
    lines = [
        f"fid {row['fid']}  {caption}",
        f"pub {fmt(pub)}  raw {fmt(raw)}  gap {fmt(gap)}  dgeo {fmt(dgeo)}",
        f"n_lines {lo.get('n_lines')}  cam_h_lane {fmt(lo.get('cam_h_lane'))}  "
        f"cam_h {fmt(lo.get('cam_h'))}",
        f"mad {row.get('dgeo_mad_skips')}  state {row.get('state')}  "
        f"v_ego {fmt(row.get('v_ego'))}",
    ]
    box = img.copy()
    cv2.rectangle(box, (4, 4), (640, 106), (0, 0, 0), -1)
    img[:] = cv2.addWeighted(box, 0.55, img, 0.45, 0)
    for i, text in enumerate(lines):
        cv2.putText(img, text, (10, 24 + i * 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.58, (235, 235, 235), 1, cv2.LINE_AA)
    return img


def frame_path(name: str, entry: dict) -> Path:
    if entry.get("path"):
        return Path(entry["path"])
    return ROOT / "debug" / name / "speedrush" / "drive" / entry["file"]


def render_anomaly(name: str, rows: list[dict], frames: list[dict], out_dir: Path):
    segs, cur = [], []
    for r in rows:
        yv = r.get("dgeo_yv")
        if yv is not None and float(yv) > 370.0:
            if cur and r["ts_mono_ns"] - cur[-1]["ts_mono_ns"] > 2e9:
                segs.append(cur)
                cur = []
            cur.append(r)
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    if not segs:
        return None
    seg = max(segs, key=len)
    t0 = seg[0]["ts_mono_ns"]
    picks = []
    if frames:
        in_range = [f for f in frames if seg[0]["fid"] - 6 <= f["fid"] <= seg[-1]["fid"] + 6]
        if len(in_range) > 9:
            idx = np.linspace(0, len(in_range) - 1, 9).astype(int)
            picks = [in_range[i] for i in idx]
        else:
            picks = in_range
    tiles = []
    for entry in picks:
        row = row_at_fid(rows, entry["fid"])
        if row is None:
            continue
        img = cv2.imread(str(frame_path(name, entry)))
        if img is None:
            continue
        img = cv2.resize(img, (W, H))
        t_rel = (row["ts_mono_ns"] - t0) / 1e9
        cap = f"t+{t_rel:.1f}s frame {entry['fid']}"
        img = draw_lane_geometry(img, row, cap)
        cv2.imwrite(str(out_dir / f"anomaly_{row['fid']:04d}.jpg"), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        tiles.append((img, cap))
    if not tiles:
        for row in [seg[int(i)] for i in np.linspace(0, len(seg) - 1, 9).astype(int)]:
            canvas = np.full((H, W, 3), 26, np.uint8)
            for gx in range(0, W, 80):
                cv2.line(canvas, (gx, 0), (gx, H), (44, 44, 44), 1)
            for gy in range(0, H, 80):
                cv2.line(canvas, (0, gy), (W, gy), (44, 44, 44), 1)
            t_rel = (row["ts_mono_ns"] - t0) / 1e9
            cap = f"t+{t_rel:.1f}s NO FRAME"
            img = draw_lane_geometry(canvas, row, cap)
            cv2.putText(img, "no saved drive frame for this session", (10, H - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 80, 220), 2, cv2.LINE_AA)
            tiles.append((img, cap))
    montage(tiles, out_dir / "anomaly_montage.jpg")
    plot_series(seg, out_dir / "anomaly_timeseries.png", name)
    return seg


def montage(tiles, path: Path, cols: int = 3, tw: int = 640, th: int = 360):
    rows = (len(tiles) + cols - 1) // cols
    canvas = np.full((rows * (th + 34), cols * tw, 3), 24, np.uint8)
    for i, (img, cap) in enumerate(tiles):
        r, c = divmod(i, cols)
        y, x = r * (th + 34), c * tw
        canvas[y:y + th, x:x + tw] = cv2.resize(img, (tw, th))
        cv2.putText(canvas, cap, (x + 6, y + th + 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.52, (230, 230, 230), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])


def plot_series(rows: list[dict], path: Path, name: str):
    if not rows:
        return
    t0 = rows[0]["ts_mono_ns"]
    ts = [(r["ts_mono_ns"] - t0) / 1e9 for r in rows]
    pub = [None if r.get("lane_obs", {}).get("y_v") is None
           else float(r["lane_obs"]["y_v"]) for r in rows]
    raw = [None if r.get("lane_obs", {}).get("y_v_raw") is None
           else float(r["lane_obs"]["y_v_raw"]) for r in rows]
    dgeo = [None if r.get("dgeo_yv") is None else float(r["dgeo_yv"]) for r in rows]
    camh = [None if r.get("lane_obs", {}).get("cam_h_lane") is None
            else float(r["lane_obs"]["cam_h_lane"]) for r in rows]
    med5 = []
    win = []
    for v in raw:
        if v is not None:
            win.append(v)
            win = win[-5:]
        med5.append(float(np.median(win)) if win else None)

    canvas = np.full((720, 1280, 3), 26, np.uint8)
    left, right = 70, 1240
    top, bottom = 40, 430
    y_lo, y_hi = 330.0, 390.0

    def px(t):
        return int(left + (right - left) * (t / max(ts[-1], 1e-6)))

    def py(v):
        return int(bottom - (bottom - top) * ((v - y_lo) / (y_hi - y_lo)))

    for v in (340, 350, 360, 370, 380, 390):
        cv2.line(canvas, (left, py(v)), (right, py(v)), (55, 55, 55), 1)
        cv2.putText(canvas, str(v), (20, py(v) + 5), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (180, 180, 180), 1, cv2.LINE_AA)
    for v, col, lab, dash in ((GATE_LO, (0, 0, 200), "gate 341", True),
                              (GATE_HI, (0, 0, 200), "gate 381", True),
                              (FALLBACK, (150, 150, 150), "fallback 361", True)):
        for x in range(left, right, 28):
            cv2.line(canvas, (x, py(v)), (min(right, x + 14), py(v)), col, 1)
        cv2.putText(canvas, lab, (right - 120, py(v) - 4), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, col, 1, cv2.LINE_AA)

    def poly(vals, color, thick=1, dash=False):
        pts = [(px(t), py(v)) for t, v in zip(ts, vals) if v is not None]
        for i in range(1, len(pts)):
            if dash and i % 2 == 0:
                continue
            cv2.line(canvas, pts[i - 1], pts[i], color, thick, cv2.LINE_AA)

    poly(raw, (0, 150, 255), 1)
    poly(med5, (0, 220, 0), 2)
    poly(dgeo, (0, 255, 255), 1, True)
    poly(pub, (0, 255, 255), 2)

    # cam_h_lane panel
    ctop, cbot = 520, 680
    c_lo, c_hi = 0.5, 1.6

    def cy(v):
        return int(cbot - (cbot - ctop) * ((v - c_lo) / (c_hi - c_lo)))

    for v in (0.6, 0.8, 1.0, 1.2, 1.35, 1.6):
        cv2.line(canvas, (left, cy(v)), (right, cy(v)), (55, 55, 55), 1)
        cv2.putText(canvas, f"{v:.2f}", (18, cy(v) + 5), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (180, 180, 180), 1, cv2.LINE_AA)
    for v in (1.20, 1.35):
        for x in range(left, right, 28):
            cv2.line(canvas, (x, cy(v)), (min(right, x + 14), cy(v)), (0, 180, 0), 1)
    pts = [(px(t), cy(v)) for t, v in zip(ts, camh) if v is not None]
    for i in range(1, len(pts)):
        cv2.line(canvas, pts[i - 1], pts[i], (200, 200, 0), 2, cv2.LINE_AA)

    legend = [("pub", (0, 255, 255)), ("dgeo", (0, 255, 255)),
              ("raw", (0, 150, 255)), ("med5 raw", (0, 220, 0)),
              ("cam_h_lane", (200, 200, 0))]
    for i, (lab, col) in enumerate(legend):
        x = left + 10 + i * 150
        cv2.line(canvas, (x, 14), (x + 26, 14), col, 3)
        cv2.putText(canvas, lab, (x + 32, 19), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (230, 230, 230), 1, cv2.LINE_AA)
    cv2.putText(canvas, f"{name}  horizon: published vs raw (5-tick raw median)",
                (left, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (235, 235, 235), 1,
                cv2.LINE_AA)
    cv2.putText(canvas, "time (s)", (left, 706), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (180, 180, 180), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), canvas)


def render_mad(name: str, rows: list[dict], frames: list[dict], out_dir: Path):
    if not frames:
        return []
    stage1 = []
    prev_stage = None
    for r in rows:
        st = r.get("stage_id")
        if prev_stage is None:
            prev_stage = st
        if st != prev_stage:
            break
        stage1.append(r)
    inc, prev = [], None
    for r in stage1:
        mk = r.get("dgeo_mad_skips")
        if mk is None:
            continue
        if prev is not None and mk > prev:
            inc.append((r, mk - prev))
        prev = mk
    if not inc:
        return []
    idx = np.linspace(0, len(inc) - 1, 9).astype(int)
    tiles = []
    for i in idx:
        row, delta = inc[int(i)]
        entry = frame_at_fid(frames, row["fid"])
        if entry is None:
            continue
        img = cv2.imread(str(frame_path(name, entry)))
        if img is None:
            continue
        img = cv2.resize(img, (W, H))
        t_rel = (row["ts_mono_ns"] - stage1[0]["ts_mono_ns"]) / 1e9
        cap = f"t+{t_rel:.1f}s fid {row['fid']} mad+{delta}"
        cv2.rectangle(img, (0, 0), (W - 1, H - 1), (0, 0, 220), 4)
        box = img.copy()
        cv2.rectangle(box, (4, 4), (620, 80), (0, 0, 0), -1)
        img[:] = cv2.addWeighted(box, 0.55, img, 0.45, 0)
        for j, text in enumerate((cap,
                                  f"mad_total {row.get('dgeo_mad_skips')}  "
                                  f"state {row.get('state')}  v_ego {row.get('v_ego')}",
                                  f"n_lines {(row.get('lane_obs') or {}).get('n_lines')}  "
                                  f"cam_h_lane {(row.get('lane_obs') or {}).get('cam_h_lane')}")):
            cv2.putText(img, text, (10, 26 + j * 22), cv2.FONT_HERSHEY_SIMPLEX,
                        0.58, (235, 235, 235), 1, cv2.LINE_AA)
        tiles.append((img, cap))
    montage(tiles, out_dir / "mad_montage.jpg")
    return [(r["fid"], d) for r, d in inc]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=SESSIONS + ("all",), default="all")
    args = ap.parse_args()
    names = SESSIONS if args.session == "all" else (args.session,)
    for name in names:
        rows, frames = load_session(name)
        out_dir = OUT / name
        out_dir.mkdir(parents=True, exist_ok=True)
        seg = render_anomaly(name, rows, frames, out_dir)
        print(f"{name}: frames={len(frames)} anomaly_rows={0 if seg is None else len(seg)}")
        if name == "20261113_212838":
            inc = render_mad(name, rows, frames, out_dir)
            print(f"{name}: mad_increments={len(inc)}")
        print(f"  -> {out_dir}")


if __name__ == "__main__":
    main()
