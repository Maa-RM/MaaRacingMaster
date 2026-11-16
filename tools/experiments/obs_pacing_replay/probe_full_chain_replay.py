#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""观测链消费时序回放：完整生产链离线跑，验收两个口径（线观测优先）。

**要回答什么**
1. 基线复刻（V0）：当前生产的提交/读取时序在离线回放里能否复现 trace 的
   实测数字（线读取龄 p50 ~72-76、深度 ~73-86）——回放的有效性标尺；
2. 三个候选时序能否把线观测读取龄压进 50ms（一拍），代价是什么：
   - V1 拍首提交：线帧在拍起点就交给后台（锚/灭点行/焦距全部用上一拍驻留值，
     与生产同源——生产在 ~20ms 提交时用的也是同一份驻留深度结果），读取点
     不动；预期发布龄 ~14-20ms，早读取点（帧龄 ~20ms）多数拍能读到本帧结果；
   - V2 生产提交位置 + 拍内晚读：提交不动，在拍内 ~40ms 处（追踪/决策前的
     自然空档）补一次读取并消费新结果；预期 p50 ~40ms、发布尾部越窗的拍
     退回下一拍（龄 ~90）；
   - V3 拍首提交 + 早读 + 晚读：早读照旧供灭点行/深度配对，晚读消费本帧
     结果；发布尾部（~21ms）也落在晚读窗内，预期两口径 p95 都进 50ms。

**与 lane_obs_dml 模拟的区别**：那里用固定睡眠建模深度后处理、消费龄按拍
边界向上取整推算；这里跑**真实生产组件**（StreetPerception / LaneObsWorker /
AsyncDepthRoadObserver / 真实标定与配置），消费龄逐拍实测（观测件自带的
det_ts_ns / last_age_ms），不取整、不建模。

**为什么 V1 的锚语义与生产等价**：生产在拍内 ~20ms 提交时，锚来自本拍深度
take 的驻留结果（帧 N-1 的深度，本拍 ~16ms 才发布）。拍首提交时该结果尚未
发布，锚退回上一拍已消费的同源驻留值（帧 N-2）——线网格锚是慢变量，差一拍
对锚判据（连续两拍步长门）的影响要实机验证，本探针只测时序，语义差记入
README 边界。

**口径**
- 线观测读取龄 = 读取时刻 − 消费快照的检测输入源戳（det_ts_ns），与生产
  trace 的 obs_age.lane 同源同义；按源戳去重得口径一（新结果首次消费龄），
  全拍收集为口径二（每拍实际使用龄，含复用）；
- 深度读取龄 = take 后观测件的 last_age_ms（口径起点=帧采集回调时刻，与
  生产 dgeo_age_ms 同源）；is_new 为口径一；
- 拍长 = 实测 beat 间隔（目标 50ms，超时记 overrun）。

**已知边界**
- 回放进程没有游戏/采集/调试落盘负载，CPU 争抢比生产轻——基线复刻若复现
  不了 trace（尤其深度本体 46ms 的争抢膨胀），以 V0 复刻差为准记录边界；
- 拍内「工作段」用睡眠占位（生产这里是规划器/决策的真实 Python 负载），
  V2 的晚读时机按生产 trace 的段账取 ~40ms。

用法::

    .venv/Scripts/python.exe tools/experiments/obs_pacing_replay/probe_full_chain_replay.py \
        --variant v0 --duration 12
    .venv/Scripts/python.exe tools/experiments/obs_pacing_replay/probe_full_chain_replay.py \
        --variant v1 --duration 12
    .venv/Scripts/python.exe tools/experiments/obs_pacing_replay/probe_full_chain_replay.py \
        --variant v2 --duration 12
    # 首行必须显示「退出节流 成功」，否则延迟数字作废（同 lane_obs_dml 教训）
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maaracing_master.plugins.speedrush import (  # noqa: E402
    DEPTH_MODEL_REL, PERCEPTION_MODEL_REL)
from maaracing_master.plugins.speedrush import model_assets  # noqa: E402
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.depth_geo import (  # noqa: E402
    AsyncDepthRoadObserver, DepthRoadObserver, load_session)
from maaracing_master.plugins.speedrush.lane_obs import LaneObsWorker  # noqa: E402
from maaracing_master.plugins.speedrush.perception import StreetPerception  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402

FRAMES_DIR = (REPO / "tools/experiments/speedrush_depthart_prod/frames"
              / "20261111_104222_p2_stable41")

BEAT_S = 0.05            # 生产控制拍 20Hz
LANE_PUSH_AT_S = 0.020   # V0/V2：生产提交位置（trace 线段账 cap p50 ~19-23ms）
EARLY_READ_AT_S = 0.020  # 生产读取位置：trace yolo 读取龄 p50 21-25ms（检测后 ~2ms）
LATE_READ_AT_S = 0.040   # V2/V3：拍内晚读时机（trace 段账：发布 p50 ~34ms 之后）
DEPTH_INTERVAL_S = 0.06  # 生产 AsyncDepthRoadObserver.INFER_MIN_INTERVAL_S


def disable_power_throttling() -> str:
    """退出 Windows 进程节能节流（EcoQoS）；未退出的延迟数字全部作废。"""
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
        state = _State(1, 0x1, 0)
        ok = kernel32.SetProcessInformation(
            handle, 4, ctypes.byref(state), ctypes.sizeof(state))
        return (f"节流前 control={before.ControlMask} state={before.StateMask}；"
                f"退出节流 {'成功' if ok else '失败'}")
    except Exception as exc:  # noqa: BLE001
        return f"无法查询/设置节流：{exc!r}"


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def fmt(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    return (f"n={len(xs):4d}  p50={pct(xs, 50):6.1f}  p90={pct(xs, 90):6.1f}"
            f"  p95={pct(xs, 95):6.1f}  p99={pct(xs, 99):6.1f}  max={max(xs):6.1f}")


def ratio(xs: list[float], cap: float) -> str:
    if not xs:
        return "n=0"
    ok = sum(1 for v in xs if v <= cap)
    return f"{ok / len(xs) * 100:5.1f}% ({ok}/{len(xs)})"


class Recorder:
    """逐拍记账：线/深两口径 + 发布龄 + 拍长。"""

    def __init__(self) -> None:
        self.lane_1: list[float] = []
        self.lane_2: list[float] = []
        self.lane_pub: list[float] = []
        self.depth_1: list[float] = []
        self.depth_2: list[float] = []
        self.yolo_1: list[float] = []
        self.beat_ms: list[float] = []
        self.lane_miss: int = 0      # 晚读点没等到本帧结果（退回上一帧）的拍数
        self._last_lane_ts = None
        self._last_depth_ts = None

    def lane_read(self, snap: dict | None, now: float, pub_ref: float | None) -> None:
        if not snap:
            return
        det_ts = snap.get("det_ts_ns")
        if not det_ts:
            return
        age = (now - det_ts / 1e9) * 1000.0
        self.lane_2.append(age)
        if det_ts != self._last_lane_ts:
            self._last_lane_ts = det_ts
            self.lane_1.append(age)
        else:
            self.lane_miss += 1
        if pub_ref is not None:
            self.lane_pub.append(pub_ref)

    def depth_read(self, age_ms: float | None, is_new: bool,
                   frame_ts_ns: int | None, now: float) -> None:
        if age_ms is None:
            return
        self.depth_2.append(age_ms)
        if is_new and frame_ts_ns:
            if frame_ts_ns != self._last_depth_ts:
                self._last_depth_ts = frame_ts_ns
                self.depth_1.append(age_ms)


def run_variant(variant: str, duration: float) -> Recorder:
    print(f"\n=== 变体 {variant}（{duration:.0f}s）===")
    cfg = load_decision()
    cal = load_calib()

    perf_path, err = model_assets.select(PERCEPTION_MODEL_REL)
    if perf_path is None:
        raise RuntimeError(f"感知权重不可用: {err}")
    perc = StreetPerception(str(perf_path))
    print(f"感知就绪: {Path(perf_path).name}")

    lw = LaneObsWorker(cfg.lane_obs, off_max=cfg.validate.x_lane_abs_max)
    lw.start()
    if lw.error:
        raise RuntimeError(f"线观测未启动: {lw.error}")
    print(f"线观测就绪: provider={cfg.lane_obs.provider} "
          f"fp16={'有' if cfg.lane_obs.weights_fp16 else '无'}")

    dpath, derr = model_assets.select(DEPTH_MODEL_REL)
    if dpath is None:
        raise RuntimeError(f"深度权重不可用: {derr}")
    depth_sess = load_session(Path(dpath))
    dgeo = AsyncDepthRoadObserver(
        DepthRoadObserver(depth_sess, cal),
        infer_min_interval_s=DEPTH_INTERVAL_S)
    dgeo.start()
    print("深度观测就绪（v5 DepthART）")

    frames = [cv2.imread(str(p)) for p in sorted(FRAMES_DIR.glob("*.jpg"))]
    frames = [cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in frames if f is not None]
    if not frames:
        raise RuntimeError(f"帧源为空: {FRAMES_DIR}")
    print(f"帧源: {len(frames)} 张（{FRAMES_DIR.name}，循环喂）")

    rec = Recorder()
    base_fid = 10000
    n = 0
    t_end = time.perf_counter() + duration
    t_next = time.perf_counter() + BEAT_S
    prev_t_beat = None
    prev_pushed_fid = None
    warmup_beats = 20   # 前若干拍热身（DML kernel 编译、线程爬速），不记账
    try:
        while time.perf_counter() < t_end:
            rest = t_next - time.perf_counter()
            if rest > 0:
                time.sleep(rest)
            t_beat = time.perf_counter()
            n += 1
            record = n > warmup_beats
            frame = frames[n % len(frames)]
            fid = base_fid + n
            ts_ns = int(t_beat * 1e9)

            # ── 拍首提交（V1/V3）：锚/灭点/焦距=上一拍驻留值，与生产同源 ──
            submit_at_start = variant in ("v1", "v3")
            if submit_at_start:
                lw.push(frame, fid, None, 0.0, BEAT_S, False,
                        focal=cal.focal_px, frame_ts_ns=ts_ns, pre_note=True)
            pushed_fid = fid

            # ── 感知（真实 YOLO，内部持锁+放锁 note()）──
            t_det0 = time.perf_counter()
            result = perc.detect(frame, frame_id=fid, ts_ns=ts_ns)
            det_ms = (time.perf_counter() - t_det0) * 1000.0

            # ── 生产读取位置：对齐 trace 的读取点（帧龄 ~20ms）──
            rest = (t_beat + EARLY_READ_AT_S) - time.perf_counter()
            if rest > 0:
                time.sleep(rest)
            t_early = time.perf_counter()
            lane_early = lw.latest()

            # ── 深度提交（生产位置：读取后立刻）──
            dgeo.push(frame, object_mask=None, frame_ts_ns=ts_ns,
                      detections=None, fid=fid, y_v=None)

            # ── 深度消费：v4 挪到晚读点（对齐发布相位，治错相罚拍）；
            # 其余变体在拍前段 take（生产位置）──
            if variant != "v4":
                _r, _g, is_new = dgeo.take()
                if record:
                    rec.depth_read(dgeo.last_age_ms, is_new,
                                   dgeo.last_frame_ts_ns, time.perf_counter())

            if not submit_at_start:
                rest = (t_beat + LANE_PUSH_AT_S) - time.perf_counter()
                if rest > 0:
                    time.sleep(rest)
                lw.push(frame, fid, None, 0.0, BEAT_S, False,
                        focal=cal.focal_px, frame_ts_ns=ts_ns)

            # ── 拍内工作段占位（生产：追踪/决策/规划的 Python 负载）──
            lane_used = lane_early
            if variant in ("v2", "v3"):
                rest = (t_beat + LATE_READ_AT_S) - time.perf_counter()
                if rest > 0:
                    time.sleep(rest)
                t_late = time.perf_counter()
                lane_late = lw.latest()
                # 晚读源戳更新才切换消费对象（=晚读等到了本帧/更新结果）
                if (lane_late and lane_early
                        and (lane_late.get("det_ts_ns") or 0)
                        > (lane_early.get("det_ts_ns") or 0)):
                    lane_used = lane_late
                    if record:
                        rec.lane_read(lane_late, t_late, None)
                elif record:
                    rec.lane_read(lane_early, t_early, None)
            elif record:
                rec.lane_read(lane_early, t_early, None)

            # 发布龄：本拍或上一拍提交帧的快照自带段账（cap+queue+ms）
            if record and lane_used and lane_used.get("det_fid") in (
                    pushed_fid, prev_pushed_fid):
                c = lane_used.get("cap_ms")
                q = lane_used.get("queue_ms") or 0.0
                m = lane_used.get("ms")
                if c is not None and m is not None:
                    rec.lane_pub.append(c + q + m)
            prev_pushed_fid = pushed_fid

            if variant == "v4":
                _r, _g, is_new = dgeo.take()
                if record:
                    rec.depth_read(dgeo.last_age_ms, is_new,
                                   dgeo.last_frame_ts_ns, time.perf_counter())

            # 拍长（真实拍间隔）
            if record and prev_t_beat is not None:
                rec.beat_ms.append((t_beat - prev_t_beat) * 1000.0)
                rec.yolo_1.append(det_ms)
            prev_t_beat = t_beat
            t_next += BEAT_S
            if t_next < time.perf_counter():
                t_next = time.perf_counter()  # 不追帧，直接滑向下一拍
    finally:
        lw.stop()
        dgeo.stop()

    h = dgeo.health()
    print(f"线观测 worker: det_n={lw.det_n} push_n={lw.push_n} drop_n={lw.drop_n}"
          f" slot_skips={lw.slot_skips} busy_skips={lw.busy_skips}")
    print(f"深度 worker: pushed={h.get('pushed')} applied={h.get('applied')}"
          f" stale={h.get('stale_drops')} busy={h.get('busy_skips')}"
          f" fail={h.get('failures')}")
    return rec


def report(variant: str, rec: Recorder) -> None:
    print(f"\n── 变体 {variant} 结果 ──")
    print(f"拍长 ms（目标 50）: {fmt(rec.beat_ms)}")
    print(f"YOLO detect ms   : {fmt(rec.yolo_1)}")
    print(f"线发布龄 ms      : {fmt(rec.lane_pub)}")
    print(f"线 口径一(首次消费): {fmt(rec.lane_1)}  ≤50ms {ratio(rec.lane_1, 50)}")
    print(f"线 口径二(每拍使用): {fmt(rec.lane_2)}  ≤50ms {ratio(rec.lane_2, 50)}")
    print(f"深 口径一(首次消费): {fmt(rec.depth_1)}  ≤100ms {ratio(rec.depth_1, 100)}")
    print(f"深 口径二(每拍使用): {fmt(rec.depth_2)}  ≤100ms {ratio(rec.depth_2, 100)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=("v0", "v1", "v2", "v3", "v4"), default="v0")
    ap.add_argument("--duration", type=float, default=12.0)
    args = ap.parse_args()
    print(disable_power_throttling())
    rec = run_variant(args.variant, args.duration)
    report(args.variant, rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
