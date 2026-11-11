# -*- coding: utf-8 -*-
"""多段平滑度探针（稳定 41 帧集）：出租车距离的「接近中乱跳」与「同图乱跳」。

判据（维护者定调 2026-11-11）：换其他模型——米制直读也好、车道线换算也罢——
**不能接受接近中乱跳，甚至同一张图都乱跳**。两个乱跳的度量：
- 时序乱跳：相邻跟踪帧的 |Δd|（>5m 计一次跳）与合拢速率 |Δd|/Δt 散布；
- 同图乱跳：同一帧内 框底条带（底部 25% 行，生产口径）与 整框中位 的读数差
  |band−whole|（生产已知整框口径在同图内不稳定，底带单调平滑）。

标尺独立：MoGe 生产权重米制直读 × depth_scale(0.893, gate0)；其余模型乘法
路宽尺（14m = 四车道 × 3.5m，RULES；在本段「路缘两侧进画面」的行上量，
乘法换算对仿射偏移模型是近似，单列 caveat——只比乱跳形状）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402
import probe_taxi_distance as pt  # noqa: E402

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "bakeoff_out"
FRAMES_BASE = ROOT / "frames"
DEPTH_SCALE = 0.893
ROAD_FULL_M = 14.0
JUMP_M = 5.0

DEPTH_SPECS = [
    ("moge_prod_q4f16", "prod_moge_q4f16.onnx", None, True),
    ("moge2_vits_fp32", "moge2_vits/model_fp32.onnx", None, False),
    ("zipdepth_fp16", "zipdepth/zipdepth_fp16_opset17.onnx", (448, 800), False),
    ("depthart_mnv4s_448", "depthart/relative_mnv4s_448_default.onnx", (448, 448), False),
    ("yolo26n", "yolo26_depth/yolo26n-depth.onnx", None, False),
    ("lite_mono_640x192", "lite_mono_base_640x192", (192, 640), False),
    ("midas_small", "midas/model-small.onnx", (256, 256), False),
]


def whole_box_z(v: np.ndarray, box) -> float | None:
    """整框中位（同图乱跳的对照口径，生产已知不稳定的旧读法）。"""
    gh, gw = v.shape[:2]
    x0 = max(0, int(box[0] * gw / 1280)); y0 = max(0, int(box[1] * gh / 720))
    x1 = min(gw, int(box[2] * gw / 1280)); y1 = min(gh, int(box[3] * gh / 720))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    zv = v[y0:y1, x0:x1]
    zv = zv[np.isfinite(zv)]
    if zv.size < 10:
        return None
    return float(np.median(zv))


def pct(a, q):
    return float(np.percentile(a, q)) if a else None


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    segs = sorted(d for d in FRAMES_BASE.iterdir()
                  if d.is_dir() and d.name.endswith("_stable41"))
    results = {}
    for seg in segs:
        meta = json.loads((seg / "meta.json").read_text(encoding="utf-8"))
        frames_info = meta["frames"]
        frames = [cv2.cvtColor(cv2.imread(str(seg / r["file"])),
                               cv2.COLOR_BGR2RGB) for r in frames_info]
        ts = [r["ts_ns"] / 1e9 for r in frames_info]
        if len(frames) < 4:
            continue
        det_sess = pb.make_session("prod_perception/model.onnx")
        dets = [pt.detect_cars(det_sess, fr) for fr in frames]
        track = pt.track_taxi(dets)
        n_tracked = sum(1 for b in track if b)
        if n_tracked < 3:
            print(f"[skip] {seg.name}: tracked {n_tracked}/{len(frames)}")
            continue
        print(f"== {seg.name}: {len(frames)} 帧, tracked {n_tracked}")
        seg_res = {"frames": [int(r["file"][:4]) for r in frames_info],
                   "n_tracked": n_tracked, "models": {}}

        for name, rel, hw, is_prod_moge in DEPTH_SPECS:
            try:
                if name.startswith("moge"):
                    sess = pb.make_session(rel, free_dims={"batch_size": 1,
                                                           "height": 720,
                                                           "width": 1280})
                    run = lambda rgb, s=sess: pb.run_moge(s, rgb)
                elif name.startswith("lite_mono"):
                    enc = pb.make_session(rel + "/encoder.onnx")
                    dec = pb.make_session(rel + "/decoder.onnx")
                    run = lambda rgb, e=enc, d=dec, h_=hw: pb.run_lite_mono(
                        e, d, rgb, h_)
                elif name.startswith("yolo26"):
                    sess = pb.make_session(rel)
                    run = lambda rgb, s=sess: pb.run_yolo26(s, rgb)
                elif name.startswith("zipdepth"):
                    sess = pb.make_session(rel)
                    run = lambda rgb, s=sess: pb.run_zipdepth(s, rgb)
                elif name.startswith("depthart"):
                    sess = pb.make_session(rel)
                    run = lambda rgb, s=sess: pb.run_depthart(s, rgb, False)
                else:
                    sess = pb.make_session(rel)
                    run = lambda rgb, s=sess: pb.run_midas(s, rgb)

                band, whole = [], []
                for fr, box in zip(frames, track):
                    if box is None:
                        band.append(None); whole.append(None)
                        continue
                    v_raw, valid = run(fr)
                    v = pb.to_near_small(v_raw)
                    v = pb.resize_to(v, (720, 1280))
                    zb = pt.band_z(v, box)
                    zw = whole_box_z(v, box)
                    # pb.run_moge 适配器内部已乘 gate0 depth_scale 0.893（单入口），
                    # 不再重复乘（双重校正，2026-11-11 校准对齐时发现）。
                    if False and is_prod_moge:
                        zb = zb * DEPTH_SCALE if zb is not None else None
                        zw = zw * DEPTH_SCALE if zw is not None else None
                    band.append(zb); whole.append(zw)

                # 路宽尺（本段全幅路宽 → m/单位）
                scale_m = None
                if not is_prod_moge:
                    ests = []
                    for fr in frames:
                        v_raw, valid = run(fr)
                        v = pb.to_near_small(v_raw)
                        v = pb.resize_to(v, (720, 1280))
                        valid = pb.resize_to(valid.astype(np.float32),
                                             (720, 1280)) > 0.5
                        wpx = pt.road_full_width_px(v, valid)
                        if wpx:
                            zmid = float(np.nanmedian(v[500:640]))
                            ests.append(ROAD_FULL_M /
                                        (wpx * zmid / (pb.MOGE_FX_NORM * 1280.0)))
                    if ests:
                        scale_m = float(np.median(ests))

                s_m = []
                for zb in band:
                    if zb is None:
                        s_m.append(None)
                    elif is_prod_moge:
                        s_m.append(zb)
                    elif scale_m:
                        s_m.append(zb * scale_m)
                    else:
                        s_m.append(None)

                # 时序乱跳
                idx = [i for i, x in enumerate(s_m) if x is not None]
                jumps, rates = 0, []
                for a, b in zip(idx, idx[1:]):
                    dd = abs(s_m[b] - s_m[a])
                    if dd > JUMP_M:
                        jumps += 1
                    dt = ts[b] - ts[a]
                    if dt > 0:
                        rates.append(dd / dt)
                # 同图乱跳
                diffs = [abs(band[i] - whole[i]) for i in idx
                         if band[i] is not None and whole[i] is not None]
                seg_res["models"][name] = {
                    "scale_m_per_unit": "metric" if is_prod_moge else scale_m,
                    "series": s_m,
                    "temporal_jumps": jumps,
                    "closing_rate_p50": pct(rates, 50),
                    "closing_rate_p95": pct(rates, 95),
                    "inframe_diff_p50": pct(diffs, 50),
                    "inframe_diff_p95": pct(diffs, 95),
                    "n_frame_diffs": len(diffs),
                }
                m = seg_res["models"][name]
                print(f"  {name}: jumps {m['temporal_jumps']}  "
                      f"rate p50 {m['closing_rate_p50'] and round(m['closing_rate_p50'],1)}  "
                      f"同图差 p50 {m['inframe_diff_p50'] and round(m['inframe_diff_p50'],1)}/"
                      f"p95 {m['inframe_diff_p95'] and round(m['inframe_diff_p95'],1)}")
            except Exception as e:  # noqa: BLE001
                seg_res["models"][name] = {"error": str(e)[:200]}
                print(f"  [FAIL] {name}: {str(e)[:120]}")
        results[seg.name] = seg_res

    (OUT_DIR / "smoothness.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved:", OUT_DIR / "smoothness.json")


if __name__ == "__main__":
    main()
