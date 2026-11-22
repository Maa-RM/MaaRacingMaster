# -*- coding: utf-8 -*-
"""infinite_drive 控制局 raw 调试图落盘器：10fps 节流原帧取证。

**职责与边界**：控制拍每 tick 喂一次快照（只入队引用，不复制不渲染）；IO
线程按 10fps 节流写盘。写盘失败静默计数——取证是旁路，绝不干扰控制链。
只落原帧与索引，不渲染叠加图：控制链的决策取证走 trace（``LaneDrive`` 逐拍
append），像素级核对以原帧为底图离线进行。

**目录契约**（与录制语料同一套，见 ``session_layout``）：
  `raw/<seq:04d>_raw.jpg`   原始帧全幅（q95，人工金标标注/离线重渲染的底图）
  `frames.jsonl`            帧索引：seq ↔ 采集帧号 ↔ 帧时戳 ↔ 文件（hud=null）
  `meta.json`               会话元信息（与录制局同构）
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.plugins.infinite_drive.session_layout import (
    SOURCE_CONTROL,
    build_meta,
    current_session_token,
    raw_relpath,
)

# 10Hz 取证（2026-10-24 从逐拍下调）：控制拍 ≈20Hz，调试图按 0.1s 节流，
# 与 trace 的 20Hz 同钟但不同密度——人工对照局看得清又不淹没磁盘。
# 磁盘代价 ≈ 10 帧/秒 × ~0.2MB ≈ 2MB/s；IO 线程跟不上时
# 由有界队列丢旧保新，主循环只做 put_nowait，绝不阻塞。
DEBUG_FPS = 10
DEBUG_INTERVAL_S = 1.0 / DEBUG_FPS
_QUEUE_MAX = 20                   # 约 2 秒 @10Hz；满则丢旧快照，保最新
_RAW_JPEG_QUALITY = 95
_HUD_JPEG_QUALITY = 85            # meta 契约字段；本落盘器不产 hud 图，恒为 0 张


class DebugPairWriter:
    """raw 落盘器：tick() 入队本拍快照，IO 线程逐拍写盘（10Hz）。

    生命周期 = 控制链：链建即起 daemon，链收 stop()（排空队列后退出）。
    队列有界（满丢旧保新）；写盘异常静默计数——观测件不碰主路。
    tick() 里的 frame 是 WGC 当前缓存数组（帧号变才重绑新数组，旧数组不被
    原地改写）；snap 内字段为本拍新建对象，入队后主循环不再改它们。

    产物布局与人工录制会话同一套（``session_layout``）：``raw/`` +
    ``frames.jsonl`` + ``meta.json``。人工局由录制器自己写这套，本 writer
    只在控制链里建，两种局不会同时写一个目录。"""

    def __init__(self, session_dir: Path,
                 interval_s: float | None = None,
                 *, phase: int | None = None,
                 round_no: int | None = None):
        self._dir = Path(session_dir)
        self._raw_dir = self._dir / "raw"
        self._interval = DEBUG_INTERVAL_S if interval_s is None else interval_s
        self._last_push = float("-inf")     # 首拍立出图
        self._seq = 0
        self.saved = 0
        self.dropped = 0
        self.errors = 0
        self.hud_written = 0
        self.last_ms = 0.0
        self._phase = phase
        self._round_no = round_no
        self._frame_size: tuple[int, int] | None = None
        self._started_ns = time.perf_counter_ns()
        self._started_iso = datetime.now().isoformat(timespec="seconds")
        # 帧索引：raw 的 seq 与采集帧号、帧时戳一一对应。此前实跑会话只有
        # 图没有索引，队列满丢旧保新会让「第 i 张图 ↔ 第 i 拍」不成立——离线
        # 把像素结论接到控制状态上时只能作废（2026-10-27 当日 P1 核查踩过）。
        # 字段与人工录制器同一套（``session_layout``），不另起编号。
        self._idx_path = self._dir / "frames.jsonl"
        self._idx_f = None
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="infinite_drive-debug-io")
        self._thread.start()

    def stats(self) -> dict:
        """IO 侧观测：写盘数/丢旧数/异常数/队列深度/单帧耗时（ms）。"""
        return {"saved": self.saved, "dropped": self.dropped,
                "errors": self.errors, "q": self._q.qsize(),
                "ms": round(self.last_ms, 1)}

    @property
    def dir(self) -> Path:
        """本会话目录（调用方收尾重建行为索引用，不暴露内部状态）。"""
        return self._dir

    def tick(self, frame_rgb: np.ndarray | None, snap: dict) -> None:
        """控制拍调用：按节流入队（默认 10fps）。绝不抛。"""
        if frame_rgb is None:
            return
        now = time.monotonic()
        if now - self._last_push < self._interval:
            return
        self._last_push = now
        try:
            self._q.put_nowait((frame_rgb, dict(snap)))
        except queue.Full:
            # 丢旧保新：取证更看重最新一拍（旧快照的 fid 已在 trace 里）
            try:
                self._q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait((frame_rgb, dict(snap)))
            except queue.Full:
                self.dropped += 1

    def stop(self, reason: str = "chain_end") -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self._idx_f is not None:
            try:
                self._idx_f.close()
            except OSError:
                pass
            self._idx_f = None
        self._write_meta(reason)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame_rgb, snap = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(frame_rgb, snap)
            except Exception:  # noqa: BLE001 —— 取证失败不碰主路
                self.errors += 1

    def _process(self, frame_rgb: np.ndarray, snap: dict) -> None:
        t0 = time.perf_counter()
        self._seq += 1
        self._dir.mkdir(parents=True, exist_ok=True)
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        h, w = frame_rgb.shape[:2]
        if self._frame_size is None:
            self._frame_size = (int(w), int(h))
        raw_name = raw_relpath(self._seq)
        ok_raw = cv2.imwrite(
            str(self._dir / raw_name),
            cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, _RAW_JPEG_QUALITY])
        if not ok_raw:
            self.errors += 1       # 索引不指向不存在的文件
            return
        self._write_index(snap)
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        self.saved += 1

    def _write_index(self, snap: dict) -> None:
        """帧索引一行：seq ↔ 采集帧号 ↔ 帧时戳 ↔ 文件（与人工局同一套字段）。

        缺 fid 或时戳就写 null，不拿 seq 反推、不用当前时刻冒充采集时刻——
        这条索引的全部价值就是"图与拍能一一对上"，伪造一个就等于没有。
        叠加图恒缺席，索引以 hud=null 表达，不整行不留。
        写失败只计数（观测件不碰主路）。"""
        row = {"seq": self._seq, "frame_id": snap.get("fid"),
               "ts_ns": snap.get("ts_ns"), "age_ms": snap.get("age_ms"),
               "file": raw_relpath(self._seq), "hud": None}
        try:
            if self._idx_f is None:
                self._idx_f = open(self._idx_path, "a", encoding="utf-8")
            self._idx_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._idx_f.flush()
        except OSError:
            self.errors += 1

    def _write_meta(self, reason: str) -> None:
        """会话元信息（与人工局同构）：控制局没有手柄项，显式 None。"""
        w, h = self._frame_size or (0, 0)
        meta = build_meta(
            source=SOURCE_CONTROL,
            started_at=self._started_iso,
            ended_at=datetime.now().isoformat(timespec="seconds"),
            monotonic_start_ns=self._started_ns,
            phase=self._phase,
            round_no=self._round_no,
            frame_w=w,
            frame_h=h,
            jpeg_quality=_RAW_JPEG_QUALITY,
            hud_jpeg_quality=_HUD_JPEG_QUALITY,
            frames_written=self.saved,
            frames_dropped=self.dropped,
            hud_written=self.hud_written,
            write_errors=self.errors,
            pad_slot=None,
            pad_poll_hz=None,
            pad_samples=None,
            stop_reason=reason,
            session=current_session_token(),
        )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.errors += 1
