"""车道线米制间距：用深度点云量「相邻两条线到底几米」，与标称 3.5m 对比。

问题
----
车道线网格的横位 L（道）是把像素位置除以「车道宽像素数」得到的，而车道宽
像素数来自相机几何（fy、cam_h、y_v）与标称车道宽 3.5m。如果标称值或几何
有偏，整张网格的绝对尺度就跟着偏——但它是自洽的，任何只依赖线的量都发现
不了。

深度给的是独立的米制读数：线画在路面上，深度图在线的像素上能给出 (X, Z)。
两条相邻线的 X 之差就是这条车道实际有多宽。本探针只量这个，不做任何修正。

用法::

    .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_lane_width_m.py
    ... --session 20261113_083813 --n 40
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

MODEL = ("maaracing_master/plugins/speedrush/resources/models/depth/"
         "relative_mnv4m_slim_spf_448_default.onnx")
LOG_ROOT = r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\logs"
DEBUG_ROOT = r"C:\Users\yomen\AppData\Roaming\MaaRacingMaster\debug"
LANE_W_M = 3.5
CX = 640.0


def load_trace(session: str) -> dict[int, dict]:
    path = os.path.join(LOG_ROOT, session, "trace", "speedrush.jsonl")
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") is None:
                out[int(d["fid"])] = d
    return out


def load_frames(session: str) -> list[dict]:
    path = os.path.join(DEBUG_ROOT, session, "speedrush", "drive",
                        "frames.jsonl")
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def line_xz(v: np.ndarray, gm, line: dict, y_v: float, fy: float,
            cam_h: float) -> tuple[float, float] | None:
    """沿线取点 → (X, Z) 拟合 X = p·Z + q，返回 (p, q)。"""
    m, a_c, A, _mad = gm
    fit = max(line.get("fits") or [], key=lambda f: f["y1"] - f["y0"],
              default=None)
    if fit is None:
        return None
    s, a = fit["s"], fit["a"]
    y0 = max(float(fit.get("y0") or 440), 470.0)
    y1 = min(float(fit.get("y1") or 700), 700.0)
    if y1 - y0 < 30:
        return None
    zs, xs = [], []
    h, w = v.shape
    for y in np.arange(y0, y1, 6.0):
        x = s * y + a
        if not (2 <= x < w - 2):
            continue
        yi, xi = int(y), int(x)
        val = float(np.median(v[max(0, yi - 1):yi + 2, max(0, xi - 1):xi + 2]))
        u = (x + 0.5) / w
        den = val - A - a_c * u
        if den <= 1e-4:
            continue
        z = fy * cam_h * m / den      # (r−y_v)=(V−A−a·u)/m → Z=fy·h/(r−y_v)
        if not (4.0 <= z <= 60.0):
            continue
        zs.append(z)
        xs.append((x - CX) * z / fy)
    if len(zs) < 8:
        return None
    p, q = np.polyfit(np.array(zs), np.array(xs), 1)
    return float(p), float(q)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="20261113_083813")
    ap.add_argument("--n", type=int, default=40)
    args = ap.parse_args()

    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(MODEL, sess_options=so,
                                providers=["CPUExecutionProvider"])
    trace = load_trace(args.session)
    frames = load_frames(args.session)
    base = os.path.join(DEBUG_ROOT, args.session, "speedrush", "drive")
    picked = []
    for f in frames:
        t = trace.get(int(f["fid"]))
        if not t or not t.get("dgeo_new"):
            continue
        ls = [l for l in ((t.get("lane_obs") or {}).get("lines") or [])
              if l.get("accepted") and l.get("k") is not None
              and l.get("fits")]
        if len(ls) < 2:
            continue
        picked.append((f, t, ls))
    if not picked:
        print("没有可用帧")
        return 1
    step = max(1, len(picked) // args.n)

    widths = []
    for f, t, ls in picked[::step][: args.n]:
        img = cv2.imread(os.path.join(base, f["file"].replace("/", os.sep)))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        out = sess.run(None, {sess.get_inputs()[0].name:
                              dg._da_preprocess(rgb)})[0][0, 0]
        v = dg._resize_align_corners(out, dg.FRAME_H, dg.FRAME_W)
        y_v = float(t.get("dgeo_yv") or 360.0)
        fy = float(t.get("dgeo_focal") or 630.0)
        cam_h = float(t.get("dgeo_cam_h") or 1.25)
        gm = dg._ground_model(v, y_v, None)
        if gm is None:
            continue
        fits = {}
        for l in ls:
            r = line_xz(v, gm, l, y_v, fy, cam_h)
            if r is not None:
                fits[int(l["k"])] = r
        for k in sorted(fits):
            if k + 1 not in fits:
                continue
            p1, q1 = fits[k]
            p2, q2 = fits[k + 1]
            z_ref = 15.0
            w_m = abs((p2 * z_ref + q2) - (p1 * z_ref + q1))
            if 2.0 <= w_m <= 6.0:
                widths.append(w_m)
                print(f"  fid{f['fid']} k{k}→k{k+1}  深度间距 {w_m:.2f} m "
                      f"(标称 {LANE_W_M})")
    if not widths:
        print("没有量到有效间距")
        return 1
    a = np.array(widths)
    print(f"\n样本 {len(a)}：深度米制车道宽 p10={np.percentile(a,10):.2f} "
          f"p50={np.median(a):.2f} p90={np.percentile(a,90):.2f} m")
    print(f"  相对标称 3.5m 的中位偏差 {np.median(a) - LANE_W_M:+.2f} m "
          f"({(np.median(a) / LANE_W_M - 1) * 100:+.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
