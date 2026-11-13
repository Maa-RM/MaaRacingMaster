#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线观测模型效率探针：算力分布 + 几种推理配置的实测（自包含）。

**要回答什么**
1. 0.54MB / 12 万参数的模型，算力到底花在哪一层（按节点估 MAC，按输出分支归账）；
2. 原论文（TwinLiteNet+，small 配置 1.40 GFLOPs）之外，还有哪些不重训就能拿到的
   效率：DML、FP16、INT8；
3. 每种配置换来的墙钟与输出偏差（以 FP32 CPU 为基准的 softmax 最大差 / 像素翻转率）。

**环境要求**：Windows 下从 IDE 子进程跑基准会命中「能效节流」，脚本启动即退出
（`PROCESS_POWER_THROTTLING_EXECUTION_SPEED`），否则线程数与延迟全部失真。

用法::

    .venv/Scripts/python.exe tools/experiments/lane_model_efficiency/probe_model_efficiency.py
    .venv/Scripts/python.exe tools/experiments/lane_model_efficiency/probe_model_efficiency.py --int8
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import os
import statistics
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import onnx
import onnxruntime as ort
from onnx import shape_inference

REPO = Path(__file__).resolve().parents[3]
LANE_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/models/lane"
             / "plus_small_384x640.onnx")
FRAMES_DIR = (REPO / "tools/experiments/speedrush_depthart_prod/frames"
              / "20261111_104222_p2_stable41")
_INPUT = "images"


def disable_power_throttling() -> str:
    """退出 Windows 进程节能节流；本机实测不退出时 4 线程前向慢 8 倍。"""
    try:
        class _State(ctypes.Structure):
            _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG),
                        ("StateMask", wt.ULONG)]

        k = ctypes.windll.kernel32
        handle = ctypes.c_void_p(k.GetCurrentProcess())
        before = _State(1, 0, 0)
        size = wt.DWORD(0)
        k.GetProcessInformation(handle, 4, ctypes.byref(before),
                                ctypes.sizeof(before), ctypes.byref(size))
        state = _State(1, 0x1, 0)
        ok = k.SetProcessInformation(handle, 4, ctypes.byref(state),
                                     ctypes.sizeof(state))
        return (f"节流前 control={before.ControlMask} state={before.StateMask}；"
                f"退出节流 {'成功' if ok else '失败'}")
    except Exception as exc:  # noqa: BLE001
        return f"无法处理节流：{exc!r}"


# ── 预处理（与生产 lane_obs._letterbox 同形）────────────────────────

def letterbox(bgr: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, 12, 12, 0, 0, cv2.BORDER_CONSTANT,
                            value=(114, 114, 114))
    return np.ascontiguousarray(
        (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def lane_prob(out) -> np.ndarray:
    logits = out[1][0][:, 12:372, :].astype(np.float32)
    e = np.exp(logits - logits.max(0, keepdims=True))
    return e[1] / e.sum(0)


# ── 线提取（与生产 lane_obs 同算法，只保留判据相关的量）─────────────

PROB_THRESH = 0.30
NEAR_Y = (560.0, 720.0)
FAR_Y = (440.0, 545.0)


def row_segments(prob: np.ndarray):
    rows = []
    for yc in range(206, 360):
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


def build_tracks(rows, tol: float = 13.0, max_gap: int = 10):
    tracks: list[dict] = []
    for yc, segs in rows:
        used = set()
        for tr in tracks:
            tr["_pred"] = None
            if yc - tr["yc"] > max_gap:
                continue
            tr["_pred"] = tr["xc"] + tr.get("v", 0.0) * (yc - tr["yc"])
        for seg in segs:
            best, best_d = None, tol
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


def fit(pts, cam_h: float, y_lo: float, y_hi: float, focal: float):
    use = [q for q in pts if y_lo <= 2.0 * q["yc"] <= y_hi]
    if len(use) < 4:
        return None
    ys = np.array([2.0 * q["yc"] for q in use])
    xs = np.array([2.0 * q["xc"] for q in use])
    w = np.array([max(0.1, q["p"]) for q in use])
    if ys.max() - ys.min() < 40:
        return None
    A = np.vstack([ys, np.ones_like(ys)]).T
    s, a = np.linalg.lstsq(A * w[:, None], xs * w, rcond=None)[0]
    rms = float(np.sqrt(np.average((xs - (s * ys + a)) ** 2, weights=w)))
    return {"L": float(s * cam_h / 3.5), "rms": rms, "n": len(ys),
            "y0": float(ys.min()), "y1": float(ys.max())}


def extract_lines(prob: np.ndarray, cam_h: float = 1.28,
                  focal: float = 807.7, rms_max: float | None = None) -> list[dict]:
    """概率图 → 线候选（与生产 `_lines_from_prob` 同口径的判据量）。

    ``rms_max`` 给定时只留过质量门的线（生产消费端 LINE_RMS_MAX=3.0px）。"""
    lines = []
    for pts in build_tracks(row_segments(prob)):
        f = fit(pts, cam_h, *NEAR_Y, focal) or fit(pts, cam_h, *FAR_Y, focal)
        if f is None:
            continue
        if rms_max is not None and f["rms"] > rms_max:
            continue
        lines.append(f)
    lines.sort(key=lambda d: d["L"])
    return lines


def compare_extract(frames: list[np.ndarray], ref_sess, got_sess,
                    tol_lane: float = 0.02, rms_max: float = 3.0) -> dict:
    """逐帧比较两套概率图提取出的线候选（只比过质量门的线）。"""
    n = count_bad = raw_bad = 0
    dls, drms = [], []
    for blob in frames:
        pa = lane_prob(ref_sess.run(None, {_INPUT: blob}))
        pb = lane_prob(got_sess.run(None, {_INPUT: blob}))
        raw_a = extract_lines(pa)
        raw_b = extract_lines(pb)
        if len(raw_a) != len(raw_b):
            raw_bad += 1
        a = extract_lines(pa, rms_max=rms_max)
        b = extract_lines(pb, rms_max=rms_max)
        n += 1
        if len(a) != len(b):
            count_bad += 1
            continue
        for x, y in zip(a, b):
            dls.append(abs(x["L"] - y["L"]))
            drms.append(abs(x["rms"] - y["rms"]))
    dls.sort()
    drms.sort()
    over = sum(1 for d in dls if d > tol_lane)
    return {"frames": n, "raw_count_mismatch": raw_bad,
            "count_mismatch": count_bad,
            "dl_p50": round(statistics.median(dls), 4) if dls else None,
            "dl_p95": round(dls[int(len(dls) * 0.95) - 1], 4) if dls else None,
            "dl_max": round(dls[-1], 4) if dls else None,
            "dl_over_tol": over, "pairs": len(dls),
            "drms_max": round(drms[-1], 3) if drms else None}


# ── ① 算力分布：按节点估 MAC，按输出分支归账 ─────────────────────────

def flops_split(model_path: Path) -> None:
    m = shape_inference.infer_shapes(onnx.load(str(model_path)))
    g = m.graph
    shapes = {}
    for v in list(g.value_info) + list(g.input) + list(g.output):
        t = v.type.tensor_type
        if t.HasField("shape"):
            shapes[v.name] = [d.dim_value for d in t.shape.dim]
    inits = {t.name: t for t in g.initializer}
    prod = lambda a: int(np.prod(a)) if a else 0  # noqa: E731

    mac: dict[str, int] = {}
    for n in g.node:
        val = 0
        out = shapes.get(n.output[0], [])
        if n.op_type == "Conv" and len(out) == 4 and 0 not in out:
            w = inits.get(n.input[1])
            if w is not None:
                k = list(w.dims)
                grp = next((a.i for a in n.attribute if a.name == "group"), 1)
                val = out[0] * out[2] * out[3] * (k[1] * k[2] * k[3]) * out[1] // grp
        elif n.op_type in ("MatMul", "Gemm"):
            a = shapes.get(n.input[0], [])
            ws = (list(inits[n.input[1]].dims) if n.input[1] in inits
                  else shapes.get(n.input[1], []))
            if a and ws:
                val = prod(a[:-1]) * ws[-1] if len(ws) > 1 else prod(a) * ws[0]
        mac[n.name] = int(val)

    producer = {o: n.name for n in g.node for o in n.output}
    node_in = {n.name: list(n.input) for n in g.node}

    def ancestors(outputs: list[str]) -> set[str]:
        seen, stack = set(), list(outputs)
        while stack:
            t = stack.pop()
            nn = producer.get(t)
            if nn is None or nn in seen:
                continue
            seen.add(nn)
            stack.extend(node_in[nn])
        return seen

    da, ll = ancestors(["da"]), ancestors(["ll"])
    total = sum(mac.values())
    print("== ① 算力分布（Conv/MatMul MAC，按 2×MAC 计 GFLOP）==")
    print(f"   模型总计 {total * 2 / 1e9:.2f} GFLOP，节点 {len(mac)}")
    print(f"   共享主干（编码器+CAAM）{sum(mac[n] for n in da & ll) * 2 / 1e9:.2f} GFLOP")
    print(f"   仅 da 分支 {sum(mac[n] for n in da - ll) * 2 / 1e9:.2f} GFLOP（"
          f"{(da - ll).__len__()} 节点）")
    print(f"   仅 ll 分支 {sum(mac[n] for n in ll - da) * 2 / 1e9:.2f} GFLOP（"
          f"{(ll - da).__len__()} 节点）")
    for name in sorted(mac, key=lambda k: -mac[k])[:8]:
        node = next(x for x in g.node if x.name == name)
        print(f"   {name:40s} {node.op_type:12s} "
              f"{mac[name] * 2 / 1e9:6.3f} GFLOP out={shapes.get(node.output[0])}")


# ── ② 推理配置实测 ──────────────────────────────────────────────────

def bench(path: Path, provider: str, blob: np.ndarray, threads: int = 4,
          reps: int = 20):
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    providers = ([provider, "CPUExecutionProvider"] if provider != "CPUExecutionProvider"
                 else ["CPUExecutionProvider"])
    sess = ort.InferenceSession(str(path), so, providers=providers)
    for _ in range(5):
        out = sess.run(None, {_INPUT: blob})
    times = []
    for _ in range(reps):
        t = time.perf_counter()
        out = sess.run(None, {_INPUT: blob})
        times.append((time.perf_counter() - t) * 1000.0)
    times.sort()
    return sess, out, times[len(times) // 2], times[int(len(times) * 0.95) - 1]


def to_fp16(src: Path) -> Path:
    from onnxconverter_common import float16
    dst = Path(tempfile.gettempdir()) / "lane_eff_fp16.onnx"
    onnx.save(float16.convert_float_to_float16(onnx.load(str(src)),
                                               keep_io_types=True), str(dst))
    return dst


def to_int8(src: Path, frames: list[np.ndarray]) -> Path:
    from onnxruntime.quantization import (CalibrationDataReader, QuantFormat,
                                          QuantType, quantize_static)
    dst = Path(tempfile.gettempdir()) / "lane_eff_int8.onnx"

    class Reader(CalibrationDataReader):
        def __init__(self) -> None:
            self.items = iter(frames)

        def get_next(self):
            item = next(self.items, None)
            return None if item is None else {_INPUT: item}

    quantize_static(str(src), str(dst), Reader(), quant_format=QuantFormat.QDQ,
                    per_channel=True, activation_type=QuantType.QUInt8,
                    weight_type=QuantType.QInt8)
    return dst


def compare(ref: np.ndarray, got: np.ndarray) -> tuple[float, float, float]:
    a, b = lane_prob(ref), lane_prob(got)
    return (float(np.abs(a - b).max()), float(np.abs(a - b).mean()),
            float(((a > 0.30) != (b > 0.30)).mean()))


def compare_many(ref_sess, got_sess, blobs: list[np.ndarray]) -> tuple[float, float, float]:
    """多帧对比（单帧的 max 会被一帧特例决定，翻转率用全部像素汇总）。"""
    worst = 0.0
    mean_sum = 0.0
    flip_sum = 0
    pixels = 0
    for blob in blobs:
        ref = ref_sess.run(None, {_INPUT: blob})
        got = got_sess.run(None, {_INPUT: blob})
        dmax, dmean, flip = compare(ref, got)
        worst = max(worst, dmax)
        mean_sum += dmean
        flip_sum += flip * lane_prob(ref).size
        pixels += lane_prob(ref).size
    return worst, mean_sum / len(blobs), flip_sum / max(1, pixels)


def main() -> int:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="线观测模型效率探针")
    ap.add_argument("--frames", default=str(FRAMES_DIR))
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--int8", action="store_true", help="额外测静态 INT8（会写临时模型）")
    ap.add_argument("--replay", default=None,
                    help="离线回放目录（连续真帧）：逐帧比较 fp32 与 fp16 提取出的线候选")
    args = ap.parse_args()

    print(disable_power_throttling())
    files = sorted(glob.glob(os.path.join(args.frames, "*.jpg")))
    if not files:
        print("没有可用帧")
        return 2
    blobs = [letterbox(cv2.imread(p)) for p in files[:12]]

    flops_split(LANE_ONNX)

    print("\n== ② 推理配置（同一帧，p50/p95；偏差对 12 帧汇总）==")
    ref_sess, ref_out, cpu_p50, cpu_p95 = bench(
        LANE_ONNX, "CPUExecutionProvider", blobs[0], reps=args.reps)
    print(f"   CPU 4 线程 fp32   {cpu_p50:6.2f} / {cpu_p95:6.2f} ms")
    dml_sess, _dml_out, dml_p50, dml_p95 = bench(
        LANE_ONNX, "DmlExecutionProvider", blobs[0], reps=args.reps)
    dmax, dmean, flip = compare_many(ref_sess, dml_sess, blobs)
    print(f"   DML fp32          {dml_p50:6.2f} / {dml_p95:6.2f} ms   "
          f"max|dp|={dmax:.4f} mean={dmean:.2e} 翻转@0.30={flip * 100:.3f}%")
    fp16_path = to_fp16(LANE_ONNX)
    f16_sess, _f16_out, f16_p50, f16_p95 = bench(
        fp16_path, "DmlExecutionProvider", blobs[0], reps=args.reps)
    dmax, dmean, flip = compare_many(ref_sess, f16_sess, blobs)
    print(f"   DML fp16          {f16_p50:6.2f} / {f16_p95:6.2f} ms   "
          f"max|dp|={dmax:.4f} mean={dmean:.2e} 翻转@0.30={flip * 100:.3f}%")

    if args.int8:
        i8_path = to_int8(LANE_ONNX, blobs)
        i8_sess, _i8_out, i8_p50, i8_p95 = bench(
            i8_path, "CPUExecutionProvider", blobs[0], reps=args.reps)
        dmax, dmean, flip = compare_many(ref_sess, i8_sess, blobs)
        print(f"   CPU 4 线程 INT8   {i8_p50:6.2f} / {i8_p95:6.2f} ms   "
              f"max|dp|={dmax:.4f} mean={dmean:.2e} 翻转@0.30={flip * 100:.3f}%")

    if args.replay:
        files = sorted(glob.glob(os.path.join(args.replay, "*.jpg")))
        if not files:
            print(f"\n回放目录没有帧：{args.replay}")
            return 2
        blobs = [letterbox(cv2.imread(p)) for p in files]
        row = compare_extract(blobs, ref_sess, f16_sess)
        print(f"\n== ③ 离线回放：fp32 vs fp16 提取出的线候选（{args.replay}）==")
        print(f"   帧数 {row['frames']}：原始线数不一致 {row['raw_count_mismatch']} 帧；"
              f"过质量门（rms≤3px）后不一致 {row['count_mismatch']} 帧；"
              f"匹配 {row['pairs']} 对线")
        print(f"   横位 |ΔL| p50={row['dl_p50']} p95={row['dl_p95']} "
              f"max={row['dl_max']} 道；超 0.02 道 {row['dl_over_tol']} 对；"
              f"残差 |Δrms| max={row['drms_max']} px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
