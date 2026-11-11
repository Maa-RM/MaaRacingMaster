# -*- coding: utf-8 -*-
"""弯道/偏航下地平线与车道几何的鲁棒性。

**要回答**：框底行测距 ``z = fy·cam_h/(y_bot − y_h)`` 只依赖俯仰（地平线行），
不依赖偏航；但弯道的路面横坡（bank）、坡度与车道线拟合误差可能把 ``y_h`` 带偏。
本探针在 32 段历史驾驶的抽帧上同时算：

1. 灭点行 ``y_v``（全部线对交点中位）；
2. 自车道对的曲率强度（两条线各拟合二次项，中心曲率 / 车道宽）；
3. 线对交点的离散度（弯道下不同位置的线方向不同，交点会散）。

按曲率强度分桶看 ``y_v`` 的漂移：若弯道帧的 ``y_v`` 与直道同分布，固定地平线可用；
若随曲率系统偏移，则上产必须逐帧估计地平线。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, str(Path(__file__).parent))
import probe_lane_geometry as lg  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "curve_horizon_out"
POOL = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug")
WEIGHTS = ROOT / "weights" / "lane" / "plus_small_384x640.onnx"
YH0 = 361.12
PER_DRIVE = 18
Y_MID = 520.0


def sample() -> list[Path]:
    out: list[Path] = []
    for d in sorted(POOL.glob("*/speedrush/drive/*")):
        fs = sorted((d / "raw").glob("*_raw.jpg"))
        if not fs:
            continue
        idx = np.linspace(0, len(fs) - 1, min(PER_DRIVE, len(fs))).astype(int)
        out.extend(fs[i] for i in sorted(set(int(v) for v in idx)))
    return out


def fit_quad(pts):
    a = np.asarray(pts, np.float64)
    if len(a) < lg.MIN_TRACK:
        return None
    y, x = a[:, 1], a[:, 0]
    if y.max() - y.min() < 40.0:
        return None
    yc = (y - Y_MID) / 100.0
    A = np.stack([yc * yc, yc, np.ones_like(y)], 1)
    c = np.linalg.lstsq(A, x, rcond=None)[0]
    rms = float(np.sqrt(np.mean((A @ c - x) ** 2)))
    if rms > lg.MAX_RMS:
        return None
    return c, rms


def analyze(prob) -> dict:
    tracks = lg.tracks(prob)
    fits = [f for f in (lg.fit_line(p) for p in tracks) if f]
    if len(fits) < 2:
        return {"lines": len(fits)}
    yv = lg.vanish_row(fits)
    if yv is None:
        return {"lines": len(fits)}
    xs = sorted((s * 620.0 + a, i) for i, (s, a, _, _) in enumerate(fits))
    pair = None
    for (x0, i0), (x1, i1) in zip(xs, xs[1:]):
        if x0 <= 640.0 <= x1 and x1 - x0 >= 80.0:
            pair = (i0, i1)
            break
    if pair is None:
        return {"lines": len(fits), "y_v": yv, "yv_spread": 0.0, "curv": None}
    i0, i1 = pair
    q0 = fit_quad(tracks[i0])
    q1 = fit_quad(tracks[i1])
    curv = None
    c0 = c1 = None
    if q0 and q1:
        c0, c1 = q0[0], q1[0]
        w520 = abs(c1[2] - c0[2])          # yc=0 处的车道宽
        curv = 0.5 * (c0[0] + c1[0]) / max(1.0, w520)
    return {"lines": len(fits), "y_v": yv, "yv_spread": 0.0,
            "curv": None if curv is None else float(curv),
            "lin0": None if c0 is None else [float(c0[1]), float(c0[2])],
            "lin1": None if c1 is None else [float(c1[1]), float(c1[2])]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(str(WEIGHTS), sess_options=so,
                                providers=["DmlExecutionProvider",
                                           "CPUExecutionProvider"])
    frames = sample()
    print(f"sample {len(frames)}")
    recs = []
    for i, p in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        r = analyze(lg.lane_prob(sess, rgb))
        r["file"] = str(p.relative_to(POOL))
        recs.append(r)
        if (i + 1) % 60 == 0:
            print(f"{i + 1}/{len(frames)}")
    ok = [r for r in recs if r.get("y_v") is not None]
    base = float(np.median([r["y_v"] for r in ok]))
    buckets = {"<0.5%": [], "0.5~1.5%": [], ">1.5%": [], "no_curv": []}
    for r in ok:
        c = r.get("curv")
        if c is None:
            buckets["no_curv"].append(r["y_v"])
            continue
        a = abs(c)
        key = "<0.5%" if a < 0.005 else ("0.5~1.5%" if a < 0.015 else ">1.5%")
        buckets[key].append(r["y_v"])
    summary = {"n_sample": len(frames), "n_ok": len(ok),
               "y_v_median": base,
               "y_v_rel_median_pct": float(np.median(
                   [abs(r["y_v"] - YH0) / (620.0 - YH0) * 100 for r in ok])),
               "buckets": {}}
    for k, v in buckets.items():
        if not v:
            summary["buckets"][k] = None
            continue
        a = np.asarray(v)
        summary["buckets"][k] = {
            "n": len(v), "y_v_med": float(np.median(a)),
            "y_v_p10_p90": [float(np.percentile(a, 10)), float(np.percentile(a, 90))],
            "dev_med_px": float(np.median(np.abs(a - base)))}
    # 曲率与地平线偏差的相关性
    pairs = [(r["curv"], r["y_v"] - base) for r in ok if r.get("curv") is not None]
    if len(pairs) > 20:
        c = np.asarray([p[0] for p in pairs])
        d = np.asarray([p[1] for p in pairs])
        summary["corr_curv_dy"] = float(np.corrcoef(c, d)[0, 1])
    (OUT / "curve_horizon.json").write_text(
        json.dumps({"summary": summary, "frames": recs}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
