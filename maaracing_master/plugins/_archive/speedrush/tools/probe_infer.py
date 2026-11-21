#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""感知推理诊断：适配器映射 / 图融合 / CPU 回退 / 延迟，一次看清。

**要回答什么**
1. 这块机器上有哪些 DXGI 适配器、ORT 实际拿到的 provider 是谁（双显卡笔记本上
   ``device_id`` 指的是哪块卡，过去只能靠猜）；
2. 整张图有没有被 DirectML 融合成一个节点——**设置 ``optimized_model_filepath``
   会让融合失效**，前向从 3.5 ms 掉到 6.0 ms（本机实测），但启动日志里看不出来；
3. 有没有算子落到 CPU（DML 分区失败时通常伴随明显变慢）；
4. 生产会话选项下的真实前向与完整 detect 延迟分布。

**为什么单独成脚本**：融合计数要靠 ORT profiler，而 profiler 只该在诊断时开。
生产启动路径只记 provider 与回退原因，不跑 profiler。

**适配器名称不可信**：本机 DXGI/WMI 把 RTX 4060 报成 GTX 1050 Ti（驱动侧伪装），
``nvidia-smi`` 才是真实身份。故本脚本把两边的结果并列输出，不互相覆盖。

用法：
    python maaracing_master/plugins/speedrush/tools/probe_infer.py
    python maaracing_master/plugins/speedrush/tools/probe_infer.py --compare-legacy
    python maaracing_master/plugins/speedrush/tools/probe_infer.py --frames <raw目录> --limit 40
    python maaracing_master/plugins/speedrush/tools/probe_infer.py --json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402

from maaracing_master.core.yolo_detector import (  # noqa: E402
    YOLODetector,
    build_session_options,
)

DEFAULT_MODEL = (REPO / "maaracing_master" / "plugins" / "speedrush"
                 / "resources" / "models" / "perception" / "model.onnx")

# DML 会话析构后再 run 另一个会话会段错误（onnxruntime-directml 1.24.4）：
# 本进程创建的会话全部保活到退出。
_SESSIONS: list = []


# ---------- 适配器枚举 ----------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str) -> None:
        super().__init__()
        value = uuid.UUID(text)
        self.Data1, self.Data2, self.Data3 = value.time_low, value.time_mid, value.time_hi_version
        for i, byte in enumerate(value.bytes[8:]):
            self.Data4[i] = byte


class _AdapterDesc1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", ctypes.c_longlong),
        ("Flags", wintypes.UINT),
    ]


def dxgi_adapters() -> list[dict]:
    """按 DXGI 枚举顺序列出适配器（= DML device_id 的顺序）。失败返回空表。"""
    try:
        dxgi = ctypes.WinDLL("dxgi")
        fn = dxgi.CreateDXGIFactory1
        fn.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        fn.restype = ctypes.c_long
        factory = ctypes.c_void_p()
        guid = _GUID("770aae78-f26f-4dba-a829-253c83d1b387")   # IDXGIFactory1
        if fn(ctypes.byref(guid), ctypes.byref(factory)) != 0:
            return []
        vtable = ctypes.cast(
            ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0],
            ctypes.POINTER(ctypes.c_void_p))
        enum_adapters = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p))(vtable[12])
        get_desc = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_AdapterDesc1))
        release = ctypes.CFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)

        out: list[dict] = []
        for index in range(16):
            adapter = ctypes.c_void_p()
            if enum_adapters(factory, index, ctypes.byref(adapter)) != 0:
                break
            table = ctypes.cast(
                ctypes.cast(adapter, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0],
                ctypes.POINTER(ctypes.c_void_p))
            desc = _AdapterDesc1()
            get_desc(table[10])(adapter, ctypes.byref(desc))
            out.append({
                "index": index,
                "name": desc.Description,
                "vendor_id": f"0x{desc.VendorId:04x}",
                "vram_mb": desc.DedicatedVideoMemory // (1024 * 1024),
                "software": bool(desc.Flags & 2),
                "luid": int(desc.AdapterLuid),
            })
            release(table[2])(adapter)
        release(vtable[2])(factory)
        return out
    except Exception:  # noqa: BLE001 —— 诊断工具，枚举失败不该拦住主流程
        return []


def nvidia_smi_identity() -> list[dict]:
    """NVML 口径的真实 GPU 身份（DXGI/WMI 的适配器名可能被驱动伪装）。"""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=False)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            rows.append({"name": parts[0], "memory": parts[1],
                         "driver": parts[2], "compute_cap": parts[3]})
    return rows


# ---------- 会话与融合 ----------

def _input_shape(session) -> tuple[int, int]:
    shape = session.get_inputs()[0].shape
    height = shape[2] if isinstance(shape[2], int) else 640
    width = shape[3] if isinstance(shape[3], int) else 640
    return int(height), int(width)


def probe_session(model: Path, *, legacy_opt: bool = False,
                  rounds: int = 30, profile_runs: int = 3) -> dict:
    """建会话测延迟，并用**独立**的 profiling 会话统计每次 run 的节点数。

    ``legacy_opt=True`` 复现修复前的配置（设置 optimized_model_filepath），
    用于 A/B 报告，不应出现在生产路径里。

    计时与计数分两个会话：profiler 一旦开启会记录**所有** run，计时轮次会被一起
    数进去；独立会话只跑固定次数，节点数才可除。
    """
    opts = build_session_options()
    if legacy_opt:
        opts.optimized_model_filepath = os.path.join(
            tempfile.gettempdir(), "probe_infer_legacy.onnx")
    session = ort.InferenceSession(
        str(model), opts, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    _SESSIONS.append(session)

    height, width = _input_shape(session)
    feed = {session.get_inputs()[0].name:
            np.random.rand(1, 3, height, width).astype(np.float32)}
    for _ in range(5):
        session.run(None, feed)
    times = []
    for _ in range(rounds):
        started = time.perf_counter()
        session.run(None, feed)
        times.append((time.perf_counter() - started) * 1000.0)

    profiling_opts = build_session_options()
    if legacy_opt:
        profiling_opts.optimized_model_filepath = os.path.join(
            tempfile.gettempdir(), "probe_infer_legacy_prof.onnx")
    profiling_opts.enable_profiling = True
    # profiler 缺省写到当前工作目录；固定落到临时目录，别污染仓库根
    profiling_opts.profile_file_prefix = os.path.join(
        tempfile.gettempdir(), "probe_infer_profile")
    profiler = ort.InferenceSession(
        str(model), profiling_opts,
        providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    _SESSIONS.append(profiler)
    profile_feed = {profiler.get_inputs()[0].name: feed[session.get_inputs()[0].name]}
    for _ in range(profile_runs):
        profiler.run(None, profile_feed)
    events = json.load(open(profiler.end_profiling(), encoding="utf-8"))
    node_rows = [e for e in events if e.get("cat") == "Node"]
    cpu_rows = [e for e in node_rows if "CPUExecutionProvider" in e.get("name", "")]
    times.sort()
    return {
        "legacy_optimized_path": legacy_opt,
        "providers": list(session.get_providers()),
        "input_hw": [height, width],
        "nodes_per_run": round(len(node_rows) / max(1, profile_runs), 1),
        "cpu_fallback_nodes": len(cpu_rows) // max(1, profile_runs),
        "run_ms_p50": round(statistics.median(times), 2),
        "run_ms_p95": round(times[max(0, int(len(times) * 0.95) - 1)], 2),
    }


def detect_latency(model: Path, frames_dir: Path | None, limit: int) -> dict | None:
    """走生产检测路径（预处理 + run + 解码/NMS）的延迟与检出统计。"""
    if frames_dir is None or not frames_dir.is_dir():
        return None
    import cv2

    files = sorted(frames_dir.glob("*.jpg"))[:limit]
    if not files:
        return None
    detector = YOLODetector(str(model), conf=0.35, iou=0.45)
    images = []
    for path in files:
        bgr = cv2.imread(str(path))
        if bgr is not None:
            images.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    if not images:
        return None
    for image in images[:3]:
        detector(image)
    call_ms, run_ms = [], []
    counts = {"coin": 0, "car": 0, "bonus_car": 0}
    for image in images:
        started = time.perf_counter()
        by_class, _dets, _raw = detector(image)
        call_ms.append((time.perf_counter() - started) * 1000.0)
        run_ms.append(detector.last_run_ms)
        for name in counts:
            counts[name] += len(by_class.get(name, []))
    call_ms.sort()
    run_ms.sort()
    return {
        "frames": len(images),
        "provider": detector.provider,
        "call_ms_p50": round(statistics.median(call_ms), 2),
        "call_ms_p95": round(call_ms[int(len(call_ms) * 0.95) - 1], 2),
        "run_ms_p50": round(statistics.median(run_ms), 2),
        "detections": counts,
    }


# ---------- 输出 ----------

def main() -> int:
    # Windows 控制台默认 cp936，诊断输出含中文会乱码
    try:
        # sys.stdout 类型声明是 TextIO，reconfigure 是 TextIOWrapper 的方法——经 getattr 取用
        reconf = getattr(sys.stdout, "reconfigure", None)
        if callable(reconf):
            reconf(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="感知推理会话与适配器诊断")
    parser.add_argument("--model", default=str(DEFAULT_MODEL), help="ONNX 模型路径")
    parser.add_argument("--frames", default=None, help="真帧目录（测完整 detect 延迟）")
    parser.add_argument("--limit", type=int, default=40, help="真帧取样上限")
    parser.add_argument("--rounds", type=int, default=30, help="前向计时轮数")
    parser.add_argument("--compare-legacy", action="store_true",
                        help="同时测量修复前的会话配置（会明显更慢）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    model = Path(args.model)
    if not model.is_file():
        print(f"模型不存在: {model}", file=sys.stderr)
        return 2

    report: dict = {
        "nvml": nvidia_smi_identity(),
        "dxgi": dxgi_adapters(),
        "session": probe_session(model, rounds=args.rounds),
        "detect": detect_latency(model, Path(args.frames) if args.frames else None,
                                 args.limit),
    }
    if args.compare_legacy:
        report["session_legacy"] = probe_session(
            model, legacy_opt=True, rounds=args.rounds)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("== NVML（真实身份，以此为准） ==")
    for gpu in report["nvml"] or [{"name": "（nvidia-smi 不可用）"}]:
        print("  ", gpu)
    print("== DXGI 适配器（顺序 = DML device_id；名称可能被驱动伪装） ==")
    for ad in report["dxgi"] or [{"index": "-", "name": "（枚举失败）"}]:
        print(f"   [{ad.get('index')}] {ad.get('name')}  "
              f"vendor={ad.get('vendor_id')} vram={ad.get('vram_mb')}MB "
              f"software={ad.get('software')}")
    print("== 会话 ==")
    for key in ("session", "session_legacy"):
        item = report.get(key)
        if not item:
            continue
        print(f"   {key}: providers={item['providers']} "
              f"input={item['input_hw']} 节点/次={item['nodes_per_run']} "
              f"CPU回退={item['cpu_fallback_nodes']} "
              f"run p50={item['run_ms_p50']}ms p95={item['run_ms_p95']}ms")
    if report["session_legacy"]:
        new, old = report["session"], report["session_legacy"]
        print(f"   修复收益: {old['run_ms_p50']} → {new['run_ms_p50']} ms")
    if report["detect"]:
        item = report["detect"]
        print("== 完整 detect（真帧） ==")
        print(f"   {item['frames']} 帧 provider={item['provider']} "
              f"call p50={item['call_ms_p50']}ms p95={item['call_ms_p95']}ms "
              f"run p50={item['run_ms_p50']}ms 检出={item['detections']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
