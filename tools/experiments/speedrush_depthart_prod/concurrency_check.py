# -*- coding: utf-8 -*-
"""生产排布并发验证：20Hz 控制拍 + 深度异步 worker + 线观测，三种线编排对比。

线编排（`--lane`）：
  cpu          生产现状（decision.json：provider=cpu, threads=4），不碰 DML 锁
  dml          线也走 DML，非阻塞抢锁、抢不到弃帧
  dml_stagger  线走 DML，等感知放锁后的下一拍再开工（与深度同相位）

主线程按 50ms 节拍跑感知（阻塞持锁）→ push → take；10 秒后读 health()。
输出 concurrency_out/concurrency_<lane>.json。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.core import dml_lock                       # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo, lane_obs  # noqa: E402
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "concurrency_out"
FRAMES = HERE / "frames" / "20261109_201359_p1_stable41"
MODELS = ROOT / "maaracing_master" / "plugins" / "speedrush" / "resources" / "models"
TICK_S = 0.05
DURATION_S = 10.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", choices=("cpu", "dml", "dml_stagger"),
                    default="cpu")
    ap.add_argument("--depth-wait", choices=("stagger", "none"),
                    default="stagger")
    ap.add_argument("--depth-interval", type=float, default=None,
                    help="覆盖深度节流窗（秒）；缺省用生产常量")
    args = ap.parse_args()

    rgbs = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
            for p in sorted(FRAMES.glob("*_raw.jpg"))[:4]]

    cal = load_calib()
    sess = depth_geo.load_session(
        MODELS / "depth" / "relative_mnv4m_slim_spf_448_default.onnx")
    obs = depth_geo.DepthRoadObserver(sess, cal)
    async_obs = depth_geo.AsyncDepthRoadObserver(
        obs, **({} if args.depth_interval is None else
                {"infer_min_interval_s": args.depth_interval}))
    if args.depth_wait == "none":
        # A/B：不等控制拍节拍、不等本拍 push，完成即取最新帧开工
        async_obs._stagger_wait = lambda: not async_obs._stop.is_set()
    async_obs.start()
    perc = StreetPerception(str(MODELS / "perception" / "model.onnx"))

    so = depth_geo.ort.SessionOptions()
    so.log_severity_level = 3
    if args.lane == "cpu":
        so.intra_op_num_threads = 4
        providers = ["CPUExecutionProvider"]
    else:
        providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
    lane_sess = depth_geo.ort.InferenceSession(
        str(MODELS / "lane" / "plus_small_384x640.onnx"), sess_options=so,
        providers=providers)
    lane_in = lane_sess.get_inputs()[0].name
    lane_stop = threading.Event()
    lane_stats = {"runs": 0, "busy": 0, "holds": []}

    def lane_loop() -> None:
        i = 0
        next_t = time.perf_counter()
        while not lane_stop.is_set():
            rgb = rgbs[i % len(rgbs)]
            i += 1
            feed = {lane_in: lane_obs._letterbox(rgb)}
            acquired = True
            if args.lane == "dml_stagger":
                seq, _t = dml_lock.TICK_BEAT.snapshot()
                dml_lock.TICK_BEAT.wait_newer(seq, timeout=0.2)
            if args.lane != "cpu":
                acquired = dml_lock.LOCK.acquire(blocking=False)
            if acquired:
                t = time.perf_counter()
                try:
                    lane_sess.run(None, feed)
                    lane_stats["runs"] += 1
                    lane_stats["holds"].append(
                        (time.perf_counter() - t) * 1000.0)
                finally:
                    if args.lane != "cpu":
                        dml_lock.LOCK.release()
            else:
                lane_stats["busy"] += 1
            next_t += TICK_S
            rest = next_t - time.perf_counter()
            if rest > 0:
                lane_stop.wait(rest)

    th = threading.Thread(target=lane_loop, daemon=True)
    th.start()

    new_n = 0
    have_n = 0
    ticks = 0
    tick_ms = []
    t_end = time.perf_counter() + DURATION_S
    while time.perf_counter() < t_end:
        t0 = time.perf_counter()
        fid = ticks + 1
        rgb = rgbs[ticks % len(rgbs)]
        perc.detect(rgb, frame_id=fid, ts_ns=time.perf_counter_ns())
        async_obs.push(rgb, frame_ts_ns=time.perf_counter_ns(), fid=fid,
                       y_v=361.0)
        reading, _grid, is_new = async_obs.take()
        if is_new:
            new_n += 1
        if reading is not None:
            have_n += 1
        ticks += 1
        tick_ms.append((time.perf_counter() - t0) * 1000.0)
        rest = TICK_S - (time.perf_counter() - t0)
        if rest > 0:
            time.sleep(rest)

    lane_stop.set()
    th.join(timeout=1.0)
    h = async_obs.health()
    async_obs.stop()

    tick_ms.sort()
    out = {"lane_mode": args.lane, "depth_wait": args.depth_wait,
           "depth_interval": args.depth_interval,
           "ticks": ticks, "new_results": new_n,
           "have_reading": have_n,
           "tick_p50": round(tick_ms[len(tick_ms) // 2], 2),
           "lane": {k: (round(float(np.median(v)), 2) if k == "holds" and v
                        else v if k != "holds" else None)
                    for k, v in lane_stats.items()},
           "health": h}
    OUT.mkdir(exist_ok=True)
    tag = "prod" if args.depth_interval is None else str(args.depth_interval)
    (OUT / f"concurrency_{args.lane}_{args.depth_wait}_{tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
