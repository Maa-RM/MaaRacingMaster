#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线回放台（真业务代码版）：磁盘供帧驱动 speedrush 驾驶环，试着复现驾驶期推理停顿。

**与 repro.py 的分工**：repro.py 自包含重建 DML 调用面（不 import 业务代码），已证
「只把推理循环起来」不复现（V1~V4 最大 70~80ms）。但那一轮的变量集不齐——没有真
业务的并发线程组，阴性结果就没有排除力。本台跑**真业务模块**，把录制模式那一整套
线程补齐：控制环（感知 DML）、深度链（MoGe DML）、深度调试、HUD 采样（OCR）与落盘、
车道观测、录制帧编码落盘、录制手柄高频采样、调试落盘、两条流水汇、性能采样、显卡探针。

**为什么选录制模式**：它不 acquire 手柄、不要求窗口独占（模块按配置放宽了这两条
启动约束），是最省伪造的形态；而生产停顿恰恰是在人工录制局复现的。

**接缝**：驾驶环只经注入的采集协议取帧（`ctx.capture.frame_with_age()` 四元组）。
两种采集形态：`--capture live`（默认）起生产同款 WGC 中心采集器采真实游戏窗口、
套生产 CaptureAdapter——检测返回什么不重要，要的是采集线程、真实帧到达节奏与断流
动力学在场；`--capture disk` 用磁盘供帧替身循环录制素材，供帧做成「独立线程按录制
时的真实间隔解码发布、消费者读最新帧」，与中心缓存同语义（在消费者线程按需解码会
把解码成本算到控制环头上，等于又少控一个变量）。

**标尺独立于业务代码**（实验区 C 类硬约束）：复现判据不只看业务自己的计时，还要
现场抓拍文件（含 Win32 线程 CPU 时间增量）与外部 nvidia-smi 调用时长同窗佐证。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/dml_stall_repro/replay_business.py --seconds 180
    # 指定素材局（缺省用首份生产现场那一局）：
    ... --session 20261107_203433_manual_p1

产物落用户数据目录（debug/speedrush/ 与 logs/），不入库；每次运行另落一份
`replay_fidelity.json` 声明本轮覆盖与未覆盖的变量。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import cv2                                                    # noqa: E402
import numpy as np                                            # noqa: E402

from maaracing_master.core.logger import logger               # noqa: E402
from maaracing_master.core.perf_sampler import (              # noqa: E402
    ensure_perf_sampler_started, perf_sampler)
from maaracing_master.plugins.speedrush import module as sr   # noqa: E402

DEFAULT_SESSION = "20261107_203433_manual_p1"

# 生产现场线程清单（取自THREAD_LABELS 与首份现场抓栈）为基准逐项对齐；
# 未覆盖项必须显式声明——变量集不齐的阴性结果没有排除力。
COVERED = (
    "控制环 + 感知 DML（真 _drive_loop）",
    "深度链 MoGe DML + 深度调试落盘（真控制链装配）",
    "HUD 采样 OCR + HUD 落盘（真 HudObserver）",
    "车道观测 worker（真控制链装配）",
    "录制帧 JPEG 编码落盘 + 录制手柄采样线程（真 DriveRecorder）",
    "调试图落盘、玩法与显卡两条流水汇、显卡探针与现场抓拍",
    "性能采样器（真 PerfSampler，含其周期性显卡查询与逐线程时间采样）",
    "DML 全局锁与错峰节拍（生产同一份 core.dml_lock）",
    "OCR 进程级绑核（生产同一份 core.ocr，不改）",
)
UNCOVERED_DISK = (
    "WGC 中心采集线程：帧由磁盘解码发布，没有真实采集回调与断流 dynamics",
    "真实游戏窗口画面与桌面合成呈现负载：disk 模式不采窗口",
    "物理手柄输入：采样线程照跑但读不到按键，pads.jsonl 为空（生产同阶段 6994 条），"
    "其 CPU 与落盘负载低于生产",
)
UNCOVERED_LIVE = (
    "物理手柄输入：采样线程照跑但读不到按键，pads.jsonl 为空（生产同阶段 6994 条），"
    "其 CPU 与落盘负载低于生产",
)
COVERED_LIVE_EXTRA = (
    "WGC 中心采集线程与真实帧到达/断流动力学（真 WgcCapture + 生产 CaptureAdapter）",
    "真实游戏窗口画面与桌面合成呈现负载（采 live 窗口，需操作者把游戏开在驾驶页）",
)


class DiskCapture:
    """按录制时间戳供帧：独立线程解码发布，消费者读最新（中心缓存语义）。"""

    def __init__(self, session_dir: Path, stop: threading.Event):
        rows = [json.loads(line) for line in
                (session_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
        if not rows:
            raise SystemExit(f"帧索引为空：{session_dir}")
        self._dir = session_dir
        self._rows = rows
        self._stop = stop
        self._lk = threading.Lock()
        self._cur: tuple[np.ndarray, int, int] | None = None
        self._published = 0
        self._th = threading.Thread(target=self._produce, daemon=True,
                                    name="replay-capture")

    def start(self) -> None:
        self._th.start()

    def _produce(self) -> None:
        base = time.monotonic_ns()
        t0 = self._rows[0]["ts_ns"]
        span = self._rows[-1]["ts_ns"] - t0
        seam = self._rows[1]["ts_ns"] - t0      # 循环接缝按首帧间隔补
        fid0 = self._rows[0]["frame_id"]
        fid_span = (self._rows[-1]["frame_id"] - fid0
                    + (self._rows[1]["frame_id"] - fid0))   # 跨轮接缝按首帧号间隔补
        pass_no = 0
        while not self._stop.is_set():
            for row in self._rows:
                if self._stop.is_set():
                    return
                # 保持录制时的真实帧间隔；循环播放时时间轴继续往前走
                due = base + pass_no * (span + seam) + (row["ts_ns"] - t0)
                lag = (due - time.monotonic_ns()) / 1e9
                if lag > 0 and self._stop.wait(lag):
                    return
                bgr = cv2.imread(str(self._dir / row["file"]), cv2.IMREAD_COLOR)
                if bgr is None:
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                # 帧号必须跨轮单调递增：复用原始帧号会让新鲜度守卫把第二遍起
                # 的帧全判成旧帧，驾驶环停止产拍而探针照采（首轮实测踩到）
                fid = fid0 + pass_no * fid_span + (row["frame_id"] - fid0)
                with self._lk:
                    self._cur = (rgb, fid, time.monotonic_ns())
                    self._published += 1
            pass_no += 1

    def screenshot(self):
        with self._lk:
            return None if self._cur is None else self._cur[0]

    def frame_with_age(self):
        with self._lk:
            if self._cur is None:
                return (None, 0, 0, float("inf"))
            rgb, fid, arrived = self._cur
        return (rgb, fid, arrived, (time.monotonic_ns() - arrived) / 1e6)

    @property
    def published(self) -> int:
        with self._lk:
            return self._published


class RealLifecycle:
    """真墙钟可中断睡眠（离线台要量真实时序，不能用测试里的假时钟）。"""

    def __init__(self, stop: threading.Event):
        self._stop = stop

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def sleep(self, seconds: float) -> bool:
        return not self._stop.wait(max(seconds, 0.0))


class AnchorStub:
    """离线没有游戏画面可识别：驾驶页锚点恒命中，进商店横幅恒不命中。

    横幅若命中会让阶段 1 提前结束，故必须按节点名分流（口径同测试替身）。
    """

    def probe(self, node: str, **kwargs):
        return (sr.ProbeOutcome.MISS if node == sr.STORE_BANNER_NODE
                else sr.ProbeOutcome.HIT)

    def run(self, entry=None, reached=None, *, save=None) -> bool:
        return entry != sr.STORE_BANNER_NODE


def _live_capture():
    """真采集：起生产同款 WGC 中心采集器，套生产 CaptureAdapter。

    检测返回什么不重要——要的是采集线程、真实帧到达节奏与断流动力学在场。
    启动口径照 controller：max_fps=60、还原最小化、等首帧 2s 兜底。
    """
    from maaracing_master.core.capabilities import CaptureAdapter
    from maaracing_master.core.wgcap import WgcCapture
    from maaracing_master.core.window_utils import (ensure_window_restored,
                                                   find_game_hwnd,
                                                   resize_game_window_720p)
    hwnd = find_game_hwnd(verbose=False)
    if not hwnd:
        raise SystemExit("live 采集找不到游戏窗口：先把游戏开在驾驶页")
    try:
        ensure_window_restored(hwnd)
        # 生产 connect 时统一归一 720p：模板与 ROI 按 720p 客户区设计，不归一
        # 则锚点识别恒 0 分、阶段秒退（真图冒烟实测踩到）
        resize_game_window_720p(hwnd)
    except Exception:                              # 还原/归一失败不阻断启动
        pass
    cap = WgcCapture(hwnd, max_fps=60)
    cap.start()
    for _ in range(40):
        frame, *_ = cap.get_latest()
        if frame is not None:
            break
        time.sleep(0.05)
    else:
        cap.stop()
        raise SystemExit("WGC 启动后 2s 未收到首帧")
    return CaptureAdapter(SimpleNamespace(_wgc_capture=cap)), cap


class RecognitionLoadGraph:
    """真识别负载、退出判定解耦。

    probe 真跑跳转图模板匹配（CPU 负载与调用节奏在场），但驾驶页锚点恒报 HIT、
    商店横幅恒报 MISS——阶段寿命不取决于匹配成败，离线台才能长跑。识别/检测结果
    本就不消费（操作者口径：不在乎返回结果），故解耦只影响导航逻辑、不影响负载。
    真匹配对窗口几何/裁剪偏移敏感（实测齿轮偏约 10px 就 ROI 0 分），不该让复现台
    的寿命押在这上面。
    """

    def __init__(self, real):
        self._real = real

    def probe(self, node: str, **kwargs):
        self._real.probe(node, **kwargs)      # 真跑：负载在场，结果不消费
        return (sr.ProbeOutcome.MISS if node == sr.STORE_BANNER_NODE
                else sr.ProbeOutcome.HIT)

    def run(self, entry=None, reached=None, *, save=None) -> bool:
        return True

    def shutdown(self) -> bool:
        return self._real.shutdown()


def _real_graph(ctx):
    """真跳转图 + 退出解耦壳：只读 probe 提供模板识别负载（不点、不持手柄）。"""
    from maaracing_master.core.nav_graph import NavGraph
    from maaracing_master.plugins.speedrush import IMAGE_DIR, PIPELINE_DIR
    graph = NavGraph(ctx)
    graph.add_plugin(PIPELINE_DIR, IMAGE_DIR)
    if not graph.load():
        raise SystemExit("真跳转图加载失败")
    return RecognitionLoadGraph(graph)


def _find_session(name: str) -> Path:
    from maaracing_master.core.paths import debug_dir
    path = debug_dir() / "speedrush" / name
    if not (path / "frames.jsonl").is_file():
        raise SystemExit(f"素材局不存在或缺帧索引：{path}")
    return path


def _tick_audit(trace_dir: Path, requested_s: float) -> dict:
    """驾驶环实际产拍画像。率塌了或只覆盖一小段就不是有效阴性。

    首轮实测踩到过：帧号跨轮复用 → 新鲜度守卫判旧帧 → 驾驶环静默停产拍，而显卡
    探针照采满全程，看起来像「跑了 180s 没复现」。故阴性结论必须先过这一关。
    """
    path = trace_dir / "speedrush.jsonl"
    if not path.is_file():
        return {"控制拍": 0, "有效阴性": False, "原因": "无玩法流水"}
    ticks = [row for row in (json.loads(line) for line in
                             path.read_text(encoding="utf-8").splitlines())
             if "yolo_run_ms" in row]
    if len(ticks) < 2:
        return {"控制拍": len(ticks), "有效阴性": False, "原因": "产拍不足"}
    span = (ticks[-1]["ts_mono_ns"] - ticks[0]["ts_mono_ns"]) / 1e9
    hz = (len(ticks) - 1) / span if span > 0 else 0.0
    ok = hz >= 10.0 and span >= 0.8 * requested_s
    out = {"控制拍": len(ticks), "产拍跨度s": round(span, 1), "实际Hz": round(hz, 1),
           "有效阴性": ok}
    if not ok:
        out["原因"] = ("产拍率低于 10Hz" if hz < 10.0 else "产拍未覆盖请求时长的 80%")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", default=DEFAULT_SESSION, help="素材局目录名（disk 模式）")
    ap.add_argument("--capture", choices=("live", "disk"), default="live",
                    help="live=真 WGC 采游戏窗口（默认）；disk=磁盘供帧")
    ap.add_argument("--graph", choices=("real", "stub"), default="real",
                    help="real=真跳转图只读 probe 提供模板识别负载（默认）；stub=锚点恒命中")
    ap.add_argument("--priority", choices=("normal", "high"), default="normal",
                    help="high=本进程优先级提 HIGH（单变量测 CPU 争抢假设；不需管理员，"
                         "让我方线程赢过背景进程）")
    ap.add_argument("--seconds", type=float, default=180.0, help="回放时长上限")
    ap.add_argument("--stop-on-stall", action="store_true", default=True,
                    help="抓到现场即停（默认开）")
    ap.add_argument("--background", default="",
                    help="操作者声明的本轮环境背景（游戏/模拟器/覆盖层/壁纸等开关状态）；"
                         "落进产物，供后人判读这轮阴性排除了哪一组变量")
    args = ap.parse_args()

    stop = threading.Event()
    wgc = None
    if args.capture == "live":
        capture, wgc = _live_capture()
        covered = list(COVERED) + list(COVERED_LIVE_EXTRA)
        uncovered = list(UNCOVERED_LIVE)

        def published():
            return None
    else:
        disk = DiskCapture(_find_session(args.session), stop)
        disk.start()
        capture = disk
        covered = list(COVERED)
        uncovered = list(UNCOVERED_DISK)

        def published():
            return disk.published
    from maaracing_master.core.nav_graph import WgcapController
    ctx = SimpleNamespace(lifecycle=RealLifecycle(stop), capture=capture,
                          debug=SimpleNamespace(enabled=True),
                          click_mode="real", intent_mode=False, hwnd=0,
                          bind_tasker=lambda t, r: t.bind(r, WgcapController(capture)))
    if args.graph == "real":
        covered.append("真跳转图模板识别负载（NavGraph.probe 真跑、只读不点不持手柄；"
                       "阶段退出判定与匹配结果解耦）")
    else:
        uncovered.append("导航图模板识别：锚点替身恒命中，省掉框架识别负载")

    print(f"=== 变量集声明（capture={args.capture}, graph={args.graph}）===")
    for item in covered:
        print("  [覆盖] " + item)
    for item in uncovered:
        print("  [未覆盖] " + item)

    logger.set_file_logging(True)      # 开日志会话：流水汇与显卡探针才绑得上
    out_dir = Path(logger.session_dir) if logger.session_dir else Path.cwd()
    perf_sampler.set_thread_labels(sr.SpeedRushModule.THREAD_LABELS)
    ensure_perf_sampler_started()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if args.priority == "high":
        import psutil
        psutil.Process().nice(psutil.HIGH_PRIORITY_CLASS)
    mod = sr.SpeedRushModule(ctx)        # type: ignore[arg-type]
    mod._record_mode = True              # 录制模式：影子观察，不碰手柄
    mod._drive_mode = "depth"
    mod._control_mode = False
    mod._running = True
    mod._graph = (_real_graph(ctx) if args.graph == "real"   # type: ignore[assignment]
                  else AnchorStub())

    recorder = mod._begin_recording(1, 1, stamp)
    if recorder is None:
        print("录制器启动失败，终止")
        stop.set()
        if args.graph == "real":
            mod._graph.shutdown()
        if wgc is not None:
            wgc.stop()
        perf_sampler.stop()
        logger.set_file_logging(False)
        return 1
    hud = mod._begin_hud(recorder.out_dir / "hud", 1, 1)

    result: dict = {"ok": False}

    def run_loop() -> None:
        try:
            result["ok"] = bool(mod._drive_loop(1, recorder, 1, stamp))
        except Exception as exc:                       # 离线台要把失败原样报出
            result["error"] = repr(exc)
            import traceback
            traceback.print_exc()

    th = threading.Thread(target=run_loop, daemon=True, name="replay-drive")
    th.start()
    trace_dir = out_dir / "trace"
    deadline = time.monotonic() + args.seconds
    stall: Path | None = None
    while time.monotonic() < deadline and th.is_alive():
        time.sleep(0.5)
        if args.stop_on_stall and trace_dir.is_dir():
            found = sorted(trace_dir.glob("stall_*.txt"))
            if found:
                stall = found[0]
                break
    mod._running = False
    stop.set()
    th.join(timeout=15.0)
    if hud is not None:
        hud.stop("stopped")
    recorder.stop("stopped")
    if args.graph == "real":
        mod._graph.shutdown()
    if wgc is not None:
        wgc.stop()
    perf_sampler.stop()
    logger.set_file_logging(False)

    fidelity = {"素材局": args.session, "采集模式": args.capture,
                "图模式": args.graph, "进程优先级": args.priority,
                "回放秒数上限": args.seconds,
                "操作者背景声明": args.background or "（未声明）",
                "发布帧数": published(), "驾驶环正常返回": result,
                "产拍自审": _tick_audit(trace_dir, args.seconds),
                "覆盖变量": covered, "未覆盖变量": uncovered,
                "日志会话": str(out_dir), "录制产物": str(recorder.out_dir),
                "现场文件": None if stall is None else str(stall)}
    (recorder.out_dir / "replay_fidelity.json").write_text(
        json.dumps(fidelity, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== 结果 ===")
    print(json.dumps(fidelity, ensure_ascii=False, indent=2))
    if stall is not None:
        print(f"\n复现：现场已落 {stall}")
        return 0
    if not fidelity["产拍自审"]["有效阴性"]:
        print(f"\n本轮无效（{fidelity['产拍自审'].get('原因', '产拍不足')}）："
              "这不是「没复现」，先修回放台再跑。")
        return 2
    print("\n本轮未复现。未覆盖变量仍在，阴性结果只排除已覆盖的那一组。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())