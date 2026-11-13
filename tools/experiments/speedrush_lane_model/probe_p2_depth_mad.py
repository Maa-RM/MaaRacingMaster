#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""p2 深度链离线复算：为什么 453/800 拍 MAD 弃帧。

用法：
  python probe_p2_depth_mad.py --session 20261112_203641 --phase 2 --stride 10

读 debug 会话的原始帧 + trace 的逐拍 lane y_v，重跑 depth_geo.observe_debug，
打印每帧 ground MAD / 双侧读数 / 拒绝原因。只读，不改生产。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

APPDATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
MODEL = ROOT / "maaracing_master/plugins/speedrush/resources/models/depth" \
               "/relative_mnv4m_slim_spf_448_default.onnx"


def load_trace_yv(session: str) -> dict[int, float]:
    p = APPDATA / "MaaRacingMaster/logs" / session / "trace/speedrush.jsonl"
    out: dict[int, float] = {}
    with p.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") is not None:
                continue
            lo = d.get("lane_obs") or {}
            if lo.get("y_v") is not None:
                out[int(d["fid"])] = float(lo["y_v"])
    return out


def load_frames(session: str, phase: int) -> list[dict]:
    p = APPDATA / "MaaRacingMaster/debug" / session / "speedrush/drive/frames.jsonl"
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if int(d.get("phase") or 0) == phase:
                out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="20261112_203641")
    ap.add_argument("--phase", type=int, default=2)
    ap.add_argument("--stride", type=int, default=10)
    ap.add_argument("--provider", default="CPUExecutionProvider")
    args = ap.parse_args()

    import onnxruntime as ort
    sess = ort.InferenceSession(str(MODEL), providers=[args.provider])
    cal = load_calib()
    obs = dg.DepthRoadObserver(sess, cal)
    # 生产门限会连证据包一起丢（infer_points 超限返回 None 证据），这里放开
    # 让每帧都把 mad 带出来，自己按生产门限统计。
    mad_gate = dg.GROUND_MAD_MAX
    dg.GROUND_MAD_MAX = 1e9
    yv = load_trace_yv(args.session)
    frames = load_frames(args.session, args.phase)
    base = APPDATA / "MaaRacingMaster/debug" / args.session / "speedrush/drive"

    print(f"frames={len(frames)} yv={len(yv)} stride={args.stride}")
    print("idx fid    yv    mad   sides  left    right   rejects")
    t0 = time.perf_counter()
    n_mad = n_ok = n_none = 0
    for i in range(0, len(frames), args.stride):
        fr = frames[i]
        fid = int(fr["fid"])
        img = cv2.imread(str(base / fr["file"]))
        if img is None:
            continue
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        y_v = yv.get(fid)
        r, ev = obs.observe_debug(rgb, None, y_v=y_v)
        mad = None if not ev else ev.get("mad")
        if r is None:
            n_none += 1
            print(f"{i:4d} {fid} {str(y_v):>6}  {mad if mad is None else round(float(mad),4)}"
                  f"   None  (MAD 弃帧或拟合失败)")
            continue
        if mad is not None and float(mad) > mad_gate:
            n_mad += 1
        else:
            n_ok += 1
        print(f"{i:4d} {fid} {str(y_v):>6}  {round(float(mad),4) if mad is not None else '-':>6}"
              f"   {r.sides}    {r.left_edge_lane if r.left_edge_lane is None else round(r.left_edge_lane,2)}"
              f"   {r.right_edge_lane if r.right_edge_lane is None else round(r.right_edge_lane,2)}"
              f"   {r.rejects}")
    print(f"\nok={n_ok} mad_skip={n_mad} none={n_none}  gate={mad_gate}"
          f"  elapsed={time.perf_counter()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
