"""远线恢复整链回放：同一批帧分别喂「现役口径」与「上移口径」的线观测状态机。

回答的问题：把行扫描起点上移、远段拟合放开之后，线锁状态机是否仍稳定——
线数、TRACK/RELOCK 占比、可用率、自车横位抖动有没有变差。

口径说明：本探针喂 ``anchor=None``（纯线模式），只比较两种线输入对状态机的
影响；生产还叠加深度锚，A/B 结论只对「线输入」这一层成立。HUD 徽章假线剔除
未接入（留帧无徽章语义）。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_far_tracker_replay.py [会话 ...] [--step N] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import cv2
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
from maaracing_master.plugins.speedrush.lane_obs import (  # noqa: E402
    LineTracker, _letterbox, _lines_from_prob, _prob_from_out)

ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster"
MODEL = (REPO / "maaracing_master/plugins/speedrush/resources/models/lane/"
         "plus_small_384x640.onnx")
CAM_H, FOCAL, Y_V = 1.283, 807.7, 361.2
VARIANTS = {
    # 旧口径等价：远界只到 40 米（远段没有行区间）
    "prod": dict(near_max_m=15.0, mid_max_m=40.0, far_max_m=40.0),
    "ext": dict(near_max_m=15.0, mid_max_m=40.0, far_max_m=80.0),
}


def _trace_anchor(name):
    """trace 里当拍的深度路心横位（道）与严格标记，近似生产喂给状态机的锚。"""
    p = ROOT / "logs" / name / "trace/speedrush.jsonl"
    out = {}
    if not p.is_file():
        return out
    for s in p.open(encoding="utf8"):
        d = json.loads(s)
        if d.get("type"):
            continue
        ok = d.get("ro_anchor_ok")
        # 新会话带 ro_anchor（实际喂给状态机的深度锚）；旧会话退回 road_offset。
        val = d.get("ro_anchor")
        if val is None and ok:
            val = d.get("road_offset")
        out[int(d["fid"])] = {
            "anchor": None if val is None else float(val),
            "strict": bool(d.get("ro_anchor_strict")),
        }
    return out


def _run(name, frames, sess, inp):
    trackers = {k: LineTracker() for k in VARIANTS}
    out = {k: [] for k in VARIANTS}
    prev_fid = {k: None for k in VARIANTS}
    anchors = _trace_anchor(name)
    base = ROOT / "debug" / name / "speedrush/drive"
    for fr in frames:
        img = cv2.imread(str(base / fr["file"]))
        if img is None:
            continue
        prob = _prob_from_out(sess.run(
            None, {inp: _letterbox(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))}))
        a = anchors.get(int(fr["fid"]), {"anchor": None, "strict": False})
        for key, cfg in VARIANTS.items():
            lines = _lines_from_prob(prob, CAM_H, FOCAL, Y_V, **cfg)
            pf = prev_fid[key]
            dt = 1.0 / 20.0 if pf is None else max(1e-3, (fr["fid"] - pf) / 20.0)
            prev_fid[key] = fr["fid"]
            res = trackers[key].update(lines, int(fr["fid"]), dt,
                                       anchor=a["anchor"],
                                       anchor_strict=a["strict"])
            far = [ln for ln in lines
                   if min(f["y0"] for f in ln["fits"]) < 440.0]
            out[key].append({"fid": fr["fid"], "x": res["x"],
                             "state": res["state"],
                             "n": res["n_lines"], "usable": res["usable"],
                             "locked": res["locked"], "far": len(far)})
    return out


def _report(out):
    for key, rows in out.items():
        xs = [r["x"] for r in rows if r["x"] is not None]
        jumps = [abs(b - a) for a, b in zip(xs, xs[1:])]
        states = {s: sum(1 for r in rows if r["state"] == s)
                  for s in ("TRACK", "RELOCK", "COAST")}
        nj = sorted(jumps) if jumps else [0.0]
        print(f"  [{key}] 帧={len(rows)} 可用={sum(r['usable'] for r in rows)}"
              f" 状态={states}"
              f" 线数p50={statistics.median(r['n'] for r in rows):.0f}"
              f" 远段线p50={statistics.median(r['far'] for r in rows):.0f}"
              f" |Δx|p50={statistics.median(nj):.3f} "
              f"p90={nj[int(0.9 * len(nj))]:.3f} 道")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*", default=["20261113_163858"])
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--limit", type=int, default=150)
    args = ap.parse_args()
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0].name
    for name in args.sessions:
        p = ROOT / "debug" / name / "speedrush/drive/frames.jsonl"
        if not p.is_file():
            print(f"[跳过] {name}: 无留帧")
            continue
        frames = [json.loads(s) for s in p.open(encoding="utf8")
                  if s.strip()][:: args.step][: args.limit]
        print(f"\n== {name}  采样 {len(frames)} 帧 ==")
        out = _run(name, frames, sess, inp)
        _report(out)
        prod = [r for r in out["prod"]]
        ext = [r for r in out["ext"]]
        dp = [abs(a["x"] - b["x"]) for a, b in zip(prod, ext)
              if a["x"] is not None and b["x"] is not None]
        if dp:
            dp.sort()
            print(f"  两口径 x 差 p50={statistics.median(dp):.3f} "
                  f"p90={dp[int(0.9 * len(dp))]:.3f} 道")


if __name__ == "__main__":
    main()
