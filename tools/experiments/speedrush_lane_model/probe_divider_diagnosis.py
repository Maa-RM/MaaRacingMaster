#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 分解诊断 v2：白色虚线分界线到底死在哪一层。

v1 教训（2026-11-11 当日自纠）：v1 把「最左/最右拟合轨迹」当黄缘对去定期望分界位置，
但黄昏下白色虚线常过黄色门，缘对认错后 ¼ 位置采样落空，读出「概率中位 0」的假象；
zoom 等值线直接证明模型对虚线段有 >0.30 的响应。v2 改为：
  ① 缘对只认「黄色门通过且跨度最大」的一对（认不出就如实跳过并计覆盖）；
  ② 响应读数不依赖几何假设：内部区域 prob>0.30 像素量 + 连通块（虚线段）数；
  ③ 死因分层：内部轨迹命运（拟合点数不足 / 质量门 / 颜色门 / 接受）+ 虚线间隙
     导致的轨迹碎片化读数（单带内点数分布）；
  ④ 接受线的图像分数位置聚簇（不假设 ¼ 等分，只验「位置稳定成簇」）。

自包含：只依赖 numpy / opencv / onnxruntime。跑法（仓库根）：
  .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_divider_diagnosis.py
输出：first_look_out/diagnosis/ 下 summary.md / summary.json / zoom_diagnosis.jpg。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
OUT = HERE / "first_look_out" / "diagnosis"

PROB_THRESH = 0.30
TRACK_TOL, TRACK_GAP = 13.0, 10
NEAR_Y = (560.0, 720.0)
FAR_Y = (440.0, 545.0)
MIN_PTS, MIN_SPAN = 4, 40
YELLOW_MIN = WHITE_MIN = 0.6
LINE_RMS_MAX = 3.0
LINE_THETA_MAX = 0.35
VPX, Y_H = 640.7, 354.2
LANE_W = 3.5
DEFAULT_CAM_H = 1.28
FOCAL_STANDIN = 807.7
LETTERBOX_PAD = 12
EDGE_MARGIN = 0.05          # 内部线判定：距两缘 5% 跨度以内不算内部
EDGE_MIN_SPAN_PX = 480.0    # 黄缘对在参考行的最小跨度（四车道应显著大于三车道）

MODELS = [
    ("plus_small(现役)", HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx", True),
    ("twinlitenet(原版)", HERE / "weights/twinlitenet_orig_360x640.onnx", False),
]


def sample_frames(raw: Path, n: int) -> list[Path]:
    fs = sorted(raw.glob("*.jpg")) or sorted(raw.glob("*.png"))
    trim = max(0, int(len(fs) * 0.10))
    fs = fs[trim:len(fs) - trim] if len(fs) > 20 else fs
    step = max(1, len(fs) // n)
    return fs[::step][:n]


def preprocess(rgb: np.ndarray, padded: bool) -> np.ndarray:
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    if padded:
        im = cv2.copyMakeBorder(im, LETTERBOX_PAD, LETTERBOX_PAD, 0, 0,
                                cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray(
        (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def lane_prob(sess: ort.InferenceSession, rgb: np.ndarray, padded: bool) -> np.ndarray:
    inp = sess.get_inputs()[0].name
    outs = [o.name for o in sess.get_outputs()]
    ll_name = next((n for n in outs if "ll" in n.lower()), outs[-1])
    logits = sess.run([ll_name], {inp: preprocess(rgb, padded)})[0][0]
    e = np.exp(logits - logits.max(0, keepdims=True))
    prob = (e[1] / e.sum(0)).astype(np.float32)
    if prob.shape[0] == 360 + 2 * LETTERBOX_PAD:
        prob = prob[LETTERBOX_PAD:LETTERBOX_PAD + 360]
    return prob


def row_segments(prob: np.ndarray, thresh: float = PROB_THRESH):
    rows = []
    for yc in range(206, 360, 3):
        row = prob[yc]
        idx = np.flatnonzero(row > thresh)
        if len(idx) == 0:
            continue
        splits = np.flatnonzero(np.diff(idx) > 1) + 1
        segs = []
        for seg in np.split(idx, splits):
            if len(seg) == 0:
                continue
            p = row[seg]
            segs.append({"yc": yc, "xc": float((seg * p).sum() / p.sum()),
                         "p": float(p.mean())})
        if segs:
            rows.append((yc, segs))
    return rows


def build_tracks(rows):
    tracks: list[dict] = []
    for yc, segs in rows:
        used = set()
        for tr in tracks:
            tr["_pred"] = None
            if yc - tr["yc"] > TRACK_GAP:
                continue
            tr["_pred"] = tr["xc"] + tr.get("v", 0.0) * (yc - tr["yc"])
        for seg in segs:
            best, best_d = None, TRACK_TOL
            for j, tr in enumerate(tracks):
                if j in used or tr["_pred"] is None:
                    continue
                d = abs(seg["xc"] - tr["_pred"])
                if d < best_d:
                    best, best_d = j, d
            if best is None:
                tracks.append({"pts": [seg], "yc": yc, "xc": seg["xc"], "v": 0.0})
                used.add(len(tracks) - 1)
            else:
                tr = tracks[best]
                dv = (seg["xc"] - tr["xc"]) / max(1, yc - tr["yc"])
                tr["v"] = 0.7 * tr.get("v", 0.0) + 0.3 * float(np.clip(dv, -8, 8))
                tr["pts"].append(seg)
                tr["yc"], tr["xc"] = yc, seg["xc"]
                used.add(best)
    return [t["pts"] for t in tracks]


def fit_line(pts, y_lo: float, y_hi: float):
    use = [q for q in pts if y_lo <= 2.0 * q["yc"] <= y_hi]
    if len(use) < MIN_PTS:
        return None
    ys = np.array([2.0 * q["yc"] for q in use])
    xs = np.array([2.0 * q["xc"] for q in use])
    wt = np.array([max(0.1, q["p"]) for q in use])
    if ys.max() - ys.min() < MIN_SPAN:
        return None
    A = np.vstack([ys, np.ones_like(ys)]).T
    s, a = np.linalg.lstsq(A * wt[:, None], xs * wt, rcond=None)[0]
    rms = float(np.sqrt(np.average((xs - (s * ys + a)) ** 2, weights=wt)))
    L = float(s * DEFAULT_CAM_H / LANE_W)
    theta = float(np.arctan((VPX - a - L * LANE_W * Y_H / DEFAULT_CAM_H)
                            / FOCAL_STANDIN))
    return {"s": float(s), "a": float(a), "rms": rms, "theta": theta,
            "y0": float(ys.min()), "y1": float(ys.max()), "n": len(ys)}


def line_color(hsv: np.ndarray, pts) -> tuple[float, float]:
    H, W = hsv.shape[:2]
    ny = nw = n = 0
    step = max(1, len(pts) // 12)
    for q in pts[::step]:
        x, y = int(round(2 * q["xc"])), int(round(2 * q["yc"]))
        if not (0 <= x < W and 0 <= y < H):
            continue
        win = hsv[max(0, y - 1):y + 2, max(0, x - 5):x + 6]
        h, s, v = (win[..., 0].astype(int), win[..., 1].astype(int),
                   win[..., 2].astype(int))
        n += 1
        if ((h >= 14) & (h <= 42) & (s >= 110) & (v >= 90)).any():
            ny += 1
        if ((s < 75) & (v >= 125)).any():
            nw += 1
    return (ny / n if n else 0.0), (nw / n if n else 0.0)


def pctl(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f, c = int(np.floor(k)), int(np.ceil(k))
    return xs[f] if f == c else xs[f] * (c - k) + xs[c] * (k - f)


REF_Y = 560.0


def analyze_frame(rgb: np.ndarray, prob: np.ndarray) -> dict:
    """单帧分解。缘对只认黄色门通过的一对；认不出时 fate/簇 读数照出、几何读数空缺。"""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    tracks = build_tracks(row_segments(prob))
    fitted = []
    for pts in tracks:
        f = fit_line(pts, *NEAR_Y) or fit_line(pts, *FAR_Y)
        yf, wf = line_color(hsv, pts)
        x_ref = (f["s"] * REF_Y + f["a"]) if f else \
            float(np.median([2.0 * q["xc"] for q in pts[-5:]]))
        fitted.append({"pts": pts, "f": f, "yf": yf, "wf": wf, "x_ref": x_ref})

    yellow = sorted((t for t in fitted if t["f"] is not None and t["yf"] >= YELLOW_MIN),
                    key=lambda t: t["x_ref"])
    edges, edge_note = None, ""
    if len(yellow) >= 2:
        cand = max(zip(yellow, yellow[1:]),
                   key=lambda p: p[1]["x_ref"] - p[0]["x_ref"])
        if cand[1]["x_ref"] - cand[0]["x_ref"] >= EDGE_MIN_SPAN_PX:
            edges = cand
    if edges is None:
        edge_note = "no_yellow_pair"

    out = {"edge_note": edge_note, "fate": {"fit": 0, "quality": 0, "color": 0,
           "accepted": 0}, "fracs": [], "band_pts": [], "trk_pts": [],
           "interior_px": None, "interior_blobs": None}
    if edges is None:
        return out
    left, right = edges
    span = right["x_ref"] - left["x_ref"]

    for t in fitted:
        if t is left or t is right:
            continue
        frac = (t["x_ref"] - left["x_ref"]) / span
        if not (EDGE_MARGIN < frac < 1 - EDGE_MARGIN):
            continue
        if t["f"] is None:
            out["fate"]["fit"] += 1
        elif t["f"]["rms"] > LINE_RMS_MAX or abs(t["f"]["theta"]) > LINE_THETA_MAX:
            out["fate"]["quality"] += 1
        elif max(t["yf"], t["wf"]) < WHITE_MIN:
            out["fate"]["color"] += 1
        else:
            out["fate"]["accepted"] += 1
            out["fracs"].append(frac)
        if t["f"] is None:
            bands = (sum(1 for q in t["pts"] if NEAR_Y[0] <= 2 * q["yc"] <= NEAR_Y[1]),
                     sum(1 for q in t["pts"] if FAR_Y[0] <= 2 * q["yc"] <= FAR_Y[1]))
            out["band_pts"].append(max(bands))
            out["trk_pts"].append(len(t["pts"]))

    # 内部区域响应（不依赖 ¼ 假设）：prob>0.30 的像素量与连通块数
    mask = (prob > PROB_THRESH)
    col0 = int(max(0, (left["x_ref"] + 0.03 * span) / 2))
    col1 = int(min(640, (right["x_ref"] - 0.03 * span) / 2))
    if col1 > col0:
        sub = mask[206:360, col0:col1]
        out["interior_px"] = int(sub.sum())
        n, _ = cv2.connectedComponents(sub.astype(np.uint8), connectivity=8)
        out["interior_blobs"] = int(n - 1)
    return out


def zoom_cell(rgb: np.ndarray, prob: np.ndarray) -> np.ndarray:
    base = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)[360:720]
    for val, col in ((0.15, (0, 0, 255)), (0.30, (0, 255, 0))):
        m = (prob > val).astype(np.uint8) * 255
        m = cv2.resize(m, (1280, 720), interpolation=cv2.INTER_NEAREST)[360:720]
        cnts, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(base, cnts, -1, col, 1)
    return base


def cluster(fracs: list[float], tol: float = 0.06) -> list[float]:
    """一维聚簇（排序后相邻差 > tol 切开），回每簇中位。"""
    if not fracs:
        return []
    xs = sorted(fracs)
    groups, cur = [], [xs[0]]
    for v in xs[1:]:
        if v - cur[-1] <= tol:
            cur.append(v)
        else:
            groups.append(cur)
            cur = [v]
    groups.append(cur)
    return [round(float(np.median(g)), 3) for g in groups if len(g) >= 3]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-session", type=int, default=24)
    args = ap.parse_args()

    root = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
    sessions = []
    for d in sorted(root.iterdir(), reverse=True):
        raw = d / "raw"
        if raw.is_dir() and any(raw.iterdir()):
            sessions.append((d.name, raw))
        if len(sessions) >= 2:
            break

    OUT.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    zooms: list[np.ndarray] = []
    zoom_tags: list[str] = []

    for name, path, padded in MODELS:
        sess = ort.InferenceSession(str(path),
                                    providers=["CPUExecutionProvider"])
        for skey, raw in sessions:
            frames = sample_frames(raw, args.per_session)
            agg = {"n": 0, "n_pair": 0, "fate": {"fit": 0, "quality": 0,
                   "color": 0, "accepted": 0},
                   "band_pts": [], "trk_pts": [], "fracs": [],
                   "interior_px": [], "interior_blobs": []}
            for i, fp in enumerate(frames):
                bgr = cv2.imread(str(fp))
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                prob = lane_prob(sess, rgb, padded)
                r = analyze_frame(rgb, prob)
                agg["n"] += 1
                if r["edge_note"]:
                    continue
                agg["n_pair"] += 1
                for k in agg["fate"]:
                    agg["fate"][k] += r["fate"][k]
                agg["band_pts"].extend(r["band_pts"])
                agg["trk_pts"].extend(r["trk_pts"])
                agg["fracs"].extend(r["fracs"])
                if r["interior_px"] is not None:
                    agg["interior_px"].append(r["interior_px"])
                    agg["interior_blobs"].append(r["interior_blobs"])
                if i in (4, 12) and len(zooms) < 8:
                    zooms.append(zoom_cell(rgb, prob))
                    zoom_tags.append(f"{name} {skey} fid={fp.stem}")
            summary.setdefault(name, {})[skey] = {
                "frames": agg["n"],
                "frames_with_yellow_pair": agg["n_pair"],
                "interior_fate": agg["fate"],
                "frag_best_band_pts_med": round(float(np.median(agg["band_pts"])), 1)
                if agg["band_pts"] else None,
                "frag_track_pts_med": round(float(np.median(agg["trk_pts"])), 1)
                if agg["trk_pts"] else None,
                "interior_px_med": round(float(np.median(agg["interior_px"])), 1)
                if agg["interior_px"] else None,
                "interior_blobs_med": round(float(np.median(agg["interior_blobs"])), 1)
                if agg["interior_blobs"] else None,
                "accepted_frac_clusters": cluster(agg["fracs"]),
            }
            print(f"[{skey}] {name}: 黄缘对帧={agg['n_pair']}/{agg['n']} "
                  f"命运={agg['fate']} 内部px中位={summary[name][skey]['interior_px_med']} "
                  f"虚线块中位={summary[name][skey]['interior_blobs_med']} "
                  f"单带点数中位={summary[name][skey]['frag_best_band_pts_med']} "
                  f"位置簇={summary[name][skey]['accepted_frac_clusters']}")

    if zooms:
        cells = []
        for im, t in zip(zooms, zoom_tags):
            bar = np.full((24, 1280, 3), 20, np.uint8)
            cv2.putText(bar, t, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                        (255, 255, 255), 1)
            cells += [bar, im]
        cv2.imwrite(str(OUT / "zoom_diagnosis.jpg"),
                    np.vstack(cells), [cv2.IMWRITE_JPEG_QUALITY, 88])

    md = ["# P0 分解诊断 v2：白色虚线死在哪一层", "",
          "缘对只认黄色门通过的一对（v1 教训：最左右轨迹在黄昏下常是白虚线，几何尺会认错）；",
          "响应读数不依赖 ¼ 等分假设（透视下等间距不成等分数）。", ""]
    for name, bysess in summary.items():
        md.append(f"## {name}")
        for skey, d in bysess.items():
            md.append(
                f"- **{skey}**：黄缘对帧 {d['frames_with_yellow_pair']}/{d['frames']}；"
                f"内部轨迹命运 {d['interior_fate']}；内部 prob>{PROB_THRESH} 像素中位 "
                f"{d['interior_px_med']}、虚线块中位 {d['interior_blobs_med']}；"
                f"未成线轨迹的单带点数中位 {d['frag_best_band_pts_med']}"
                f"（拟合需 ≥{MIN_PTS}）、整轨点数中位 {d['frag_track_pts_med']}；"
                f"接受线的分数位置簇 {d['accepted_frac_clusters']}")
        md.append("")
    (OUT / "summary.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(f"输出：{OUT}")


if __name__ == "__main__":
    main()
