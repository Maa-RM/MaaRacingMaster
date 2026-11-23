# -*- coding: utf-8 -*-
"""infinite_drive 控制局调试图落盘器：raw 原帧 + 单图状态叠加，10fps 节流。

**职责与边界**：控制拍每 tick 喂一次快照（只入队引用，不复制不渲染）；IO
线程按 10fps 节流写盘。写盘/渲染失败静默计数——取证是旁路，绝不干扰控制链。
控制链的决策取证仍走 trace（``LaneDrive`` 逐拍 append）；本模块的叠加图是它的
像素对照面，不是第二份决策记录。

**目录契约**（与录制语料同一套，见 ``session_layout``）：
  `raw/<seq:04d>_raw.jpg`   原始帧全幅（q95，人工金标标注/离线重渲染的底图）
  `<seq:04d>_hud.jpg`       单图状态叠加（q85）：
    画面层 = 车道线拟合（实段/外推虚线/被拒灰线，按近中远分色）+ 灭点行
             + 同拍街车的原始框（车账过期时整组转灰）
    底部带 = 道路横位尺（道坐标）：自车横位、当前维持点、六个候选点
             （绿=净空/红=被堵/缺席=无车账）、街车横位与最近车距
    顶部带 = 当拍读数：观测状态与锁线数、检测/角度龄、车头角与目标角、
             角速度、输出杆量、油门比例；不可用或过期整行转警示色
  `frames.jsonl`            帧索引：seq ↔ 采集帧号 ↔ 帧时戳 ↔ 文件（hud=叠加图或 null）
  `meta.json`               会话元信息（与录制局同构）

**为什么不搬旧版双联雷达**：本系统没有可行驶栅格、深度平面与轨迹候选，右联
面板会全是空槽；六点维持与车账在「道坐标」里是一维的，用底部横位尺表达比
硬投影到画面更诚实、也更容易逐帧对账。

**叠加图为什么在落盘线程渲染**：控制拍只做 put_nowait；渲染走本模块 IO 线程，
队列满丢旧保新。写失败只计数，索引里的 hud 用 null 表达缺席，绝不指向不存在
的文件。
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from maaracing_master.core.debug import _put_text
from maaracing_master.plugins.infinite_drive.session_layout import (
    SOURCE_CONTROL,
    build_meta,
    current_session_token,
    hud_relpath,
    raw_relpath,
)

# 10Hz 取证（2026-10-24 从逐拍下调）：控制拍 ≈20Hz，调试图按 0.1s 节流，
# 与 trace 的 20Hz 同钟但不同密度——人工对照局看得清又不淹没磁盘。
# 磁盘代价 ≈ 10 对/秒 × (raw ~0.2MB + hud ~0.3MB) ≈ 5MB/s；IO 线程跟不上时
# 由有界队列丢旧保新，主循环只做 put_nowait，绝不阻塞。
DEBUG_FPS = 10
DEBUG_INTERVAL_S = 1.0 / DEBUG_FPS
_QUEUE_MAX = 20                   # 约 2 秒 @10Hz；满则丢旧快照，保最新
_RAW_JPEG_QUALITY = 95
_HUD_JPEG_QUALITY = 85

# 线与框的配色（BGR）：近中远三段按距离分色（与 ranges 三档同语义），
# 被拒线灰度保留——"程序看见了但没用"必须可审计。
_LANE_BAND_BGR = {"near": (90, 220, 90), "mid": (70, 190, 255),
                  "far": (255, 160, 70)}
_LANE_REJ_BGR = (120, 120, 120)
_TEXT_BGR = (235, 235, 235)
_WARN_BGR = (60, 190, 255)
_EGO_BGR = (90, 255, 120)
_TARGET_BGR = (255, 230, 60)
_CAR_BGR = (0, 150, 255)
_CAR_STALE_BGR = (120, 120, 120)
_POINT_CLEAR_BGR = (80, 200, 80)
_POINT_BLOCKED_BGR = (60, 60, 220)
_BAND_BGR = (18, 18, 18)

# 车道线几何按 1280×720 设计域给出；绘制时按实际帧尺寸等比缩放。
_DESIGN_W, _DESIGN_H = 1280.0, 720.0


def _num(value) -> float | None:
    """只认有限实数（bool 不算），其余一律 None——缺测不补假值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _lane_label_pos(end_x: float, end_y: float, width: int, height: int,
                    *, stagger: bool = False) -> tuple[int, int] | None:
    """线标签落点；端点跑出画面（主要是远端外推）时返回 None。

    不把出界端点夹到边缘——夹取会把标签贴到一条无关的线旁，错位比缺席更
    糟。标签始终留在底部横位尺之上，相邻线按奇偶错开一行降低叠字。"""
    if not (4.0 <= end_x <= width - 96):
        return None
    y = int(_clamp(end_y, 14, height - 96))
    if stagger:
        y = max(14, y - 16)
    return int(end_x), y


def _edge_caption(states, reason) -> str | None:
    """点位缺席说明：车道原点不可用 ≠ 同拍车账没跟上，两种情况分开写。"""
    if isinstance(states, list):
        return None
    return ("points: lane not usable" if reason == "lane"
            else "points: no car evidence")


def _draw_lane_lines(img: np.ndarray, lane: dict) -> None:
    """画面层：逐条画已锁定的候选线。

    每条线的拟合段按 ``fits`` 逐段绘制——实线=有像素支撑的观测段，虚线=向
    两端外推段；被拒线用灰度细线保留。段颜色跟着近/中/远分档走，和
    ``ranges`` 的米制分段同一语义，不用另读文字。"""
    lines = lane.get("lines")
    if not isinstance(lines, list):
        return
    H, W = img.shape[:2]
    sx, sy = W / _DESIGN_W, H / _DESIGN_H
    for idx, ln in enumerate(lines):
        if not isinstance(ln, dict):
            continue
        accepted = bool(ln.get("accepted", True))
        end: tuple[float, float] | None = None
        for ft in ln.get("fits") or ():
            if not isinstance(ft, dict):
                continue
            s, a, y_a, y_b = (_num(ft.get("s")), _num(ft.get("a")),
                              _num(ft.get("y0")), _num(ft.get("y1")))
            if None in (s, a, y_a, y_b) or y_b <= y_a:
                continue
            band = ft.get("band")
            col = (_LANE_BAND_BGR.get(band, (170, 170, 170))
                   if accepted else _LANE_REJ_BGR)
            thick = 2 if accepted else 1
            lo, hi = _clamp(y_a, 0.0, _DESIGN_H), _clamp(y_b, 0.0, _DESIGN_H)
            if hi > lo:
                cv2.line(img, (int((s * lo + a) * sx), int(lo * sy)),
                         (int((s * hi + a) * sx), int(hi * sy)), col, thick)
            # 外推段：虚线，明确区分"看到的"与"推出来的"
            for e0, e1 in ((_num(ft.get("eval_y0")), y_a),
                           (y_b, _num(ft.get("eval_y1")))):
                if e0 is None or e1 is None:
                    continue
                e0 = _clamp(e0, 0.0, _DESIGN_H)
                e1 = _clamp(e1, 0.0, _DESIGN_H)
                for row in np.arange(min(e0, e1), max(e0, e1), 6.0):
                    x0 = (s * row + a) * sx
                    row2 = min(row + 3.0, max(e0, e1))
                    x1 = (s * row2 + a) * sx
                    cv2.line(img, (int(x0), int(row * sy)),
                             (int(x1), int(row2 * sy)), col, 1)
            if end is None or y_b > end[1]:
                end = (s * y_b + a, y_b)
        if end is None:
            continue
        pos = _lane_label_pos(end[0] * sx, end[1] * sy, W, H,
                              stagger=bool(idx % 2))
        if pos is None:
            continue
        x, y = pos
        col = _TEXT_BGR if accepted else _LANE_REJ_BGR
        k = ln.get("k")
        label = f"k{k:+d}" if isinstance(k, int) and not isinstance(k, bool) else "k?"
        resid = _num(ln.get("resid"))
        if resid is not None:
            label += f" r{resid:+.2f}"
        if not accepted and ln.get("reject"):
            label += f" {ln['reject']}"
        _put_text(img, label, (x, y), scale=0.36, color=col)


def _draw_horizon(img: np.ndarray, lane: dict) -> None:
    """灭点行（地平线）：车道线与街车距离共用的米制骨架。"""
    y_v = _num(lane.get("y_v"))
    if y_v is None:
        return
    H, W = img.shape[:2]
    y = int(_clamp(y_v * H / _DESIGN_H, 1, H - 2))
    for x in range(0, W, 16):
        cv2.line(img, (x, y), (min(x + 8, W), y), (150, 150, 150), 1)
    _put_text(img, f"vanish {y_v:.0f}", (W - 96, max(12, y - 4)),
              scale=0.34, color=(170, 170, 170))


def _draw_cars(img: np.ndarray, lane: dict) -> None:
    """画面层：同拍街车原始框；车账与检测拍不同源时整组转灰并标 stale。"""
    traffic = lane.get("traffic")
    if traffic is None:
        return
    stale = (getattr(traffic, "frame_id", None) != lane.get("det_fid")
             or getattr(traffic, "ts_ns", None) != lane.get("det_ts_ns"))
    col = _CAR_STALE_BGR if stale else _CAR_BGR
    for d in getattr(traffic, "cars", ()) or ():
        cx, cy, w, h = (getattr(d, "cx", None), getattr(d, "cy", None),
                        getattr(d, "w", None), getattr(d, "h", None))
        if None in (cx, cy, w, h):
            continue
        x1, y1 = int(cx - w / 2), int(cy - h / 2)
        cv2.rectangle(img, (x1, y1), (x1 + int(w), y1 + int(h)), col, 2)
        conf = _num(getattr(d, "conf", None))
        tag = f"{conf:.2f}" if conf is not None else "car"
        if stale:
            tag += " stale"
        _put_text(img, tag, (max(2, x1), max(12, y1 - 4)),
                  scale=0.36, color=col)


def _draw_lane_strip(img: np.ndarray, drive: dict) -> None:
    """底部横位尺：把一维道坐标的决策画成可逐帧对账的标尺。

    自车横位、维持点、六个候选点的净/堵、街车横位与最近车距全在同一行
    坐标系里；无车账时点位状态显式缺席（不画绿点冒充净空）。"""
    H, W = img.shape[:2]
    band_h = max(78, int(H * 0.11))
    y0 = H - band_h
    overlay = img.copy()
    cv2.rectangle(overlay, (0, y0), (W, H), _BAND_BGR, -1)
    cv2.addWeighted(overlay, 0.66, img, 0.34, 0, img)
    cv2.line(img, (0, y0), (W, y0), (80, 80, 80), 1)

    half = float(_num(drive.get("half_lanes")) or 2.0) + 0.5
    xl, xr = 56, W - 28

    def px(v: float) -> int:
        return int(_clamp(xl + (v + half) / (2 * half) * (xr - xl), xl, xr))

    # 整数道刻度 + 底部道号：半道级位置一眼可读
    for i in range(-int(half), int(half) + 1):
        x = px(float(i))
        cv2.line(img, (x, y0 + 6), (x, H - 12), (92, 92, 92), 1)
        _put_text(img, f"{i:+d}" if i else "0", (x - 6, H - 4),
                  scale=0.32, color=(160, 160, 160))

    ego = _num(drive.get("ego_x"))
    if ego is not None and abs(ego) <= half:
        x = px(ego)
        cv2.drawMarker(img, (x, y0 + 10), _EGO_BGR,
                       cv2.MARKER_TRIANGLE_DOWN, 9, 2)
        _put_text(img, f"ego {ego:+.2f}", (min(x + 4, W - 74), y0 + 14),
                  scale=0.36, color=_EGO_BGR)

    target = _num(drive.get("target"))
    if target is not None and abs(target) <= half:
        x = px(target)
        cv2.line(img, (x, y0 + 4), (x, H - 14), _TARGET_BGR, 1)
        _put_text(img, f"T {target:+.2f}", (min(x + 4, W - 74), y0 + 30),
                  scale=0.36, color=_TARGET_BGR)

    states = drive.get("edge_states")
    if isinstance(states, list):
        for point, blocked in states:
            p = _num(point)
            if p is None or abs(p) > half:
                continue
            col = _POINT_BLOCKED_BGR if blocked else _POINT_CLEAR_BGR
            cv2.circle(img, (px(p), y0 + 42), 4, col, -1)
    else:
        caption = _edge_caption(states, drive.get("edge_reason"))
        if caption:
            _put_text(img, caption, (W - 205, y0 + 30),
                      scale=0.34, color=(150, 150, 150))

    cars = []
    for item in drive.get("cars") or ():
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        cx, z = _num(item[0]), _num(item[1])
        if cx is not None and z is not None:
            cars.append((cx, z))
    cars.sort(key=lambda c: c[1])
    horizon = _num(drive.get("horizon_m")) or 40.0
    for i, (cx, z) in enumerate(cars[:6]):
        if abs(cx) > half:
            continue
        x = px(cx)
        col = _POINT_BLOCKED_BGR if z <= horizon else _CAR_STALE_BGR
        cv2.line(img, (x, y0 + 52), (x, y0 + 62), col, 2)
        if i < 3:
            _put_text(img, f"{z:.0f}m", (min(max(x + 3, 2), W - 38),
                                          y0 + 56), scale=0.32, color=col)

    throttle = _num(drive.get("throttle_scale"))
    if throttle is not None and throttle <= 0.0:
        _put_text(img, "THROTTLE CUT", (6, y0 + 14), scale=0.38,
                  color=_POINT_BLOCKED_BGR)


def _draw_readout(img: np.ndarray, snap: dict) -> None:
    """顶部读数带：当拍观测与决策的关键数值，缺测一律画 "-"。"""
    drive = snap.get("drive")
    lane = drive.get("lane") if isinstance(drive, dict) else None
    lane = lane if isinstance(lane, dict) else {}
    fid = snap.get("fid")
    H, W = img.shape[:2]

    def fmt(v, spec="%+.2f") -> str:
        return "-" if v is None else (spec % v)

    if not isinstance(drive, dict):
        lines = [f"#{fid}  drive: no decision snapshot"]
        color = (160, 160, 160)
    else:
        usable = bool(drive.get("usable"))
        flags = "".join((" HOLD" if lane.get("hold") else "",
                         " PRED" if lane.get("pred") else ""))
        lines = [
            f"#{fid} {lane.get('state', '-')} locked={fmt(_num(lane.get('n_locked')), '%d')}"
            f" det={fmt(_num(lane.get('det_age_ms')), '%.0f')}ms"
            f" thA={fmt(_num(lane.get('theta_age_ms')), '%.0f')}ms"
            f"{'' if usable else ' STALE'}{flags}",
            f"ego={fmt(_num(drive.get('ego_x')))}"
            f" T={fmt(_num(drive.get('target')))}"
            f" psi={fmt(_num(drive.get('heading')), '%+.3f')}"
            f" psi*={fmt(_num(drive.get('target_heading')), '%+.3f')}"
            f" yaw={fmt(_num(drive.get('yaw_rate')), '%+.3f')}"
            f" steer={fmt(_num(drive.get('steer')), '%+.2f')}"
            f" thr={fmt(_num(drive.get('throttle_scale')), '%.2f')}",
        ]
        color = _TEXT_BGR if usable else _WARN_BGR

    band_h = 12 + 19 * len(lines)
    overlay = img.copy()
    cv2.rectangle(overlay, (0, 0), (W, band_h), _BAND_BGR, -1)
    cv2.addWeighted(overlay, 0.62, img, 0.38, 0, img)
    scale = 0.42 if W >= 1000 else 0.34
    for i, text in enumerate(lines):
        _put_text(img, text, (8, 20 + i * 19), scale=scale, color=color)


def render_debug(frame_rgb: np.ndarray, snap: dict) -> np.ndarray:
    """单图状态叠加（纯函数，None 全安全；异常由调用方计数）。

    ``snap["drive"]`` 缺席（不是驾驶页、或本帧没跑到决策）时只画原帧与
    「无决策快照」读数带——缺席本身是可读信号，不拿上一拍数据冒充本拍。
    """
    img = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    drive = snap.get("drive")
    if isinstance(drive, dict):
        lane = drive.get("lane")
        if isinstance(lane, dict):
            _draw_lane_lines(img, lane)
            _draw_horizon(img, lane)
            _draw_cars(img, lane)
        _draw_lane_strip(img, drive)
    _draw_readout(img, snap)
    return img


class DebugPairWriter:
    """raw + 叠加图落盘器：tick() 入队本拍快照，IO 线程逐拍渲染写盘（10Hz）。

    生命周期 = 控制链：链建即起 daemon，链收 stop()（排空队列后退出）。
    队列有界（满丢旧保新）；渲染/写盘异常静默计数——观测件不碰主路。
    tick() 里的 frame 是 WGC 当前缓存数组（帧号变才重绑新数组，旧数组不被
    原地改写）；snap 内的驾驶快照只引用本拍新建对象（lane 快照、车账、
    决策值），入队后主循环不再改它们，IO 线程延迟渲染拿到的仍是本拍内容。

    产物布局与人工录制会话同一套（``session_layout``）：``raw/`` + 叠加图 +
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
        # 帧索引：raw/hud 的 seq 与采集帧号、帧时戳一一对应。此前实跑会话只有
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
        pair = (frame_rgb, dict(snap))
        try:
            self._q.put_nowait(pair)
        except queue.Full:
            # 丢旧保新：取证更看重最新一拍（旧快照的 fid 已在 trace 里）
            try:
                self._q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(pair)
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
        # 渲染失败只让"叠加图缺席"，原帧照常进索引（hud=null）——与录制局
        # 同一条纪律：索引里的文件一定存在，缺席用 null 表达，不整行不留。
        hud_name = None
        try:
            hud = render_debug(frame_rgb, snap)
            name = hud_relpath(self._seq)
            if cv2.imwrite(str(self._dir / name), hud,
                           [cv2.IMWRITE_JPEG_QUALITY, _HUD_JPEG_QUALITY]):
                self.hud_written += 1
                hud_name = name
            else:
                self.errors += 1
        except Exception:  # noqa: BLE001 —— 可视化失败不碰主路
            self.errors += 1
        self._write_index(snap, hud_name)
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        self.saved += 1

    def _write_index(self, snap: dict, hud_name: str | None) -> None:
        """帧索引一行：seq ↔ 采集帧号 ↔ 帧时戳 ↔ 文件（与人工局同一套字段）。

        缺 fid 或时戳就写 null，不拿 seq 反推、不用当前时刻冒充采集时刻——
        这条索引的全部价值就是"图与拍能一一对上"，伪造一个就等于没有。
        叠加图写失败时 hud=null，文件名只在真正落盘时才写。
        写失败只计数（观测件不碰主路）。"""
        row = {"seq": self._seq, "frame_id": snap.get("fid"),
               "ts_ns": snap.get("ts_ns"), "age_ms": snap.get("age_ms"),
               "file": raw_relpath(self._seq), "hud": hud_name}
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
