"""路缘读数图像校验：把「这一侧离车多远」画回原始画面，肉眼核对。

为什么要有它
------------
路心（左右边界的合成量）经常不可信，但「某一侧离车多远」是点云里直接量出来
的。本探针只校验后者：把 trace 里记录的侧缘横位（道）按相机几何投影成画面里
的一条线，叠在原始帧上，看它是否贴住真实的护栏 / 边线。

投影（同 depth_geo 的口径）：地面点 (X, Z) → 像素
    y = y_v + fy·cam_h / Z
    x = cx + fy·X / Z
消去 Z 得 x(y) = cx + X·(y − y_v)/cam_h ——横向位置固定时，在画面上是一条直线。
所以「离车 X 米」的边界就是这条线，不需要逐行反解。

用法::

    .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_edge_overlay.py
    ... --session 20261113_083813 --side right --n 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import cv2

_APPDATA = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
LOG_ROOT = os.path.join(_APPDATA, "MaaRacingMaster", "logs")
DEBUG_ROOT = os.path.join(_APPDATA, "MaaRacingMaster", "debug")
LANE_W_M = 3.5
RED = (0, 0, 255)
FAR = (255, 0, 255)
NEAR = (0, 255, 255)


def load_trace(session: str) -> dict[int, dict]:
    path = os.path.join(LOG_ROOT, session, "trace", "speedrush.jsonl")
    out: dict[int, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") is None:
                out[int(d["fid"])] = d
    return out


def load_frames(session: str) -> list[dict]:
    path = os.path.join(DEBUG_ROOT, session, "speedrush", "drive",
                        "frames.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def draw_edge(img, lanes: float, y_v: float, cam_h: float,
              color=RED, thick: int = 2) -> None:
    h, w = img.shape[:2]
    cx = w / 2.0
    pts = []
    for y in range(int(y_v) + 8, h, 3):
        x = int(cx + (lanes * LANE_W_M) * (y - y_v) / cam_h)
        if 0 <= x < w:
            pts.append((x, y))
    for a, b in zip(pts, pts[1:]):
        cv2.line(img, a, b, color, thick)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="20261113_083813")
    ap.add_argument("--side", choices=("left", "right"), default="left")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(
        os.environ.get("TEMP", "."), "edge_overlay"))
    args = ap.parse_args()

    trace = load_trace(args.session)
    frames = load_frames(args.session)
    key = "dgeo_left" if args.side == "left" else "dgeo_right"
    picked = []
    for f in frames:
        t = trace.get(int(f["fid"]))
        if not t or t.get(key) is None or not t.get("dgeo_new"):
            continue
        if t.get("dgeo_sides") != 1:      # 只看单侧可读的拍：那才是要验证的构型
            continue
        picked.append((f, t))
    if not picked:
        print("没有单侧可读的帧")
        return 1
    step = max(1, len(picked) // args.n)
    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(DEBUG_ROOT, args.session, "speedrush", "drive")
    for i, (f, t) in enumerate(picked[::step][: args.n]):
        img = cv2.imread(os.path.join(base, f["file"].replace("/", os.sep)))
        if img is None:
            continue
        y_v = float(t["dgeo_yv"])
        cam_h = float(t["dgeo_cam_h"])
        lanes = float(t[key])
        draw_edge(img, lanes, y_v, cam_h, RED, 2)
        draw_edge(img, lanes - 0.5, y_v, cam_h, FAR, 1)
        draw_edge(img, lanes + 0.5, y_v, cam_h, NEAR, 1)
        cv2.putText(img, f"fid{f['fid']} {args.side}={lanes:+.2f} lane",
                    (12, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
        out = os.path.join(args.out, f"{args.side}_{f['fid']}.png")
        cv2.imwrite(out, img)
        print(f"  fid{f['fid']} {args.side}={lanes:+.2f} 道 → {out}")
    print(f"\n共 {len(picked)} 帧单侧可读，抽样 {min(args.n, len(picked))} 帧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
