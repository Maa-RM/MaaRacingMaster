# -*- coding: utf-8 -*-
"""一次校准 vs 逐帧单点锚：相对深度模型的绝对米制到底需不需要每帧重标？

**问题**：slim-SPF 是 affine-invariant 相对深度（V = A + B/z）。如果相机、FOV、
机位高、平路都固定，能否只在标定帧上定一次 `A/B`，之后直接用？

**做法**（撞出租车段，MoGe 基线作参照）：
- 逐帧全拟合：`V = A + m·(r−y_h) + a·u`，`z = m·k/(V−A−a·u)`；
- 一次校准：取全段 `m0/A0` 中位，之后不再改；
- 单点锚：`m0` 固定，用**车道线在固定行的一个地面点**逐帧解 `A`：
  `A = V_line − m0·(y_line−y_h) − a0·u_line`；
- 比三条序列与 MoGe 基线的偏差，并统计 `m`（尺度）与 `A`（偏移）的帧间漂移。

输出 `calib_once_out/calib_once.json` 与逐帧表。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "calib_once_out"
JSONL = ROOT / "prod_sim_out" / "prod_sim.json"
CAM_H = 1.25
FOCAL = 670.7          # 三锚收敛值；绝对尺度口径未裁决前用 670
Y_ANCHOR = 650.0       # 车道线锚点行（近场，线大概率在场）


def lane_anchor_point(lane_sess, rgb, yh, v):
    """取最靠近画面中心的车道线在 Y_ANCHOR 行的 (u, V) 作为地面样本。"""
    prob = lg.lane_prob(lane_sess, rgb)
    fits = [f for f in (lg.fit_line(q) for q in lg.tracks(prob)) if f]
    if not fits:
        return None
    best = min(fits, key=lambda f: abs(f[0] * Y_ANCHOR + f[1] - 640.0))
    x = best[0] * Y_ANCHOR + best[1]
    if not 0 <= x < v.shape[1]:
        return None
    xi, yi = int(round(x)), int(round(Y_ANCHOR))
    patch = v[max(0, yi - 2):yi + 3, max(0, xi - 3):xi + 4]
    if patch.size == 0:
        return None
    return xi / v.shape[1], float(np.median(patch))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    d = json.loads(JSONL.read_text(encoding="utf-8"))
    rows = [r for r in d["rows"] if r.get("box") and r.get("moge_z")]
    files = sorted((ROOT / "frames").glob("*_raw.jpg"))
    da = sim.session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    lane_sess = sim.session("lane/plus_small_384x640.onnx")

    recs = []
    for r in rows:
        p = ROOT / "frames" / r["file"]
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        out = da.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
        v = sim.resize_ac(out.astype(np.float64), 720, 1280)
        yh = r["yh_used"]
        coef = sim.ground_fit(v, yh)
        A, m, a = float(coef[2]), float(coef[0]), float(coef[1])
        box = r["box"]
        vmed, unorm = sim.band_median(v, box)
        ap = lane_anchor_point(lane_sess, rgb, yh, v)
        recs.append({"file": r["file"], "moge": r["moge_z"], "A": A, "m": m,
                     "a": a, "yh": yh, "vmed": vmed, "u": unorm,
                     "anchor": None if ap is None else {"u": ap[0], "v": ap[1]}})

    A0 = float(np.median([x["A"] for x in recs]))
    m0 = float(np.median([x["m"] for x in recs]))
    a0 = float(np.median([x["a"] for x in recs]))
    k = FOCAL * CAM_H

    def z_of(vmed, u, A):
        den = vmed - A - a0 * u
        return (m0 * k / den) if den > 1e-6 else None

    for x in recs:
        x["z_full"] = (x["m"] * k / (x["vmed"] - x["A"] - x["a"] * x["u"])
                       if x["vmed"] - x["A"] - x["a"] * x["u"] > 1e-6 else None)
        x["z_once"] = z_of(x["vmed"], x["u"], A0)
        x["z_anchor"] = None
        if x["anchor"] is not None:
            Aa = (x["anchor"]["v"] - m0 * (Y_ANCHOR - x["yh"])
                  - a0 * x["anchor"]["u"])
            x["A_anchor"] = Aa
            x["z_anchor"] = z_of(x["vmed"], x["u"], Aa)

    def err(z):
        return (abs(z - x["moge"]) / x["moge"] * 100.0
                if z and x["moge"] else None)

    for x in recs:
        x["err_full"] = err(x["z_full"])
        x["err_once"] = err(x["z_once"])
        x["err_anchor"] = err(x["z_anchor"])
        print(x["file"], "moge", round(x["moge"], 1),
              "full", None if x["z_full"] is None else round(x["z_full"], 1),
              "once", None if x["z_once"] is None else round(x["z_once"], 1),
              "anchor", None if x["z_anchor"] is None else round(x["z_anchor"], 1),
              "| A", round(x["A"], 3), "m", round(x["m"], 5),
              "A_anchor", None if "A_anchor" not in x else round(x["A_anchor"], 3))

    def med(vals):
        vals = [x for x in vals if x is not None]
        return float(np.median(vals)) if vals else None

    summary = {
        "A_median": A0, "m_median": m0,
        "A_range": (min(x["A"] for x in recs), max(x["A"] for x in recs)),
        "m_range": (min(x["m"] for x in recs), max(x["m"] for x in recs)),
        "m_iqr_pct": float((np.percentile([x["m"] for x in recs], 75)
                            - np.percentile([x["m"] for x in recs], 25))
                           / m0 * 100.0),
        "err_full_median_pct": med([x["err_full"] for x in recs]),
        "err_once_median_pct": med([x["err_once"] for x in recs]),
        "err_anchor_median_pct": med([x["err_anchor"] for x in recs]),
        "n": len(recs),
    }

    # 跨段扫描：一次校准的 A/m 能不能跨场景迁移
    all_frames = sorted((ROOT / "frames").glob("*_raw.jpg"))
    for sub in sorted((ROOT / "frames").iterdir()):
        if sub.is_dir():
            all_frames.extend(sorted(sub.glob("*_raw.jpg")))
    scan = []
    for p in all_frames:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        g = lg.geometry(lg.lane_prob(lane_sess, rgb))
        yh = g["y_v"] if g["y_v"] is not None else 354.2
        out = da.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
        v = sim.resize_ac(out.astype(np.float64), 720, 1280)
        coef = sim.ground_fit(v, yh)
        seg = "crash" if p.parent == ROOT / "frames" else p.parent.name
        scan.append({"file": p.name, "seg": seg, "yh": yh,
                     "A": float(coef[2]), "m": float(coef[0]),
                     "a": float(coef[1])})
    segs: dict[str, list] = {}
    for x in scan:
        segs.setdefault(x["seg"], []).append(x)
    seg_summary = {k: {"n": len(v),
                       "A_median": float(np.median([q["A"] for q in v])),
                       "A_iqr": float(np.percentile([q["A"] for q in v], 75)
                                      - np.percentile([q["A"] for q in v], 25)),
                       "m_median": float(np.median([q["m"] for q in v])),
                       "m_iqr_pct": float(
                           (np.percentile([q["m"] for q in v], 75)
                            - np.percentile([q["m"] for q in v], 25))
                           / max(np.median([q["m"] for q in v]), 1e-9) * 100)}
                  for k, v in segs.items()}
    summary["cross_segment"] = {
        "A_median_range": (min(s["A_median"] for s in seg_summary.values()),
                           max(s["A_median"] for s in seg_summary.values())),
        "m_median_range": (min(s["m_median"] for s in seg_summary.values()),
                           max(s["m_median"] for s in seg_summary.values())),
        "segments": seg_summary,
    }

    (OUT / "calib_once.json").write_text(
        json.dumps({"summary": summary, "rows": recs, "scan": scan},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
