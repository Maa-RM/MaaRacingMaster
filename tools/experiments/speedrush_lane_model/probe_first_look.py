#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 首眼对比：候选车道线 ONNX 在最近对局录制帧上「谁又快又好」。

自包含：只依赖 numpy / opencv / onnxruntime，不 import maaracing_master 业务码（试验区硬约束）。
线提取语义逐项复刻生产 lane_obs.py（行扫描 → 轨迹 → 近/远拟合 → 颜色门 → 质量 gate），
只读帧、只出图与读数，不接任何生产链。

模型接口（2026-11-11 实测图接口）：
  plus_small_384x640.onnx   in [1,3,384,640]（640x360 内容 + 上下 12px 114 灰边），out da/ll [1,2,384,640]
  twinlitenet_orig_360x640.onnx in [1,3,360,640]（无填充直缩），out da/ll [1,2,360,640]
  通用规则：车道头 = 名字带 ll 的输出（退化取第 2 个输出）；softmax 取通道 1；
  输出行数 384 时裁掉上下 12px 填充行，360 时直接用。

跑法（仓库根）：
  .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_first_look.py
  可选：--sessions <逗号分隔的 raw 目录> --per-session 24 --threads 4 --provider cpu
输出：first_look_out/ 下 summary.md / summary.json / <会话>_sheet.jpg（行=模型、列=帧）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
WEIGHTS = HERE / "weights"
OUT = HERE / "first_look_out"

# ---- 生产口径常数（lane_obs.py 复刻，2026-11-11 与生产同源核对）----
PROB_THRESH = 0.30
ROW_LO, ROW_HI, ROW_STEP = 206, 360, 1     # 与生产一致：步长 1
# 生产 lane_obs._row_segments 自 2026-11-11 起步长为 1（原 3）：远处虚线在
# 概率图里只有 2~5 行高，步长 3 会漏掉，虚线轨迹凑不满拟合点数。
TRACK_TOL, TRACK_GAP = 13.0, 10
NEAR_Y = (560.0, 720.0)                    # 全幅坐标
FAR_Y = (440.0, 545.0)
MIN_PTS, MIN_SPAN = 4, 40                  # 拟合最少点数 / y 跨度(px)
YELLOW_MIN = WHITE_MIN = 0.6
LINE_RMS_MAX = 3.0
LINE_THETA_MAX = 0.35
VPX, Y_H = 640.7, 354.2                    # gate0 标定（真源：生产常数）
LANE_W = 3.5
DEFAULT_CAM_H = 1.28
FOCAL_STANDIN = 807.7                      # 线角焦距标定中位（probe 无逐帧深度，取标定值代用）
SIDE_DEADBAND_PX = 20.0
SIDE_NEAR_PTS = 5
LETTERBOX_PAD = 12                         # plus 系输入上下各 12px 灰边

MODELS = [
    # (显示名, 权重路径, 说明)
    ("plus_small(现役)", REPO / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx",
     "TwinLiteNet+ Small 官方 BDD100K 权重，项目内转换"),
    ("twinlitenet(原版)", WEIGHTS / "twinlitenet_orig_360x640.onnx",
     "原版 TwinLiteNet，harrylal/TwinLiteNet-onnxruntime 仓自带转换件（MIT）"),
]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_sessions() -> list[Path]:
    """默认帧源：应用数据 debug 根下最近两个带 raw/ 的会话（跳过首尾 10% 由采样负责）。"""
    root = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) \
        / "MaaRacingMaster" / "debug" / "speedrush"
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        raw = d / "raw"
        if raw.is_dir() and any(raw.iterdir()):
            out.append(raw)
        if len(out) >= 2:
            break
    if not out:
        raise SystemExit(f"找不到 raw 帧：{root}")
    return out


def sample_frames(raw: Path, n: int) -> list[Path]:
    fs = sorted(raw.glob("*.jpg")) or sorted(raw.glob("*.png"))
    if not fs:
        raise SystemExit(f"无帧：{raw}")
    trim = max(0, int(len(fs) * 0.10))          # 掐首尾 10%（UI/收尾帧）
    fs = fs[trim:len(fs) - trim] if len(fs) > 20 else fs
    step = max(1, len(fs) // n)
    return fs[::step][:n]


def preprocess(bgr: np.ndarray, padded: bool) -> np.ndarray:
    im = cv2.resize(bgr, (640, 360), interpolation=cv2.INTER_AREA)
    if padded:
        im = cv2.copyMakeBorder(im, LETTERBOX_PAD, LETTERBOX_PAD, 0, 0,
                                cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray(
        (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def lane_prob(sess: ort.InferenceSession, bgr: np.ndarray, padded: bool) -> np.ndarray:
    """bgr：cv2 读入的原始 BGR 帧，**不要转 RGB**。

    生产 lane_obs.py 的推理输入是 `(im.astype(f32)/255).transpose(2,0,1)`，
    im 直接来自 cv2.imread，即 BGR。此前本探针传的是 RGB，属于口径错误：
    现役模型对通道不敏感（目标区都是 0.002~0.003），但微调权重会差到
    0.116 vs 1.0，导致几何门结论完全反向（2026-11-12 复核）。
    """
    inp = sess.get_inputs()[0].name
    outs = [o.name for o in sess.get_outputs()]
    ll_name = next((n for n in outs if "ll" in n.lower()), outs[-1])
    logits = sess.run([ll_name], {inp: preprocess(bgr, padded)})[0][0]   # [2,H,W]
    e = np.exp(logits - logits.max(0, keepdims=True))
    prob = (e[1] / e.sum(0)).astype(np.float32)
    if prob.shape[0] == 360 + 2 * LETTERBOX_PAD:
        prob = prob[LETTERBOX_PAD:LETTERBOX_PAD + 360]                   # 裁掉填充行
    assert prob.shape == (360, 640), f"车道头输出形状异常：{prob.shape}"
    return prob


def row_segments(prob: np.ndarray) -> list[tuple[int, list[dict]]]:
    rows = []
    for yc in range(ROW_LO, ROW_HI, ROW_STEP):
        row = prob[yc]
        idx = np.flatnonzero(row > PROB_THRESH)
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


def build_tracks(rows) -> list[list[dict]]:
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


def near_side(pts) -> int | None:
    near = pts[-SIDE_NEAR_PTS:]
    if not near:
        return None
    x = float(np.median([2.0 * q["xc"] for q in near]))
    if abs(x - VPX) < SIDE_DEADBAND_PX:
        return None
    return -1 if x < VPX else 1


def extract_lines(bgr: np.ndarray, prob: np.ndarray) -> list[dict]:
    """生产 _infer 同语义：颜色门 → 近/远拟合 → 质量门，产出 accepted 线列表。

    bgr 口径与生产一致（颜色门按 BGR→HSV 判黄/白）。
    """
    lines = []
    for pts in build_tracks(row_segments(prob)):
        # 颜色不参与提取（生产 lane_obs._lines_from_prob 2026-11-11 起叫停）：
        # 黄昏下白色分界线系统性过不了白门、甚至被误判成黄线。此前探针保留
        # 颜色门，导致把「模型已看见但颜色不达标」的帧错记成漏检。
        f_near = fit_line(pts, *NEAR_Y)
        f = f_near or fit_line(pts, *FAR_Y)
        if f is None:
            continue
        if f["rms"] > LINE_RMS_MAX or abs(f["theta"]) > LINE_THETA_MAX:
            continue
        lines.append({**f, "color": "?", "side": near_side(pts),
                      "band": "near" if f_near else "far"})
    return lines


def render_thumb(bgr: np.ndarray, prob: np.ndarray, lines: list[dict],
                 tag: str, ms: float) -> np.ndarray:
    im = cv2.resize(bgr, (640, 360), interpolation=cv2.INTER_AREA)
    for yc, segs in row_segments(prob):
        for q in segs:
            cv2.circle(im, (int(q["xc"]), yc), 1, (160, 160, 160), -1)
    for c in lines:
        col = {"y": (0, 255, 255), "w": (255, 255, 255)}.get(c["color"], (255, 0, 255))
        for y in range(int(c["y0"]), int(c["y1"]) + 1, 4):
            x = int((c["s"] * y + c["a"]) / 2.0)
            yy = y // 2
            if 0 <= x < 640 and 0 <= yy < 360:
                cv2.circle(im, (x, yy), 2, col, -1)
    sides = "".join(sorted({"L" if c["side"] == -1 else "R"
                            for c in lines if c["side"] in (-1, 1)}))
    txt = f"{tag} {ms:.0f}ms lines={len(lines)} {sides}"
    cv2.rectangle(im, (0, 0), (640, 22), (0, 0, 0), -1)
    cv2.putText(im, txt, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return im


def pctl(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f, c = int(np.floor(k)), int(np.ceil(k))
    return xs[f] if f == c else xs[f] * (c - k) + xs[c] * (k - f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default="", help="逗号分隔 raw 目录；缺省自动取最近两个会话")
    ap.add_argument("--per-session", type=int, default=24)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--provider", choices=["cpu", "dml"], default="cpu")
    ap.add_argument("--overlay-cols", type=int, default=12, help="叠加图每模型的帧数")
    args = ap.parse_args()

    sessions = ([Path(p.strip()) for p in args.sessions.split(",") if p.strip()]
                if args.sessions else find_sessions())
    models = []
    for name, path, note in MODELS:
        if not path.is_file():
            print(f"[跳过] {name}：权重缺失 {path}")
            continue
        so = ort.SessionOptions()
        so.intra_op_num_threads = args.threads
        so.inter_op_num_threads = 1
        prov = (["DmlExecutionProvider", "CPUExecutionProvider"]
                if args.provider == "dml" else ["CPUExecutionProvider"])
        sess = ort.InferenceSession(str(path), sess_options=so, providers=prov)
        padded = "plus" in name
        models.append({"name": name, "path": path, "note": note, "sess": sess,
                       "padded": padded,
                       "kb": round(path.stat().st_size / 1024, 1),
                       "sha": sha256(path)[:12]})

    OUT.mkdir(exist_ok=True)
    summary: dict = {"provider": args.provider, "threads": args.threads,
                     "sessions": {}, "models": {}}
    sheets: list[tuple[str, np.ndarray]] = []

    for raw in sessions:
        frames = sample_frames(raw, args.per_session)
        skey = raw.parent.name
        per_model_rows: list[list[np.ndarray]] = []
        for m in models:
            lat, acc_lines, both_sides, near_cnt, far_cnt = [], [], [], [], []
            thumbs = []
            overlay_frames = frames[::max(1, len(frames) // args.overlay_cols)][:args.overlay_cols]
            for i, fp in enumerate(frames):
                bgr = cv2.imread(str(fp))
                for _ in range(3 if i == 0 else 1):
                    t0 = time.perf_counter()
                    prob = lane_prob(m["sess"], bgr, m["padded"])
                    ms = (time.perf_counter() - t0) * 1000.0
                lat.append(ms)
                lines = extract_lines(bgr, prob)
                acc_lines.append(len(lines))
                sides = {c["side"] for c in lines}
                both_sides.append(len(lines) >= 2 and sides == {-1, 1})
                near_cnt.append(sum(1 for c in lines if c["band"] == "near"))
                far_cnt.append(sum(1 for c in lines if c["band"] == "far"))
                if fp in overlay_frames:
                    thumbs.append(render_thumb(bgr, prob, lines, m["name"], ms))
            while len(thumbs) < args.overlay_cols:
                thumbs.append(np.full((360, 640, 3), 30, np.uint8))
            per_model_rows.append(thumbs)
            summary["models"].setdefault(m["name"], {})[skey] = {
                "kb": m["kb"], "sha256_12": m["sha"],
                "latency_ms_p50": round(pctl(lat, .5), 1),
                "latency_ms_p95": round(pctl(lat, .95), 1),
                "lines_med": float(np.median(acc_lines)),
                "lines_p10": float(pctl(acc_lines, .1)),
                "lines_p90": float(pctl(acc_lines, .9)),
                "frames_ge1_line": round(float(np.mean([n >= 1 for n in acc_lines])), 3),
                "frames_ge2_lines": round(float(np.mean([n >= 2 for n in acc_lines])), 3),
                "frames_both_sides": round(float(np.mean(both_sides)), 3),
                "near_med": float(np.median(near_cnt)),
                "far_med": float(np.median(far_cnt)),
            }
            print(f"[{skey}] {m['name']}: p50={pctl(lat,.5):.1f}ms "
                  f"lines_med={np.median(acc_lines):.0f} "
                  f"双线率={np.mean([n >= 2 for n in acc_lines]):.0%} "
                  f"双侧率={np.mean(both_sides):.0%}")
        row_imgs = [np.hstack(row) for row in per_model_rows]
        w = max(r.shape[1] for r in row_imgs)
        row_imgs = [cv2.copyMakeBorder(r, 0, 0, 0, w - r.shape[1],
                                       cv2.BORDER_CONSTANT, value=(20, 20, 20))
                    for r in row_imgs]
        head = np.full((34, w, 3), 20, np.uint8)
        cv2.putText(head, f"{skey}   frames={len(frames)}   provider={args.provider}"
                          f"  threads={args.threads}", (8, 23),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        sheet = np.vstack([head] + row_imgs)
        sp = OUT / f"{skey}_sheet.jpg"
        cv2.imwrite(str(sp), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
        sheets.append((str(sp), sheet))
        summary["sessions"][skey] = {"raw": str(raw), "frames": len(frames)}

    md = ["# 首眼对比读数（P1）", "",
          f"provider={summary['provider']} threads={summary['threads']}；"
          "线提取语义=生产同口径复刻（行扫描/拟合/颜色/质量门，焦距取标定中位 807.7 代用）", ""]
    for skey, data in summary["sessions"].items():
        md.append(f"## {skey}（{data['frames']} 帧）")
        md.append("| 模型 | KB | 延迟 p50/p95 (ms) | 线数中位 (p10~p90) | ≥2线帧占比 | 双侧帧占比 | 近场中位 | 远场中位 |")
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for mname, msess in summary["models"].items():
            d = msess[skey]
            md.append(f"| {mname} | {d['kb']} | {d['latency_ms_p50']} / {d['latency_ms_p95']} "
                      f"| {d['lines_med']:.0f} ({d['lines_p10']:.0f}~{d['lines_p90']:.0f}) "
                      f"| {d['frames_ge2_lines']:.0%} | {d['frames_both_sides']:.0%} "
                      f"| {d['near_med']:.0f} | {d['far_med']:.0f} |")
        md.append("")
    (OUT / "summary.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(f"输出：{OUT}")


if __name__ == "__main__":
    main()
