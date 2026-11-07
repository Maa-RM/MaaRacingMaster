#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DirectML 推理互斥：同进程内任意两个 DML ``session.run`` 不得同时在飞。

本机实证（2026-09-25，onnxruntime-directml 1.24.4 / NVIDIA 驱动 32.0.15.9649）：
感知 model.onnx 与深度权重（时为 depth_small_q4f16.onnx，2026-10-01 换装
moge2 q4f16，锁协议不变）各建一个 DML 会话——
- 两线程并发 run：数秒内段错误杀进程（0xc0000005 @ nvwgf2umx.dll，NVIDIA 用户态
  驱动；2026-09-25 21:00 实机对局同型崩溃，进程带虚拟手柄一起消失）；
- 同两会话单线程交替 run：1874 对 / 60s 干净；
- 单会话独跑：深度 400 次、感知 2545 次均干净。
判据因此是「并发」而非「多会话」：跨线程使用 DML 会话的一方必须让锁，抢不到就
放弃本次（深度帧是 latest-only 慢变量，弃一帧无损）；控制拍侧（感知）阻塞持有
——感知不能跳拍。

持锁范围只圈 ``session.run`` 本体：前后处理不碰 GPU，留在锁外。

**错峰节拍**（``TICK_BEAT``，2026-11-03）：一次深度前向 54ms 是不可分割的原子，
比控制拍 50ms 还长——无论怎么排都会伸进下一拍。自由起跑时伸出量在 1~54ms 之间
随机（实测 37~42% 的拍要等锁，其中一大档等满 40~54ms）；从感知刚放锁的瞬间
起跑，伸出量压到几何下限 ~19ms。所以感知每次放锁记一拍，深度 worker 只在
一拍之后开工（见 speedrush.depth_geo 的 ``_stagger_wait``）。
"""

from __future__ import annotations

import threading
import time
from typing import NamedTuple

LOCK = threading.Lock()


class Busy(RuntimeError):
    """DML 已被其他线程占用；调用方应放弃本次推理（让锁），不得等待。"""


class InFlight(NamedTuple):
    """一次在飞 DML 调用的取证身份（停顿哨据此取线程 CPU 时间）。"""

    since: float                # 登记起点（perf_counter）
    who: str                    # yolo / moge / lane
    native_id: int              # 原生 TID：GetThreadTimes 只认它，不认 get_ident()
    run_since: float | None     # 拿到锁的时刻；None = 仍在等锁


class _RunWatch:
    """在飞 DML 调用记账：按线程登记，停顿哨（GpuProbe）取最久的一条。

    覆盖范围 = 等锁 + run 本体——两者膨胀都是停顿现场的一部分，但取证时要分得开
    （``run_since``）：等锁方卡住与持锁方卡住是两个不同的根因方向。

    必须按线程记账而非单槽：感知阻塞等锁时深度/车道仍在飞是生产常态，单槽下
    后来者覆盖前者、先结束者把仍在飞的那条一并清掉，停顿哨恰好在争抢最凶的拍
    上失明。
    """

    def __init__(self) -> None:
        self._lk = threading.Lock()
        self._inflight: dict[int, InFlight] = {}

    def enter(self, who: str, *, waiting: bool = False) -> None:
        """登记本次调用；``waiting=True`` 表示登记时还没拿到锁（随后调 acquired）。"""
        now = time.perf_counter()
        with self._lk:
            self._inflight[threading.get_ident()] = InFlight(
                now, who, threading.get_native_id(), None if waiting else now)

    def acquired(self) -> None:
        """等锁方拿到锁后打点，把「等锁」与「原生推理在飞」分开。"""
        with self._lk:
            key = threading.get_ident()
            entry = self._inflight.get(key)
            if entry is not None:
                self._inflight[key] = entry._replace(run_since=time.perf_counter())

    def exit(self) -> None:
        with self._lk:
            self._inflight.pop(threading.get_ident(), None)

    def _oldest(self) -> InFlight | None:
        if not self._inflight:
            return None
        return min(self._inflight.values(), key=lambda entry: entry.since)

    def snapshot(self) -> tuple[float | None, str]:
        """最久在飞的 (perf_counter 起点, 调用方)；无在飞时 (None, "")。"""
        with self._lk:
            entry = self._oldest()
            return (None, "") if entry is None else (entry.since, entry.who)

    def detail(self) -> InFlight | None:
        """最久在飞那条的完整取证身份；无在飞时 None。"""
        with self._lk:
            return self._oldest()


RUN_WATCH = _RunWatch()


class _TickBeat:
    """控制拍「跑完感知、放下锁」的节拍：序号 + 时刻 + 等待者唤醒。"""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._seq = 0
        self._t = float("-inf")

    def note(self) -> None:
        with self._cv:
            self._seq += 1
            self._t = time.perf_counter()
            self._cv.notify_all()

    def snapshot(self) -> tuple[int, float]:
        """(序号, 最近一次节拍时刻)；从未有过节拍时时刻为 -inf。"""
        with self._cv:
            return self._seq, self._t

    def wait_newer(self, seq: int, timeout: float) -> bool:
        """等下一次节拍（序号变化）；超时返回 False。"""
        with self._cv:
            return self._cv.wait_for(lambda: self._seq != seq, timeout=timeout)


TICK_BEAT = _TickBeat()
