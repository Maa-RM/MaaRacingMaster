# -*- coding: utf-8 -*-
"""极限超车横幅事件回看：判定前后 1.2 s 的现场与横向距离。

**为什么用横幅不用游戏日志**：横幅 OCR（`hud/hud.jsonl` 的 `event_banner`）
已经按 0.5 s 采样记录事件更新，帧与 HUD 同钟；日志只多一个时刻、不带目标
身份，因此以横幅为事件源，往前回看即可。

**画什么**（每个事件 5 帧，事件前 1.0/0.6/0.3 s、事件、事件后 0.2 s）：
- 全帧：目标框、框中心、目标所在车道的中心刻度（青色）与自车轴（品红）；
- 右侧 BEV：自车（原点）、目标（x, z）、横向阈值带（±thr）、近 1 s 轨迹；
- 文字：纵深几何 z、相机系横向 x、车道尺子横向距、两者的差。

**要回答**：判定时刻目标已到身后/不可见时，应该用哪一帧、哪个量来判横向距——
相机系 x（依赖 z）还是车道尺子 x（不依赖 z），以及提前量要多少。

输出 `overtake_scene_out/<session>/`（实验产物，gitignored）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "speedrush_depthart_prod"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
import probe_adjacent_seq as P  # noqa: E402
import probe_near_deform as ND  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402
import probe_lane_geometry as lg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import (  # noqa: E402
    load_calib, x_lane_of)

ROOT = Path(__file__).parent
OUT = ROOT / "overtake_scene_out"
DEBUG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "debug" / "speedrush"
LANE_W = 3.5
THR_DEFAULT = 2.7
BANNER = "极限超车"


def banner_events(sess: Path) -> list[int]:
    """横幅更新时刻：连击数变化即一次新事件；横幅消失后重新起算。"""
    path = sess / "hud" / "hud.jsonl"
    if not path.exists():
        return []
    last_combo, out = 0, []
    for ln in open(path, encoding="utf-8"):
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        b = (r.get("fields") or {}).get("event_banner") or {}
        txt = (b.get("text") or "").strip()
        if BANNER not in txt:
            last_combo = 0
            continue
        m = re.search(r"[×xX]\s*(\d+)", txt)
        combo = int(m.group(1)) if m else 1
        if combo != last_combo:
            if not out or r["ts_ns"] - out[-1] > 250_000_000:
                out.append(r["ts_ns"])
            last_combo = combo
    return out


def lateral_metrics(box, cal=None):
    """内侧边表面间隙：A1 2D 尺子，不依赖纵深。

    返回 (gap_m, center2d_m, inner_x, anchor_row)。内侧边=朝自车那一侧的框边；
    外侧被裁不影响它。gap = 内侧边横向 − 自车半宽（0.95 m）。
    """
    cal = load_calib() if cal is None else cal
    x0, y0, x1, y1 = box
    cx = 0.5 * (x0 + x1)
    cy = 0.5 * (y0 + y1)
    anchor = cy if cy - cal.y_h >= cal.min_denom else float(y1)
    xl = x_lane_of(cx, anchor, cal)
    inner = x1 if cx < cal.vpx else x0
    edge = x_lane_of(inner, anchor, cal)
    gap = abs(edge) * cal.lane_w_m - 0.95
    return gap, abs(xl) * cal.lane_w_m, inner, anchor


def lane_centers(prob: np.ndarray, row: float):
    """一行上的车道中心与道宽（全幅像素）。"""
    fits = [f for f in (lg.fit_line(t) for t in lg.tracks(prob)) if f]
    if len(fits) < 2:
        return None
    xs = sorted(s * row + a for s, a, _, _ in fits)
    centers = [0.5 * (a + b) for a, b in zip(xs, xs[1:])]
    widths = [b - a for a, b in zip(xs, xs[1:])]
    ok = [(c, w) for c, w in zip(centers, widths) if 60.0 <= w <= 900.0]
    return ok or None


def lane_coord(cx: float, row: float, prob: np.ndarray):
    oc = lane_centers(prob, row)
    if not oc:
        return None
    centers = [c for c, _ in oc]
    i = int(np.argmin([abs(cx - c) for c in centers]))
    c, w = oc[i]
    return (cx - c) / w, i, w


def render_event(sess: Path, ts: int, det, lane) -> dict | None:
    with open(sess / "frames.jsonl", encoding="utf-8") as fh:
        recs = [json.loads(ln) for ln in fh if ln.strip()]
    frames = [r for r in recs if -0.2 <= (ts - r["ts_ns"]) / 1e9 <= 1.2]
    if not frames:
        return None
    frames.sort(key=lambda r: -(ts - r["ts_ns"]))  # 由近到远
    picked, last = [], None
    for r in frames:
        img = cv2.imread(str(sess / r["file"]))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        prob = lg.lane_prob(lane, rgb)
        fits = [f for f in (lg.fit_line(t) for t in lg.tracks(prob)) if f]
        yv = lg.vanish_row(fits) if len(fits) >= 2 else None
        yh = yv if yv is not None else 361.0
        cand = []
        for b in ND.detect_cars_full(det, rgb):
            z = P.geom_z(b, yh)
            if z is None or not (0.3 < z < 16):
                continue
            x = P.cam_x(b, z)
            if not (0.6 < abs(x) < 7.0):
                continue
            cand.append({"box": b, "z": z, "x": x, "h": b[3] - b[1]})
        if not cand:
            continue
        if last is None:
            b = max(cand, key=lambda c: c["h"])
        else:
            b = min(cand, key=lambda c: abs(c["box"][0] + c["box"][2]
                                            - last["box"][0] - last["box"][2])
                    + 2 * abs(c["box"][1] + c["box"][3]
                              - last["box"][1] - last["box"][3]))
            if abs(b["h"] - last["h"]) / max(1, last["h"]) > 0.8:
                continue
        last = b
        b["prob"] = prob
        b["yh"] = yh
        b["img"] = img
        b["gap"] = (ts - r["ts_ns"]) / 1e9
        picked.append(b)
    if not picked:
        return None
    return {"ts": ts, "rows": picked}


def draw_one(seq: dict, b: dict, idx: int, sess: Path) -> None:
    img = b["img"].copy()
    x0, y0, x1, y1 = b["box"]
    z, xz = b["z"], b["x"]
    w_m = (x1 - x0) * z / P.FY
    thr = 0.95 + w_m / 2 + 0.8
    cal = load_calib()
    cx = int((x0 + x1) / 2)
    gap, dist2d, inner_x, anchor = lateral_metrics(b["box"], cal)
    half_ego = 0.95
    color = ((0, 0, 255) if gap < 0.0
             else (0, 255, 0) if gap <= 0.8 else (255, 120, 0))
    cv2.rectangle(img, (x0, y0), (x1, y1), color, 2)
    cv2.line(img, (inner_x, y0), (inner_x, y1), (255, 255, 0), 2)
    cv2.circle(img, (cx, int(anchor)), 3, (0, 255, 255), -1)
    cv2.line(img, (cx, 0), (cx, 719), color, 1)
    lc = lane_coord(cx, float(y1), b["prob"])
    ego = lane_coord(P.VPX, 690.0, b["prob"])
    if lc and ego:
        off, ki, _ = lc
        eoff, ei, _ = ego
        d_lane = abs((ki + off) - (ei + eoff))
        cv2.line(img, (int(cx), y1 - 4), (int(cx), y1 + 4), (255, 255, 0), 2)
        cv2.putText(img, f"tgt off={off:+.2f} ego off={eoff:+.2f} "
                    f"center~{d_lane * LANE_W:.2f}m",
                    (12, 96), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2)
        b["center_m"] = d_lane * LANE_W
    elif lc:
        off, ki, _ = lc
        cv2.putText(img, f"lane off={off:+.2f}",
                    (12, 96), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2)
    cv2.putText(img, f"gap={b['gap']:+.1f}s z={z:.1f} x_cam={xz:+.2f}",
                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.putText(img, f"edge gap={gap:+.2f}m (thr 0.80)  "
                f"center2D={dist2d:.2f}m",
                (12, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (255, 255, 255), 2)
    cv2.putText(img, f"w_m={w_m:.2f} thr={thr:.2f}",
                (12, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    # BEV
    bev = np.full((520, 380, 3), 25, np.uint8)
    ox, oy, sc = 190, 470, 22.0  # 1 m = 22 px
    cv2.rectangle(bev, (ox - int(0.95 * sc), oy - int(2.2 * sc)),
                  (ox + int(0.95 * sc), oy + int(2.2 * sc)), (80, 80, 255), -1)
    for margin in (half_ego, half_ego + 0.8):
        for sgn in (-1, 1):
            xx = ox + int(sgn * margin * sc)
            cv2.line(bev, (xx, 40), (xx, 510), (0, 200, 255), 1)
    for r in seq["rows"]:
        px = ox + int(r["x"] * sc)
        py = oy - int(r["z"] * sc)
        cv2.circle(bev, (px, py), 2, (120, 200, 120), -1)
    px, py = ox + int(xz * sc), oy - int(z * sc)
    cv2.rectangle(bev, (px - int(w_m / 2 * sc), py - int(2.2 * sc)),
                  (px + int(w_m / 2 * sc), py + int(2.2 * sc)),
                  (0, 255, 0), 1)
    cv2.putText(bev, "ego / target / threshold", (8, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
    h = max(img.shape[0], bev.shape[0])
    canvas = np.full((h, img.shape[1] + bev.shape[1], 3), 30, np.uint8)
    canvas[:img.shape[0], :img.shape[1]] = img
    canvas[:bev.shape[0], img.shape[1]:] = bev
    name = f"{sess.name}_{seq['ts']}_{idx:02d}_g{b['gap']:+.1f}.png"
    cv2.imwrite(str(OUT / sess.name / name), canvas)
    return {"gap": b["gap"], "z": z, "x_cam": xz, "edge_gap": gap,
            "center2d": dist2d, "file": name}


def main() -> None:
    names = sys.argv[1:] or ["20261113_203839_manual_p1"]
    det = sim.session("prod_perception/model.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")
    for name in names:
        sess = DEBUG / name
        if not (sess / "hud" / "hud.jsonl").exists():
            print("skip", name)
            continue
        (OUT / name).mkdir(parents=True, exist_ok=True)
        evs = banner_events(sess)
        print(name, "banner events", len(evs))
        for ts in evs:
            seq = render_event(sess, ts, det, lane)
            if seq is None:
                continue
            rows = sorted(seq["rows"], key=lambda b: -b["gap"])
            keep = []
            for target in (-1.0, -0.6, -0.3, 0.0, 0.2):
                best = min(rows, key=lambda b: abs(b["gap"] - target))
                if best not in keep:
                    keep.append(best)
            for i, b in enumerate(keep):
                info = draw_one(seq, b, i, sess)
                print("  event", ts, {k: (round(v, 2) if isinstance(v, float)
                                          else v) for k, v in info.items()})
    print("saved:", OUT)


if __name__ == "__main__":
    main()
