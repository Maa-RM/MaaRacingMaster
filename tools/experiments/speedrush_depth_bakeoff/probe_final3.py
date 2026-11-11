# -*- coding: utf-8 -*-
"""终选三件套·稳定性探针（2026-11-11）：ZipDepth / DepthART / MoGe 生产
在稳定 41 段上的同图稳定性 + 接近平滑度。

判据（标尺独立）：
- MoGe 生产 q4f16：米制直读（适配器 ×0.893，gate0）——质量基准/形态参考，
  不与其余两家竞速（延迟 56ms vs 10.6/23.6ms）；
- ZipDepth / DepthART：逐帧地面行回归两点仿射（z=a·V+b，权威锚 1.25m 关系，
  与对比图同一药方）→ 米制直读。

度量（与乱跳探针同口径）：
- 时序乱跳：相邻跟踪帧 |Δd|（>5m 计跳）与合拢速率 |Δd|/Δt 散布；
- 同图乱跳：同一帧内 框底条带（底部 25% 行，生产口径）vs 整框中位 的读数差。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402
import probe_taxi_distance as pt  # noqa: E402
import render_calibrated as rc  # noqa: E402

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "bakeoff_out"
FRAMES_BASE = ROOT / "frames"
JUMP_M = 5.0

MODELS = ["moge_prod", "zipdepth", "depthart"]


def whole_box_z(v: np.ndarray, box) -> float | None:
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


def metric_of(name: str, s, rgb: np.ndarray) -> np.ndarray:
    """720×1280 米制图（NaN 无效）。"""
    if name == "moge_prod":
        z, valid = pb.run_moge(s, rgb)
        return rc._masked_resize(z, valid)
    if name == "zipdepth":
        d, _ = s.run(None, {"pixel_values": None}) if False else (None, None)
        raise RuntimeError("use run_fn")
    raise KeyError(name)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    segs = sorted(d for d in FRAMES_BASE.iterdir()
                  if d.is_dir() and d.name.endswith("_stable41"))
    sess = {
        "moge_prod": pb.make_session("prod_moge_q4f16.onnx",
                                     free_dims={"batch_size": 1, "height": 720,
                                                "width": 1280}),
        "zipdepth": pb.make_session("zipdepth/zipdepth_fp16_opset17.onnx"),
        "depthart": pb.make_session("depthart/relative_mnv4s_448_default.onnx"),
    }
    det_sess = pb.make_session("prod_perception/model.onnx")

    def depth_map(name: str, rgb: np.ndarray) -> np.ndarray:
        if name == "moge_prod":
            z, valid = pb.run_moge(sess["moge_prod"], rgb)
            return rc._masked_resize(z, valid)
        if name == "zipdepth":
            im = cv2.resize(rgb, (800, 448), interpolation=cv2.INTER_LINEAR)
            blob = (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
            d = sess["zipdepth"].run(
                None, {"pixel_values": np.ascontiguousarray(blob)})[0][0, 0]
        else:  # depthart
            im = cv2.resize(rgb, (448, 448), interpolation=cv2.INTER_AREA)
            blob = (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
            d = sess["depthart"].run(
                None, {"image": np.ascontiguousarray(blob)})[0][0, 0]
        v = pb.to_near_small(d)
        v = pb.resize_to(v, (720, 1280))
        ab = rc.affine_from_ground(v)
        z = ab[0] * v + ab[1]
        z[~np.isfinite(v)] = np.nan
        return z

    results = {}
    n_used = 0
    for seg in segs:
        meta = json.loads((seg / "meta.json").read_text(encoding="utf-8"))
        frames_info = meta["frames"]
        if len(frames_info) < 4:
            continue
        frames = [cv2.cvtColor(cv2.imread(str(seg / r["file"])),
                               cv2.COLOR_BGR2RGB) for r in frames_info]
        ts = [r["ts_ns"] / 1e9 for r in frames_info]
        dets = [pt.detect_cars(det_sess, fr) for fr in frames]
        track = pt.track_taxi(dets)
        if sum(1 for b in track if b) < 3:
            continue
        n_used += 1
        seg_res = {"n_frames": len(frames), "models": {}}
        for name in MODELS:
            band, whole = [], []
            for fr, box in zip(frames, track):
                if box is None:
                    band.append(None); whole.append(None)
                    continue
                v = depth_map(name, fr)
                band.append(pt.band_z(v, box))
                whole.append(whole_box_z(v, box))
            idx = [i for i, x in enumerate(band) if x is not None]
            jumps, rates = 0, []
            for a, b in zip(idx, idx[1:]):
                dd = abs(band[b] - band[a])
                if dd > JUMP_M:
                    jumps += 1
                dt = ts[b] - ts[a]
                if dt > 0:
                    rates.append(dd / dt)
            diffs = [abs(band[i] - whole[i]) for i in idx
                     if band[i] is not None and whole[i] is not None]
            pct = lambda a, q: float(np.percentile(a, q)) if a else None
            seg_res["models"][name] = {
                "series": band,
                "temporal_jumps": jumps,
                "closing_rate_p50": pct(rates, 50),
                "closing_rate_p95": pct(rates, 95),
                "inframe_diff_p50": pct(diffs, 50),
                "inframe_diff_p95": pct(diffs, 95),
                "n_diffs": len(diffs),
            }
        results[seg.name] = seg_res

    # 聚合
    agg = {m: {"n": 0, "jumps": 0, "ip50": [], "ip95": [], "tp50": [], "tp95": []}
           for m in MODELS}
    for seg, s in results.items():
        for m, d in s["models"].items():
            a = agg[m]
            a["n"] += 1
            a["jumps"] += d["temporal_jumps"]
            if d["inframe_diff_p50"] is not None:
                a["ip50"].append(d["inframe_diff_p50"])
                a["ip95"].append(d["inframe_diff_p95"])
            if d["closing_rate_p50"] is not None:
                a["tp50"].append(d["closing_rate_p50"])
                a["tp95"].append(d["closing_rate_p95"])
    print(f"\n== {n_used} 段聚合（同图差=框底条带 vs 整框中位，米制）==")
    print(f"{'模型':12s} 段数 时序跳合计 同图差p50 同图差p95 合拢速率p50/p95 (m/s)")
    for m in MODELS:
        a = agg[m]
        import statistics as st
        ip50 = st.median(a["ip50"]) if a["ip50"] else None
        ip95 = st.median(a["ip95"]) if a["ip95"] else None
        tp50 = st.median(a["tp50"]) if a["tp50"] else None
        tp95 = st.median(a["tp95"]) if a["tp95"] else None
        print(f"{m:12s} {a['n']:3d} {a['jumps']:6d} "
              f"{ip50 and round(ip50, 2)!s:>7} {ip95 and round(ip95, 2)!s:>7} "
              f"{tp50 and round(tp50, 1)!s:>7}/{tp95 and round(tp95, 1)}")

    (OUT_DIR / "final3_stability.json").write_text(
        json.dumps({"segments": results, "agg": {m: {k: v for k, v in a.items()
                                                     if k != 'n'} | {"n": a["n"]}
                                                 for m, a in agg.items()}},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved:", OUT_DIR / "final3_stability.json")


if __name__ == "__main__":
    main()
