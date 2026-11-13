# -*- coding: utf-8 -*-
"""当前生产链耗时实测：深度几何全链分量 + 感知 / 线观测单次推理。

跑真实权重与真实帧（实验区 frames 缓存），DML 单线程串行，各 30 次取分位。
输出 measure_out/measure_now.json。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.core import dml_lock                      # noqa: E402
from maaracing_master.core.yolo_detector import YOLODetector    # noqa: E402
from maaracing_master.plugins.speedrush import depth_geo, lane_obs  # noqa: E402
from maaracing_master.plugins.speedrush.perception import (     # noqa: E402
    StreetPerception)
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "measure_out"
FRAMES = HERE / "frames" / "20261109_201359_p1_stable41"
MODELS = ROOT / "maaracing_master" / "plugins" / "speedrush" / "resources" / "models"
N_WARM = 5
N_RUN = 30


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def summarise(name: str, xs: list[float]) -> dict:
    return {"name": name, "n": len(xs),
            "p50": round(pct(xs, 50), 2), "p90": round(pct(xs, 90), 2),
            "min": round(min(xs), 2), "max": round(max(xs), 2),
            "mean": round(float(statistics.fmean(xs)), 2)}


def main() -> None:
    frames = sorted(FRAMES.glob("*_raw.jpg"))
    if not frames:
        raise SystemExit("无帧缓存")
    rgbs = []
    for p in frames[:4]:
        bgr = cv2.imread(str(p))
        rgbs.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))

    out: dict = {"frames": [p.name for p in frames[:4]], "result": {}}

    # ---- 深度几何全链（生产 observer） ----
    cal = load_calib()
    sess = depth_geo.load_session(
        MODELS / "depth" / "relative_mnv4m_slim_spf_448_default.onnx")
    obs = depth_geo.DepthRoadObserver(sess, cal)
    for i in range(N_WARM):
        obs.observe_debug(rgbs[i % len(rgbs)])
    totals: list[float] = []
    stages: dict[str, list[float]] = {}
    for i in range(N_RUN):
        rgb = rgbs[i % len(rgbs)]
        t = time.perf_counter()
        reading, _ = obs.observe_debug(rgb)
        dt = (time.perf_counter() - t) * 1000.0
        if reading is None:
            continue
        totals.append(dt)
        for k, v in depth_geo.LAST_STAGE_MS.items():
            stages.setdefault(k, []).append(float(v))
    out["result"]["depth_total"] = summarise("depth_total", totals)
    out["result"]["depth_stage"] = {
        k: summarise(k, v) for k, v in stages.items()}

    # ---- points 段细分：双线性放大 / 地面拟合 / 合成 ----
    raw = sess.run(None, {sess.get_inputs()[0].name: depth_geo._da_preprocess(
        rgbs[0])})[0][0, 0]
    up_ms, gm_ms, synth_ms = [], [], []
    for _ in range(N_RUN):
        t = time.perf_counter()
        v = depth_geo._resize_align_corners(raw.astype(np.float64),
                                            depth_geo.FRAME_H,
                                            depth_geo.FRAME_W)
        up_ms.append((time.perf_counter() - t) * 1000.0)
        t = time.perf_counter()
        gm = depth_geo._ground_model(v, 354.2, None)
        gm_ms.append((time.perf_counter() - t) * 1000.0)
        t = time.perf_counter()
        depth_geo._synth_points(v, 354.2, gm)
        synth_ms.append((time.perf_counter() - t) * 1000.0)
    out["result"]["points_resize"] = summarise("points_resize", up_ms)
    out["result"]["points_ground_fit"] = summarise("points_ground_fit", gm_ms)
    out["result"]["points_synth"] = summarise("points_synth", synth_ms)

    # ---- 感知（生产 StreetPerception） ----
    perc = StreetPerception(str(MODELS / "perception" / "model.onnx"))
    for i in range(N_WARM):
        perc.detect(rgbs[i % len(rgbs)])
    infer, run, wait = [], [], []
    for i in range(N_RUN):
        r = perc.detect(rgbs[i % len(rgbs)])
        infer.append(r.infer_ms)
        run.append(r.run_ms)
        wait.append(r.wait_ms)
    out["result"]["yolo_infer"] = summarise("yolo_infer", infer)
    out["result"]["yolo_run"] = summarise("yolo_run", run)
    out["result"]["yolo_wait"] = summarise("yolo_wait", wait)

    # ---- 线观测（生产预处理 + run，不含 tracker） ----
    so = depth_geo.ort.SessionOptions()
    so.log_severity_level = 3
    lane_sess = depth_geo.ort.InferenceSession(
        str(MODELS / "lane" / "plus_small_384x640.onnx"), sess_options=so,
        providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    lane_in = lane_sess.get_inputs()[0].name
    for i in range(N_WARM):
        lane_sess.run(None, {lane_in: lane_obs._letterbox(
            rgbs[i % len(rgbs)])})
    lane_run = []
    for i in range(N_RUN):
        feed = {lane_in: lane_obs._letterbox(rgbs[i % len(rgbs)])}
        t = time.perf_counter()
        lane_sess.run(None, feed)
        lane_run.append((time.perf_counter() - t) * 1000.0)
    out["result"]["lane_run"] = summarise("lane_run", lane_run)

    OUT.mkdir(exist_ok=True)
    (OUT / "measure_now.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in out["result"].items():
        if "name" in v:
            print(f"{v['name']:<12} p50={v['p50']:6.2f}  p90={v['p90']:6.2f}  "
                  f"min={v['min']:6.2f}  max={v['max']:6.2f}")
        else:
            for kk, vv in v.items():
                print(f"  {kk:<8} p50={vv['p50']:6.2f}  p90={vv['p90']:6.2f}  "
                      f"min={vv['min']:6.2f}  max={vv['max']:6.2f}")
    print(f"out={OUT / 'measure_now.json'}")


if __name__ == "__main__":
    main()
