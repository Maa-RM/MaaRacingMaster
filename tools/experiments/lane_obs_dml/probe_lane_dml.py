#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线观测搬到 DML 的代价探针（自包含，不 import 业务代码）。

**要回答什么**
1. 现役车道线观测（CPU 4 线程、20Hz 逐拍）每帧花掉多少 CPU，花在哪一段：
   预处理 / 前向 / 概率解码 / 线提取 / HUD 徽章剔除；
2. 同一模型换 DML 后，前向与整帧的墙钟、CPU 各变成多少——省下的到底是哪部分
   （后处理不会因为换 provider 而变快，这是本探针要摆出的账面）；
3. 生产节拍下（感知 20Hz 持锁、深度 60ms 错峰抢锁），线观测也挤进同一把
   DML 锁，会不会把深度饿死、把感知拍拖长——这决定「能不能换」而不是「快不快」。

**口径**
- 单帧阶段成本：每段重复 K 次取总 CPU 时间 ÷ K（Windows 的进程 CPU 时钟粒度
  约 15.6ms，单次 5ms 的差值是噪声，只有累计口径可信）；
- 端到端：逐帧墙钟取 p50/p95，CPU 取整轮累计；
- 锁争抢：三线程按生产节拍跑固定时长，统计各方的尝试/成功/让锁次数与延迟，
  并给出进程级 CPU 占用（占单核百分比）与整机占比；
- 帧源与模型都是仓库里的真实资产；会话参数照抄生产（线观测 intra=4/inter=1，
  深度与感知同各自 load_session / build_session_options）。

**为什么脚本不 import 业务代码**：结论要能换人复算；复制来的提取链只保留
计费用的主体（逐行扫描 / 轨迹关联 / 两段加权拟合 / 徽章模板匹配），与生产
同算法同参数，数字可直接对账，但不能反过来当生产实现。

用法::

    .venv/Scripts/python.exe tools/experiments/lane_obs_dml/probe_lane_dml.py
    .venv/Scripts/python.exe tools/experiments/lane_obs_dml/probe_lane_dml.py --duration 20 --json
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

REPO = Path(__file__).resolve().parents[3]
LANE_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/models/lane"
             / "plus_small_384x640.onnx")
PERCEPTION_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/models"
                   / "perception/model.onnx")
DEPTH_ONNX = (REPO / "maaracing_master/plugins/speedrush/resources/models/depth"
              / "relative_mnv4m_slim_spf_448_default.onnx")
BADGE_TPL = (REPO / "maaracing_master/plugins/speedrush/resources/policy"
             / "hud_badge_mi.png")
FRAMES_DIR = (REPO / "tools/experiments/speedrush_depthart_prod/frames"
              / "20261111_104222_p2_stable41")

CPU_THREADS = 4          # 生产 lane_obs.threads
LANE_HZ = 20.0           # 生产控制拍 20Hz，hz=0 即每拍检测
DEPTH_INTERVAL_S = 0.06  # 生产 depth_geo.INFER_MIN_INTERVAL_S
STAGGER_WAIT_S = 0.2
DA_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
DA_STD = np.array([0.229, 0.224, 0.225], np.float32)

# onnxruntime-directml 1.24.4：任一 session 析构后再对另一 session run 会段错误，
# 本进程创建的会话全部保活到退出。
_SESSIONS: list = []


def disable_power_throttling() -> str:
    """退出 Windows 进程节能节流（EcoQoS）。

    本机实证（2026-11-13）：从 IDE 子进程直接跑本探针时，进程被套上
    PROCESS_POWER_THROTTLING_EXECUTION_SPEED，ORT 4 线程前向 417ms、CPU 400ms；
    显式退出节流后同帧 26.7ms / 93ms。不先处理这一项，线程数对比全是伪影。
    """
    try:
        class _State(ctypes.Structure):
            _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG),
                        ("StateMask", wt.ULONG)]

        kernel32 = ctypes.windll.kernel32
        handle = ctypes.c_void_p(kernel32.GetCurrentProcess())
        before = _State(1, 0, 0)
        size = wt.DWORD(0)
        kernel32.GetProcessInformation(handle, 4, ctypes.byref(before),
                                       ctypes.sizeof(before), ctypes.byref(size))
        state = _State(1, 0x1, 0)          # 显式关闭 ExecutionSpeed 节流
        ok = kernel32.SetProcessInformation(
            handle, 4, ctypes.byref(state), ctypes.sizeof(state))
        return (f"节流前 control={before.ControlMask} state={before.StateMask}；"
                f"退出节流 {'成功' if ok else '失败'}")
    except Exception as exc:  # noqa: BLE001 —— 非 Windows 或受限环境
        return f"无法查询/设置节流：{exc!r}"


# ── 计时与进程/线程 CPU ──────────────────────────────────────────────

def process_cpu_s() -> float:
    """进程累计 CPU 秒（含全部线程；单核百分比 = 增量 / 墙钟）。"""
    return time.process_time()


_thread_times = {}


def _bind_thread_times() -> bool:
    if "fn" in _thread_times:
        return _thread_times["fn"] is not None
    try:
        k = ctypes.windll.kernel32
        k.OpenThread.restype = ctypes.c_void_p
        k.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.GetThreadTimes.restype = ctypes.c_bool
        k.GetThreadTimes.argtypes = [ctypes.c_void_p] + [
            ctypes.POINTER(ctypes.c_uint64)] * 4
        k.CloseHandle.restype = ctypes.c_bool
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        _thread_times["fn"] = k
    except Exception:  # noqa: BLE001 —— 非 Windows 或受限环境：只用进程口径
        _thread_times["fn"] = None
    return _thread_times["fn"] is not None


def thread_cpu_s(tid: int) -> float | None:
    """单线程累计 CPU 秒（GetThreadTimes）；读不到返回 None。"""
    if not _bind_thread_times():
        return None
    k = _thread_times["fn"]
    handle = k.OpenThread(0x0800, False, tid)
    if not handle:
        return None
    try:
        vals = [ctypes.c_uint64() for _ in range(4)]
        ok = k.GetThreadTimes(ctypes.c_void_p(handle),
                              *[ctypes.byref(v) for v in vals])
        if not ok:
            return None
        return (vals[2].value + vals[3].value) * 100e-9
    finally:
        k.CloseHandle(ctypes.c_void_p(handle))


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


_sys_times = {}


def sys_cpu_pct() -> float | None:
    """整机 CPU 占用（GetSystemTimes 差分）；外部负载是测量环境的一部分。"""
    try:
        if "fn" not in _sys_times:
            k = ctypes.windll.kernel32
            k.GetSystemTimes.argtypes = [ctypes.POINTER(wt.FILETIME)] * 3
            k.GetSystemTimes.restype = ctypes.c_bool
            _sys_times["fn"] = k
        k = _sys_times["fn"]

        def read() -> tuple[int, int, int]:
            idle, kern, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
            k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                             ctypes.byref(user))
            conv = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime  # noqa: E731
            return conv(idle), conv(kern), conv(user)

        prev = _sys_times.get("prev")
        now = read()
        _sys_times["prev"] = now
        if prev is None:
            time.sleep(0.2)
            prev, now = read(), read()
            _sys_times["prev"] = now
        d_idle = now[0] - prev[0]
        d_all = (now[1] - prev[1]) + (now[2] - prev[2])
        return None if d_all <= 0 else round((1.0 - d_idle / d_all) * 100.0, 1)
    except Exception:  # noqa: BLE001
        return None


class LoadMeter:
    """外部负载记录：测量期间每秒采样一次整机 CPU，报告 p50/max。"""

    def __init__(self) -> None:
        self.values: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sys_cpu_pct()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            v = sys_cpu_pct()
            if v is not None:
                self.values.append(v)
            self._stop.wait(1.0)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return {"p50": pct(self.values, 0.50), "max": pct(self.values, 1.0),
                "n": len(self.values)}


# ── 会话（参数照抄生产）──────────────────────────────────────────────

def lane_session(provider: str, threads: int = CPU_THREADS,
                 model_path: Path = LANE_ONNX):
    so = ort.SessionOptions()
    so.intra_op_num_threads = int(threads)
    so.inter_op_num_threads = 1
    prov = (["DmlExecutionProvider", "CPUExecutionProvider"]
            if provider == "dml" else ["CPUExecutionProvider"])
    sess = ort.InferenceSession(str(model_path), sess_options=so, providers=prov)
    _SESSIONS.append(sess)
    return sess


def perception_session():
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 4
    sess = ort.InferenceSession(
        str(PERCEPTION_ONNX), sess_options=so,
        providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    _SESSIONS.append(sess)
    return sess


def depth_session():
    sess = ort.InferenceSession(
        str(DEPTH_ONNX),
        sess_options=ort.SessionOptions(),
        providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    _SESSIONS.append(sess)
    return sess


# ── 输入构造（与生产同形）───────────────────────────────────────────

def lane_blob(rgb: np.ndarray) -> np.ndarray:
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, 12, 12, 0, 0, cv2.BORDER_CONSTANT,
                            value=(114, 114, 114))
    return np.ascontiguousarray(
        (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def yolo_blob(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = min(640 / h, 640 / w)
    nh, nw = int(h * scale), int(w * scale)
    py, px = (640 - nh) // 2, (640 - nw) // 2
    padded = np.full((640, 640, 3), 114, dtype=np.uint8)
    padded[py:py + nh, px:px + nw] = cv2.resize(
        rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0


def depth_blob(rgb: np.ndarray) -> np.ndarray:
    im = cv2.resize(rgb, (448, 448), interpolation=cv2.INTER_CUBIC)
    x = im.astype(np.float32) / 255.0
    x = (x - DA_MEAN) / DA_STD
    return x.transpose(2, 0, 1)[None]


# ── 线后处理（与生产同算法，只保留计费主体）─────────────────────────

PROB_THRESH = 0.30


def prob_from_out(out) -> np.ndarray:
    logits = out[1][0][:, 12:372, :]
    e = np.exp(logits - logits.max(0, keepdims=True))
    return (e[1] / e.sum(0)).astype(np.float32)


def row_segments(prob: np.ndarray) -> list[tuple[int, list[dict]]]:
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
    return np.linalg.lstsq(A * w[:, None], xs * w, rcond=None)[0]


def extract(prob: np.ndarray, cam_h: float = 1.28,
            focal: float = 807.7) -> list[dict]:
    lines = []
    for pts in build_tracks(row_segments(prob)):
        for band in ((560.0, 720.0), (440.0, 545.0)):
            fit(pts, cam_h, *band, focal)
        lines.append({"n": len(pts)})
    return lines


_BADGE = {}


def badge_box(rgb: np.ndarray):
    """HUD 距离徽章模板匹配（生产 _hud_badge_box 同参）。"""
    if "tpl" not in _BADGE:
        _BADGE["tpl"] = (cv2.imread(str(BADGE_TPL), cv2.IMREAD_GRAYSCALE)
                         if BADGE_TPL.is_file() else None)
        if _BADGE["tpl"] is not None:
            _BADGE["tpl"] = _BADGE["tpl"].astype(np.float32)
    tpl = _BADGE["tpl"]
    if tpl is None or rgb is None or rgb.ndim != 3:
        return None
    h, w = rgb.shape[:2]
    y0, y1 = int(h * 0.855), int(h * 0.930)
    x0, x1 = int(w * 0.28), int(w * 0.66)
    roi = rgb[y0:y1, x0:x1].astype(np.float32).mean(axis=2)
    th, tw = tpl.shape[:2]
    if roi.shape[0] < th or roi.shape[1] < tw:
        return None
    res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    return None if score < 0.70 else loc


# ── ① 单帧阶段成本 ──────────────────────────────────────────────────

def solo_bench(sess, name: str, frames: list[np.ndarray],
               reps: int = 30) -> dict:
    meter = LoadMeter()
    meter.start()
    inp = sess.get_inputs()[0].name
    blob = lane_blob(frames[0])
    out = sess.run(None, {inp: blob})
    prob = prob_from_out(out)

    def measure(fn, k: int) -> tuple[float, float]:
        t0, c0 = time.perf_counter(), process_cpu_s()
        for _ in range(k):
            fn()
        return (time.perf_counter() - t0) / k * 1000.0, \
               (process_cpu_s() - c0) / k * 1000.0

    stages: dict[str, dict] = {}
    i = 0

    def next_frame():
        nonlocal i
        im = frames[i % len(frames)]
        i += 1
        return im

    for label, fn in (
        ("pre", lambda: lane_blob(next_frame())),
        ("run", lambda: sess.run(None, {inp: blob})),
        ("decode", lambda: prob_from_out(out)),
        ("extract", lambda: extract(prob)),
        ("badge", lambda: badge_box(frames[0])),
    ):
        wall, cpu = measure(fn, reps)
        stages[label] = {"wall_ms": round(wall, 2), "cpu_ms": round(cpu, 2)}

    # 端到端：逐帧墙钟分布 + 整轮 CPU
    walls = []
    t0, c0 = time.perf_counter(), process_cpu_s()
    for _ in range(reps):
        st = time.perf_counter()
        blob = lane_blob(next_frame())
        out = sess.run(None, {inp: blob})
        extract(prob_from_out(out))
        walls.append((time.perf_counter() - st) * 1000.0)
    total_cpu = (process_cpu_s() - c0) / reps * 1000.0
    return {"name": name,
            "providers": list(sess.get_providers()),
            "stages": stages,
            "frame_p50_ms": pct(walls, 0.50),
            "frame_p95_ms": pct(walls, 0.95),
            "frame_cpu_ms": round(total_cpu, 2),
            "sys_load": meter.stop()}


# ── ①b 线程数 × ORT 自旋矩阵（CPU 前向）────────────────────────────

def thread_matrix(frames: list[np.ndarray], reps: int = 20) -> list[dict]:
    """CPU provider 下 intra_op 线程数与 allow_spinning 的组合。

    生产注释的 23ms/帧是 4 线程口径；本机实测 4 线程会慢到数百毫秒，需要把
    「线程数」与「ORT 自旋」两个变量分开看，才能判断 DML 是不是唯一出路。"""
    blob = lane_blob(frames[0])
    rows = []
    for threads in (1, 2, 4):
        for spin in (None, "0"):
            so = ort.SessionOptions()
            so.intra_op_num_threads = threads
            so.inter_op_num_threads = 1
            if spin is not None:
                so.add_session_config_entry(
                    "session.intra_op.allow_spinning", spin)
            sess = ort.InferenceSession(
                str(LANE_ONNX), sess_options=so,
                providers=["CPUExecutionProvider"])
            _SESSIONS.append(sess)
            inp = sess.get_inputs()[0].name
            for _ in range(4):
                sess.run(None, {inp: blob})
            walls = []
            c0 = process_cpu_s()
            for _ in range(reps):
                t = time.perf_counter()
                sess.run(None, {inp: blob})
                walls.append((time.perf_counter() - t) * 1000.0)
            cpu = (process_cpu_s() - c0) / reps * 1000.0
            rows.append({"threads": threads,
                         "spin": "default" if spin is None else "off",
                         "run_p50_ms": pct(walls, 0.50),
                         "run_p95_ms": pct(walls, 0.95),
                         "run_cpu_ms": round(cpu, 2)})
    return rows


# ── ② 锁争抢模拟（生产节拍）──────────────────────────────────────────

class Beat:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._seq = 0

    def note(self) -> None:
        with self._cv:
            self._seq += 1
            self._cv.notify_all()

    def snapshot(self) -> int:
        with self._cv:
            return self._seq

    def wait_newer(self, seq: int, timeout: float) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: self._seq != seq, timeout=timeout)


def gpu_poll(stop: threading.Event, out: list[float]) -> None:
    while not stop.is_set():
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3)
            line = r.stdout.strip().splitlines()[0]
            out.append(float(line))
        except Exception:  # noqa: BLE001 —— 无 GPU 读数不影响主结论
            pass
        stop.wait(0.5)


def run_scenario(mode: str, frames: list[np.ndarray], duration: float,
                 lane_sess, perc_sess, depth_sess) -> dict:
    """mode: off / cpu / dml_nonblock / dml_block / dml_block_10。

    ``dml_block_10`` = DML 阻塞取锁、线观测降到 10Hz（隔拍检测，生产可由
    ``lane_obs.detect_every=2`` 表达），用于验证「减半频率换深度」是否够用。"""
    stop = threading.Event()
    beat = Beat()
    lock = threading.Lock()
    period = 1.0 / LANE_HZ
    lane_period = period * (2.0 if mode.endswith("_10") else 1.0)
    lane_inp = lane_sess.get_inputs()[0].name
    lane_is_dml = lane_sess.get_providers()[0] == "DmlExecutionProvider"
    perc_inp = perc_sess.get_inputs()[0].name
    depth_inp = depth_sess.get_inputs()[0].name

    tick = {"n": 0, "wait": [], "run": []}
    depth = {"attempt": 0, "ok": 0, "skip": 0, "run": [], "chain": []}
    lane = {"attempt": 0, "ok": 0, "skip": 0, "run": [], "frame": [], "cpu": None}
    tick["cpu"] = depth["cpu"] = None
    gpu: list[float] = []

    def tick_loop() -> None:
        tid = threading.get_native_id()
        c0 = thread_cpu_s(tid)
        t0 = time.perf_counter()
        n = 0
        idx = 0
        while not stop.is_set():
            n += 1
            img = frames[idx % len(frames)]
            idx += 1
            blob = yolo_blob(img)
            tw = time.perf_counter()
            with lock:
                wait_ms = (time.perf_counter() - tw) * 1000.0
                tr = time.perf_counter()
                perc_sess.run(None, {perc_inp: blob})
                run_ms = (time.perf_counter() - tr) * 1000.0
            beat.note()
            tick["wait"].append(wait_ms)
            tick["run"].append(run_ms)
            rest = t0 + n * (1.0 / LANE_HZ) - time.perf_counter()
            if rest > 0:
                stop.wait(rest)
        tick["n"] = n
        tick["cpu"] = _cpu_pct(tid, c0, time.perf_counter() - t0)

    def depth_loop() -> None:
        tid = threading.get_native_id()
        c0 = thread_cpu_s(tid)
        t0 = time.perf_counter()
        last_due = t0
        while not stop.is_set():
            last_due += DEPTH_INTERVAL_S
            rest = last_due - time.perf_counter()
            if rest > 0 and stop.wait(rest):
                break
            if time.perf_counter() - last_due > DEPTH_INTERVAL_S:
                last_due = time.perf_counter()   # 落后太多就不追拍，按生产 latest-only
            # 错峰：等一拍新的控制拍再开工（生产 _stagger_wait 同义）
            seq = beat.snapshot()
            beat.wait_newer(seq, timeout=STAGGER_WAIT_S)
            if stop.is_set():
                break
            img = frames[(depth["attempt"] * 7) % len(frames)]
            depth["attempt"] += 1
            ts = time.perf_counter()
            blob = depth_blob(img)
            if not lock.acquire(blocking=False):
                depth["skip"] += 1
                continue
            try:
                tr = time.perf_counter()
                depth_sess.run(None, {depth_inp: blob})
                depth["run"].append((time.perf_counter() - tr) * 1000.0)
            finally:
                lock.release()
            depth["chain"].append((time.perf_counter() - ts) * 1000.0)
            depth["ok"] += 1
        depth["cpu"] = _cpu_pct(tid, c0, time.perf_counter() - t0)

    def lane_loop() -> None:
        tid = threading.get_native_id()
        c0 = thread_cpu_s(tid)
        t0 = time.perf_counter()
        n = 0
        idx = 3
        while not stop.is_set():
            n += 1
            img = frames[idx % len(frames)]
            idx += 5
            lane["attempt"] += 1
            ts = time.perf_counter()
            blob = lane_blob(img)
            if mode == "off":
                lane["ok"] += 1
            elif mode == "cpu":
                lane_sess.run(None, {lane_inp: blob})
                lane["ok"] += 1
            else:
                got = (lock.acquire(blocking=False)
                       if mode == "dml_nonblock"
                       else lock.acquire(blocking=True))
                if not got:
                    lane["skip"] += 1
                else:
                    try:
                        tr = time.perf_counter()
                        out = lane_sess.run(None, {lane_inp: blob})
                        lane["run"].append((time.perf_counter() - tr) * 1000.0)
                    finally:
                        lock.release()
                    extract(prob_from_out(out))
                    lane["ok"] += 1
            lane["frame"].append((time.perf_counter() - ts) * 1000.0)
            rest = t0 + n * lane_period - time.perf_counter()
            if rest > 0:
                stop.wait(rest)
        lane["cpu"] = _cpu_pct(tid, c0, time.perf_counter() - t0)

    threads = [threading.Thread(target=tick_loop, daemon=True),
               threading.Thread(target=depth_loop, daemon=True),
               threading.Thread(target=lane_loop, daemon=True),
               threading.Thread(target=gpu_poll, args=(stop, gpu), daemon=True)]
    meter = LoadMeter()
    c_proc = process_cpu_s()
    t_wall = time.perf_counter()
    meter.start()
    for t in threads:
        t.start()
    time.sleep(duration)
    stop.set()
    for t in threads:
        t.join(timeout=5.0)
    sys_load = meter.stop()
    wall = time.perf_counter() - t_wall
    proc_pct = (process_cpu_s() - c_proc) / wall * 100.0
    cores = os.cpu_count() or 1

    def stats(rows):
        return {"n": len(rows), "p50": pct(rows, 0.50),
                "p95": pct(rows, 0.95), "max": pct(rows, 1.0)}

    return {
        "mode": mode, "lane_is_dml": lane_is_dml,
        "wall_s": round(wall, 1),
        "tick": {"n": tick["n"], "wait": stats(tick["wait"]),
                 "run": stats(tick["run"]), "cpu_pct": tick["cpu"]},
        "depth": {"attempt": depth["attempt"], "ok": depth["ok"],
                  "skip": depth["skip"],
                  "ok_rate": round(depth["ok"] / max(1, depth["attempt"]), 3),
                  "hz": round(depth["ok"] / wall, 2),
                  "run": stats(depth["run"]), "chain": stats(depth["chain"]),
                  "cpu_pct": depth["cpu"]},
        "lane": {"attempt": lane["attempt"], "ok": lane["ok"],
                 "skip": lane["skip"],
                 "ok_rate": round(lane["ok"] / max(1, lane["attempt"]), 3),
                 "hz": round(lane["ok"] / wall, 2),
                 "run": stats(lane["run"]),
                 "frame": stats(lane["frame"]), "cpu_pct": lane["cpu"]},
        "proc_cpu_pct_one_core": round(proc_pct, 1),
        "proc_cpu_machine_pct": round(proc_pct / cores, 1),
        "sys_load": sys_load,
        "gpu_util": stats(gpu),
    }


def run_slots(frames: list[np.ndarray], duration: float, lane_sess,
              perc_sess, depth_sess) -> dict:
    """固定排班：每拍 感知 → 线观测 → 深度（深度只在偶数拍，10Hz）。

    三个模型共用一把 DML 锁时不抢锁，由控制拍按固定顺序放行：
    各自的 GPU 起点确定，新鲜度的长尾只来自自身耗时，不来自碰撞。
    线观测/深度的预处理在拍内并行做，GPU 只占各自槽位；后处理在放锁后做。
    """
    period = 1.0 / LANE_HZ
    stop = threading.Event()
    cv = threading.Condition()
    lane_in, perc_in, depth_in = (s.get_inputs()[0].name
                                  for s in (lane_sess, perc_sess, depth_sess))
    st = {"lane_item": None, "lane_pre_seq": -1, "lane_slot_seq": -1,
          "lane_done_seq": -1, "lane_ready_seq": -1, "lane_ready_ts": 0.0,
          "lane_cap": 0.0,
          "depth_item": None, "depth_pre_seq": -1, "depth_slot_seq": -1,
          "depth_done_seq": -1, "depth_ready_seq": -1, "depth_ready_ts": 0.0,
          "depth_cap": 0.0}
    stats = {"lane_gpu": [], "lane_slot": [], "lane_ready": [], "lane_age": [],
             "depth_gpu": [], "depth_slot": [], "depth_ready": [], "depth_age": [],
             "perc_run": [], "tick_run": []}
    counts = {"tick": 0, "lane_due": 0, "lane_ok": 0, "lane_skip": 0,
              "depth_due": 0, "depth_ok": 0, "depth_skip": 0}

    def worker(name: str, sess, inp: str, gpu_ms_track: list, slot_track: list,
               ready_track: list, post) -> None:
        while not stop.is_set():
            with cv:
                while not stop.is_set() and st[name + "_item"] is None:
                    cv.wait(0.2)
                if stop.is_set():
                    return
                item = st[name + "_item"]
                st[name + "_item"] = None
                seq = item[2]
            blob = lane_blob(item[0]) if name == "lane" else depth_blob(item[0])
            with cv:
                st[name + "_pre_seq"] = seq
                cv.notify_all()
                deadline = item[1] + 0.045
                while (not stop.is_set() and st[name + "_slot_seq"] < seq
                       and time.perf_counter() < deadline):
                    cv.wait(0.002)
                if st[name + "_slot_seq"] < seq:
                    continue          # 本拍没轮到（调度器已跳过），直接等最新帧
            t0 = time.perf_counter()
            out = sess.run(None, {inp: blob})
            t1 = time.perf_counter()
            with cv:
                st[name + "_done_seq"] = seq
                cv.notify_all()
            post(out if name == "lane" else None)
            ready = time.perf_counter()
            with cv:
                gpu_ms_track.append((t1 - t0) * 1000.0)
                slot_track.append((t0 - item[1]) * 1000.0)
                ready_track.append((ready - item[1]) * 1000.0)
                # 消费点在下一拍读（模块每拍开头读 latest）：结果就绪后
                # 第一个拍边界才算被消费，帧龄向上取整到拍。
                ticks = max(1, math.ceil((ready - item[1]) / 0.05))
                stats[name + "_age"].append(ticks * 0.05 * 1000.0)
                st[name + "_ready_seq"] = seq
                st[name + "_ready_ts"] = ready
                st[name + "_cap"] = item[1]

    def lane_post(out) -> None:
        if out is not None:
            extract(prob_from_out(out))

    def depth_post(_out) -> None:
        # 生产深度后处理（找边/栅格）约 30ms；这里只建模就绪延迟
        time.sleep(0.030)

    lane_thread = threading.Thread(target=worker, args=(
        "lane", lane_sess, lane_in, stats["lane_gpu"], stats["lane_slot"],
        stats["lane_ready"], lane_post), daemon=True)
    depth_thread = threading.Thread(target=worker, args=(
        "depth", depth_sess, depth_in, stats["depth_gpu"], stats["depth_slot"],
        stats["depth_ready"], depth_post), daemon=True)

    def master() -> None:
        n = 0
        t0 = time.perf_counter() + 0.05
        end = t0 + duration
        while not stop.is_set() and time.perf_counter() < end:
            due = t0 + n * period
            rest = due - time.perf_counter()
            if rest > 0 and stop.wait(rest):
                break
            if stop.is_set():
                break
            n += 1
            counts["tick"] += 1
            img = frames[n % len(frames)]
            with cv:
                st["lane_item"] = (img, due, n)
                cv.notify_all()
                if n % 2 == 0:
                    st["depth_item"] = (img, due, n)
                    cv.notify_all()
            counts["lane_due"] += 1
            if n % 2 == 0:
                counts["depth_due"] += 1
            # 槽 0：感知（控制拍自己的 DML 调用）
            blob = yolo_blob(img)
            tp = time.perf_counter()
            perc_sess.run(None, {perc_in: blob})
            stats["perc_run"].append((time.perf_counter() - tp) * 1000.0)
            # 偶数拍：深度先上（后处理在锁外与线观测并行），再上线的槽
            if n % 2 == 0:
                dreason = _slot_run(cv, st, stop, "depth", n, due, 0.020,
                                    stats["depth_gpu"])
                if dreason == "ok":
                    counts["depth_ok"] += 1
                else:
                    counts["depth_skip"] += 1
                    counts["depth_" + dreason] = counts.get(
                        "depth_" + dreason, 0) + 1
            # 线观测槽（等预处理就绪，最多到拍后 15ms）
            reason = _slot_run(cv, st, stop, "lane", n, due, 0.015,
                               stats["lane_gpu"])
            if reason == "ok":
                counts["lane_ok"] += 1
            else:
                counts["lane_skip"] += 1
                counts["lane_" + reason] = counts.get("lane_" + reason, 0) + 1
            now = time.perf_counter()
            stats["tick_run"].append((now - due) * 1000.0)

    lane_thread.start()
    depth_thread.start()
    c0 = process_cpu_s()
    t_wall = time.perf_counter()
    master()
    wall = time.perf_counter() - t_wall
    stop.set()
    with cv:
        cv.notify_all()
    for t in (lane_thread, depth_thread):
        t.join(timeout=2.0)
    proc_pct = (process_cpu_s() - c0) / wall * 100.0
    gpu_sum = sum(stats["lane_gpu"]) + sum(stats["depth_gpu"]) + sum(
        stats["perc_run"])

    def stats_of(rows):
        return {"n": len(rows), "p50": pct(rows, 0.50),
                "p95": pct(rows, 0.95), "max": pct(rows, 1.0)}

    return {
        "mode": "slots", "wall_s": round(wall, 1), **counts,
        "perc": stats_of(stats["perc_run"]),
        "tick": stats_of(stats["tick_run"]),
        "lane": {"gpu": stats_of(stats["lane_gpu"]),
                 "slot": stats_of(stats["lane_slot"]),
                 "ready": stats_of(stats["lane_ready"]),
                 "age": stats_of(stats["lane_age"])},
        "depth": {"gpu": stats_of(stats["depth_gpu"]),
                  "slot": stats_of(stats["depth_slot"]),
                  "ready": stats_of(stats["depth_ready"]),
                  "age": stats_of(stats["depth_age"])},
        "gpu_duty_pct": round(gpu_sum / max(1.0, wall * 10.0), 1),
        "proc_cpu_pct_one_core": round(proc_pct, 1),
    }


def _slot_run(cv, st, stop, name: str, seq: int, due: float,
              pre_wait_s: float, gpu_track: list) -> str:
    """等预处理就绪 → 放行 GPU → 等本次完成；返回 ok / pre_timeout / run_timeout。"""
    deadline = due + pre_wait_s
    with cv:
        while (not stop.is_set() and st[name + "_pre_seq"] < seq
               and time.perf_counter() < deadline):
            cv.wait(0.002)
        if st[name + "_pre_seq"] < seq:
            return "pre_timeout"
        st[name + "_slot_seq"] = seq
        cv.notify_all()
        limit = time.perf_counter() + 0.030
        while (not stop.is_set() and st[name + "_done_seq"] < seq
               and time.perf_counter() < limit):
            cv.wait(0.002)
        return "ok" if st[name + "_done_seq"] >= seq else "run_timeout"


def print_slots(row: dict) -> None:
    print(f"[slots] {row['wall_s']}s {row['tick']['n']} 拍  "
          f"感知 run p50={row['perc']['p50']} p95={row['perc']['p95']}ms  "
          f"整拍 p50={row['tick']['p50']} p95={row['tick']['p95']}ms")
    for name, due, ok in (("线观", row["lane_due"], row["lane_ok"]),
                          ("深度", row["depth_due"], row["depth_ok"])):
        item = row["lane"] if name == "线观" else row["depth"]
        print(f"  {name} {ok}/{due}（跳过 {due - ok}）  "
              f"GPU p50={item['gpu']['p50']} p95={item['gpu']['p95']}ms  "
              f"槽延迟 p50={item['slot']['p50']} p95={item['slot']['p95']}ms  "
              f"就绪 p50={item['ready']['p50']} p95={item['ready']['p95']}ms  "
              f"帧龄 p50={item['age']['p50']} p95={item['age']['p95']}ms")
    print(f"  GPU 占用 {row['gpu_duty_pct']}%，进程 CPU "
          f"{row['proc_cpu_pct_one_core']}%（单核）")
    reasons = {k: v for k, v in row.items()
               if k.startswith(("lane_", "depth_"))
               and k.endswith(("_timeout", "_blocked"))}
    if reasons:
        print(f"  超时/让锁原因：{reasons}")


def _cpu_pct(tid: int, start: float | None, wall: float) -> float | None:
    if start is None:
        return None
    now = thread_cpu_s(tid)
    if now is None:
        return None
    return round((now - start) / max(1e-6, wall) * 100.0, 1)


# ── 输出 ────────────────────────────────────────────────────────────

def print_solo(row: dict) -> None:
    st = row["stages"]
    print(f"[{row['name']}] providers={row['providers']}")
    print(f"   阶段 CPU/wall(ms): pre {st['pre']['cpu_ms']}/{st['pre']['wall_ms']}"
          f"  run {st['run']['cpu_ms']}/{st['run']['wall_ms']}"
          f"  decode {st['decode']['cpu_ms']}/{st['decode']['wall_ms']}"
          f"  extract {st['extract']['cpu_ms']}/{st['extract']['wall_ms']}"
          f"  badge {st['badge']['cpu_ms']}/{st['badge']['wall_ms']}")
    cores = row["frame_cpu_ms"] * LANE_HZ / 1000.0
    load = row["sys_load"]
    print(f"   整帧 p50={row['frame_p50_ms']}ms p95={row['frame_p95_ms']}ms "
          f"CPU={row['frame_cpu_ms']}ms  → {LANE_HZ:.0f}Hz 下 {cores:.2f} 核 "
          f"({cores / (os.cpu_count() or 1) * 100:.1f}% 整机)"
          f"  整机 CPU（含本探针）p50={load['p50']}% max={load['max']}%")


def print_scenario(row: dict) -> None:
    t, d, ln = row["tick"], row["depth"], row["lane"]
    print(f"[{row['mode']}] {row['wall_s']}s  lane_is_dml={row['lane_is_dml']}  "
          f"进程 {row['proc_cpu_pct_one_core']}% 单核 / "
          f"{row['proc_cpu_machine_pct']}% 整机  "
          f"GPU {row['gpu_util']['p50']}%（max {row['gpu_util']['max']}）  "
          f"整机 CPU（含本探针）p50={row['sys_load']['p50']}% "
          f"max={row['sys_load']['max']}%")
    print(f"   感知: {t['n']} 拍 run p50={t['run']['p50']} p95={t['run']['p95']} "
          f"max={t['run']['max']}ms 锁等 p50={t['wait']['p50']} "
          f"max={t['wait']['max']}ms 线程CPU={t['cpu_pct']}%")
    print(f"   深度: 尝试 {d['attempt']} 成功 {d['ok']} 让锁 {d['skip']} "
          f"({d['hz']}Hz, ok {d['ok_rate'] * 100:.0f}%) run p50={d['run']['p50']} "
          f"p95={d['run']['p95']}ms 全链 p50={d['chain']['p50']}ms "
          f"线程CPU={d['cpu_pct']}%")
    print(f"   线观: 尝试 {ln['attempt']} 成功 {ln['ok']} 让锁 {ln['skip']} "
          f"({ln['hz']}Hz, ok {ln['ok_rate'] * 100:.0f}%) run p50={ln['run']['p50']} "
          f"帧 p50={ln['frame']['p50']} p95={ln['frame']['p95']}ms "
          f"线程CPU={ln['cpu_pct']}%")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="线观测 DML 代价与锁争抢探针")
    parser.add_argument("--frames", default=str(FRAMES_DIR))
    parser.add_argument("--solo-frames", type=int, default=40)
    parser.add_argument("--duration", type=float, default=12.0,
                        help="每个争抢场景的时长（秒）")
    parser.add_argument("--reps", type=int, default=30, help="单帧阶段重复次数")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--lane-fp16", action="store_true",
                        help="线观测用现场转换的 FP16 模型（只跑关键争抢场景）")
    parser.add_argument("--slots", action="store_true",
                        help="只跑固定排班模拟（感知→线观→深度，深度 10Hz）")
    args = parser.parse_args()

    frames_dir = Path(args.frames)
    print(disable_power_throttling(), flush=True)
    files = sorted(frames_dir.glob("*.jpg"))[:args.solo_frames]
    frames = []
    for path in files:
        bgr = cv2.imread(str(path))
        if bgr is not None:
            frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    if not frames:
        print(f"没有可用帧：{frames_dir}", flush=True)
        return 2
    print(f"帧源 {frames_dir.name} × {len(frames)} 张 {frames[0].shape}", flush=True)

    lane_dml_path = LANE_ONNX
    if args.lane_fp16:
        from onnxconverter_common import float16
        import onnx
        import tempfile
        lane_dml_path = (Path(tempfile.gettempdir()) / "lane_probe_fp16.onnx")
        onnx.save(float16.convert_float_to_float16(
            onnx.load(str(LANE_ONNX)), keep_io_types=True), str(lane_dml_path))
        print(f"线观测 DML 模型：FP16（{lane_dml_path.name}）", flush=True)

    cpu_sess = lane_session("cpu")
    dml_sess = lane_session("dml", model_path=lane_dml_path)
    perc_sess = perception_session()
    depth_sess = depth_session()
    # 预热：DML 首帧含 kernel 编译（生产由开局倒计时吸收），不计入统计
    for sess, blob in ((cpu_sess, lane_blob(frames[0])),
                       (dml_sess, lane_blob(frames[0])),
                       (perc_sess, yolo_blob(frames[0])),
                       (depth_sess, depth_blob(frames[0]))):
        for _ in range(4):
            sess.run(None, {sess.get_inputs()[0].name: blob})

    report: dict = {"frames": [p.name for p in files],
                    "solo": [], "scenarios": []}
    if args.slots:
        row = run_slots(frames, args.duration, dml_sess, perc_sess, depth_sess)
        report["slots"] = row
        print_slots(row)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("\n== ① 单帧阶段成本（重复取均值，CPU 为进程累计口径） ==", flush=True)
    for sess, name in ((cpu_sess, "lane_cpu_4t"),
                       (dml_sess, "lane_dml_fp16" if args.lane_fp16 else "lane_dml")):
        row = solo_bench(sess, name, frames, reps=args.reps)
        report["solo"].append(row)
        print_solo(row)

    print("\n== ①b CPU 前向：线程数 × ORT 自旋 ==", flush=True)
    matrix = thread_matrix(frames, reps=20)
    report["thread_matrix"] = matrix
    for row in matrix:
        print(f"   threads={row['threads']} spin={row['spin']:7s} "
              f"run p50={row['run_p50_ms']}ms p95={row['run_p95_ms']}ms "
              f"CPU={row['run_cpu_ms']}ms")

    print("\n== ② 生产节拍下的锁争抢（感知 20Hz 持锁 / 深度 60ms 错峰） ==",
          flush=True)
    modes = (("off", "dml_block", "dml_block_10") if args.lane_fp16
             else ("off", "cpu", "dml_nonblock", "dml_block", "dml_block_10"))
    for mode in modes:
        row = run_scenario(mode, frames, args.duration,
                           cpu_sess if mode == "cpu" else dml_sess,
                           perc_sess, depth_sess)
        report["scenarios"].append(row)
        print_scenario(row)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
