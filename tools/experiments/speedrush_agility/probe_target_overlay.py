"""目标切换事件的逐拍叠加图与蒙太奇。

读会话 trace 与已存的原始帧（debug/speedrush/*/frames.jsonl），在画面上叠加：
车道线拟合、选中目标道、命中车的地面标记，再把切换前后两帧拼成蒙太奇。

用法::

    .venv/Scripts/python.exe tools/experiments/speedrush_agility/probe_target_overlay.py \
        --session <会话编号>

产物写在同目录 ``_out/``，不写任何生产状态。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "_out"
ROOT = Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster"
LANE_W = 3.5
TILE_W, TILE_H = 640, 360
BAR_H = 46


def load_trace(session: str, run: str) -> list[dict]:
    """按 run 取记录并按时间排序（同一 fid 在不同 run 里数值不同，不能混排）。"""
    p = ROOT / "logs" / session / "trace" / "speedrush.jsonl"
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("sel_dbg") is None:
                continue
            if run and not str(r.get("run_id") or "").endswith(run):
                continue
            out.append(r)
    out.sort(key=lambda r: r.get("ts_ns") or 0)
    return out


def load_frames() -> dict[int, Path]:
    out: dict[int, Path] = {}
    base = ROOT / "debug" / "speedrush"
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        idx = d / "frames.jsonl"
        if not idx.is_file():
            continue
        with open(idx, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                out.setdefault(row["frame_id"], d / row["file"])
    return out


def line_fits(lo: dict) -> list[tuple[float, float, float]]:
    """(L, a, s)：拟合像素列 x_px = a + s*y。"""
    fits = []
    for ln in lo.get("lines") or []:
        if not ln.get("accepted"):
            continue
        f0 = (ln.get("fits") or [None])[0]
        if not f0:
            continue
        if abs(f0.get("s") or 0.0) < 1e-6:
            continue
        fits.append((float(ln["L"]), float(f0["a"]), float(f0["s"])))
    fits.sort()
    return fits


def px_at(fits: list[tuple[float, float, float]], lane: float,
          row: float) -> float | None:
    """把道号换算成该行的像素列（用相邻两条拟合线插值/外推）。"""
    if len(fits) < 2:
        return None
    for i in range(len(fits) - 1):
        l0, a0, s0 = fits[i]
        l1, a1, s1 = fits[i + 1]
        if l0 <= lane <= l1 and l1 - l0 > 1e-6:
            x0, x1 = a0 + s0 * row, a1 + s1 * row
            return x0 + (lane - l0) / (l1 - l0) * (x1 - x0)
    l0, a0, s0 = fits[0]
    l1, a1, s1 = fits[-1]
    if l1 - l0 < 1e-6:
        return a0 + s0 * row
    x0, x1 = a0 + s0 * row, a1 + s1 * row
    return x0 + (lane - l0) / (l1 - l0) * (x1 - x0)


def draw_tick(rec: dict, prev: dict | None) -> np.ndarray | None:
    fid = rec.get("fid")
    path = FRAMES.get(fid)
    if path is None:
        return None
    img = cv2.imread(str(path))
    if img is None:
        return None
    lo = rec.get("lane_obs") or {}
    sd = rec.get("sel_dbg") or {}
    fits = line_fits(lo)
    # 线的 L 以自车相机轴为零点；目标与车横位是路心系，画图前减自车横位。
    ego_lane = float(sd.get("ego") or 0.0)

    for lane, a, s in fits:
        cv2.line(img, (int(a + s * 330), 330), (int(a + s * 700), 700),
                 (80, 220, 80), 2)

    hit = sd.get("hit_oid")
    hold = sd.get("graze_hold") or [None, None]
    src = {c.get("id"): c for c in rec.get("carsrc") or []}
    pos_by_id = {c.get("id"): c for c in rec.get("car_pos") or []}
    for oid, color, tag in ((hit, (0, 0, 255), "hit"),
                            (hold[0], (0, 165, 255), "hold")):
        pos = pos_by_id.get(oid)
        if pos is None or not pos.get("cy"):
            continue
        # 目标吃米制横位：像素通道（car_pos.x）与米制可差 0.7 道，优先米制。
        lane = None
        if oid in src and src[oid].get("x") is not None:
            lane = float(src[oid]["x"]) / LANE_W
        elif pos.get("x") is not None:
            lane = float(pos["x"])
        if lane is None:
            continue
        x = px_at(fits, lane - ego_lane, float(pos["cy"]))
        if x is None or not (0 <= x < img.shape[1]):
            continue
        cv2.circle(img, (int(x), int(pos["cy"])), 14, color, 3)
        cv2.putText(img, f"{tag} {oid}", (int(x) + 16, int(pos["cy"]) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)

    tgt = sd.get("target")
    if tgt is not None:
        pts = []
        for row in range(340, 715, 20):
            x = px_at(fits, float(tgt) - ego_lane, row)
            if x is not None:
                pts.append((int(np.clip(x, -100, img.shape[1] + 100)), row))
        if len(pts) >= 2:
            cv2.polylines(img, [np.array(pts, np.int32)], False,
                          (255, 80, 255), 3)
            cv2.putText(img, f"target {tgt:+.2f}", (pts[0][0] + 8, 356),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 80, 255), 2)

    d = ""
    if prev is not None and prev.get("sel_dbg", {}).get("target") is not None \
            and tgt is not None:
        dt = float(tgt) - float(prev["sel_dbg"]["target"])
        d = f" dTgt={dt:+.2f}"
    cross = ""
    if hit is not None and hit in src and src[hit].get("x") is not None:
        cross = f" xM={float(src[hit]['x']) / LANE_W:+.2f}"
    unk = ""
    if hit in src:
        unk = f" unk={int(bool(src[hit].get('x_unknown')))}" \
              f" seen={int(bool(src[hit].get('seen')))}"
    cap = (f"fid {fid}  {rec.get('sel_why')}  layer {rec.get('sel_layer')}"
           f"{cross}{unk}{d}")
    tile = np.zeros((TILE_H + BAR_H, TILE_W, 3), np.uint8)
    tile[BAR_H:] = cv2.resize(img, (TILE_W, TILE_H))
    cv2.putText(tile, cap, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (255, 255, 255), 1)
    sub = (f"origin {sd.get('origin')}  ego {sd.get('ego')}"
           f"  hit {sd.get('hit_oid')}  hold {sd.get('graze_hold')}")
    cv2.putText(tile, sub, (6, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (200, 200, 200), 1)
    return tile


def montage(tiles: list[np.ndarray], path: Path, cols: int = 2) -> None:
    if not tiles:
        return
    rows = (len(tiles) + cols - 1) // cols
    blank = np.zeros((TILE_H + BAR_H, TILE_W, 3), np.uint8)
    grid = [[tiles[r * cols + c] if r * cols + c < len(tiles) else blank
             for c in range(cols)] for r in range(rows)]
    cv2.imwrite(str(path), np.vstack([np.hstack(r) for r in grid]))


def main() -> None:
    global FRAMES
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--run", default="r2",
                    help="run 后缀（存帧来自人工局 r2）")
    ap.add_argument("--max-events", type=int, default=6)
    args = ap.parse_args()

    recs = load_trace(args.session, args.run)
    FRAMES = load_frames()
    events: list[tuple[dict, dict]] = []
    prev = None
    for rec in recs:
        if prev is not None:
            gap = ((rec.get("ts_ns") or 0) - (prev.get("ts_ns") or 0)) / 1e9
            if gap <= 0.2:
                sa, sb = prev.get("sel_dbg") or {}, rec.get("sel_dbg") or {}
                ta, tb = sa.get("target"), sb.get("target")
                if ta is not None and tb is not None \
                        and abs(float(tb) - float(ta)) > 0.5 \
                        and prev.get("sel_why") == "pass" \
                        and rec.get("sel_why") == "pass" \
                        and sa.get("hit_oid") != sb.get("hit_oid") \
                        and prev.get("fid") in FRAMES and rec.get("fid") in FRAMES:
                    events.append((prev, rec))
        prev = rec
    step = max(1, len(events) // args.max_events)
    picked = events[::step][:args.max_events]

    tiles = []
    for a, b in picked:
        ta = draw_tick(a, None)
        tb = draw_tick(b, a)
        if ta is not None and tb is not None:
            tiles.extend([ta, tb])

    OUT.mkdir(parents=True, exist_ok=True)
    montage(tiles, OUT / "target_switch_montage.jpg", cols=2)
    print(f"events {len(picked)} tiles {len(tiles)} -> "
          f"{OUT / 'target_switch_montage.jpg'}")


FRAMES: dict[int, Path] = {}

if __name__ == "__main__":
    main()
