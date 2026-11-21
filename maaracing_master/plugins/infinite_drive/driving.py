# -*- coding: utf-8 -*-
"""无限驾驶的道路定位与车头角闭环。"""

from __future__ import annotations

import math
import time

from maaracing_master.core.logger import logger
from maaracing_master.core.template_match import load_template, match_template_cs
from maaracing_master.core.trace_sink import get_trace_sink
from maaracing_master.plugins.infinite_drive import IMAGE_DIR
from maaracing_master.plugins.infinite_drive.view_guard import ViewGuard, ViewGuardConfig

_DRIVE_HUD = (
    ("inf_drive_score_rate.png", (0.7539, 0.0333, 0.0672, 0.0458), 0.80),
    ("inf_drive_gear.png", (0.0344, 0.0333, 0.0281, 0.0528), 0.72),
)
_MAX_HEADING_AGE_S = 0.22
_MAX_VIEW_AGE_S = 0.8
_ANCHOR_PAIR_S = 0.8
_LANE_CENTERS = (-1.5, -0.5, 0.5, 1.5)


def drive_hud_visible(frame) -> bool:
    """优先看深色底的计分条，倒计时等计分条尚未出现时用齿轮。"""
    height, width = frame.shape[:2]
    for name, (x, y, w, h), threshold in _DRIVE_HUD:
        template = load_template(name, [IMAGE_DIR])
        if template is None:
            raise RuntimeError(f"驾驶页模板缺失：{name}")
        rx, ry = round(width * x), round(height * y)
        rw, rh = round(width * w), round(height * h)
        # 先裁 ROI，避免 20Hz 的小图识别反复对整帧做灰度转换。
        box, _score = match_template_cs(frame[ry:ry + rh, rx:rx + rw], template,
                                        colorspace="gray", threshold=threshold)
        if box is not None:
            return True
    return False


def road_anchor(reading) -> float | None:
    """两侧同时读到且宽度接近四车道时，返回以路心为原点的自车横位。"""
    if reading is None or reading.sides != 2 or reading.degraded:
        return None
    left, right = reading.left_edge_lane, reading.right_edge_lane
    if left is None or right is None or not left < 0.0 < right:
        return None
    if abs((right - left) - len(_LANE_CENTERS)) > 0.6:
        return None
    return -(left + right) / 2.0


def _lane_center(x: float, lines) -> float | None:
    """从已锁的左右相邻线选当前车道中心。"""
    left = math.floor(x)
    center = left + 0.5
    if center not in _LANE_CENTERS:
        return None
    boundaries = {(line.get("k"), line.get("side")) for line in lines if line.get("accepted")}
    return center if {(left, -1), (left + 1, 1)} <= boundaries else None


class HeadingControl:
    """横位误差给目标车头角，按实测车头角与角速度出杆。"""

    def __init__(self, lane_w_m: float) -> None:
        self._lane_w_m = lane_w_m
        self.reset()

    def reset(self) -> None:
        self._ts_ns = 0
        self._heading = 0.0
        self.yaw_rate = 0.0
        self.desired_heading = 0.0
        self._steer = 0.0

    def update(self, lateral_error_lane: float, heading: float, ts_ns: int) -> float:
        if ts_ns <= self._ts_ns:
            return self._steer
        dt = (ts_ns - self._ts_ns) / 1e9
        if self._ts_ns and dt <= _MAX_HEADING_AGE_S:
            rate = (heading - self._heading) / dt
            self.yaw_rate += dt / (0.16 + dt) * (rate - self.yaw_rate)
        else:
            self.yaw_rate = 0.0
        self._ts_ns, self._heading = ts_ns, heading

        # 空间预瞄生成温和的目标角，D 项只看测得的角速度，目标更新不踢杆。
        desired = math.atan2(lateral_error_lane * self._lane_w_m, 45.0)
        self.desired_heading = max(-0.10, min(0.10, desired))
        steer = 3.5 * (self.desired_heading - heading) - 0.18 * self.yaw_rate
        # 小指令归真零，留给游戏自己的回正。限幅避开卡牌触发的满杆换道。
        self._steer = 0.0 if abs(steer) < 0.10 else max(-0.45, min(0.45, steer))
        return self._steer


class DepthDrive:
    """深度提供定位锚，锁定后用新鲜的车道线横位和车头角保持本车道。"""

    def __init__(self) -> None:
        from maaracing_master.plugins.infinite_drive import (
            DEPTH_MODEL_REL, LANE_FP16_MODEL_REL, LANE_MODEL_REL, model_assets,
        )
        from maaracing_master.plugins.infinite_drive.depth_geo import (
            CAM_H_M, AsyncDepthRoadObserver, DepthRoadObserver, load_session,
        )
        from maaracing_master.plugins.infinite_drive.lane_obs import LaneObsConfig, LaneObsWorker
        from maaracing_master.plugins.infinite_drive.world_model import load_calib

        weights, error = model_assets.select(DEPTH_MODEL_REL)
        if weights is None:
            raise RuntimeError(error or "未找到深度权重")
        self._cal = load_calib()
        self._camera_height = CAM_H_M
        self._control = HeadingControl(self._cal.lane_w_m)
        self._depth = AsyncDepthRoadObserver(DepthRoadObserver(load_session(weights), self._cal))
        self._lines = LaneObsWorker(LaneObsConfig(
            enabled=True, provider="dml", weights=LANE_MODEL_REL, weights_fp16=LANE_FP16_MODEL_REL,
        ))
        # 开局先等深度路心和线道号收敛，避免把已在保险杠机位的冷启动当成错机位。
        self._view_config = ViewGuardConfig(enabled=True, empty_sample_s=2.0)
        self._trace = get_trace_sink("infinite_drive")
        self.begin()

    def start(self) -> None:
        self._lines.start()
        if self._lines.error:
            raise RuntimeError(self._lines.error)
        if self._lines.note:
            logger.log(f"[无限驾驶] {self._lines.note}", "WARNING")
        self._depth.start()

    def stop(self) -> None:
        self._lines.stop()
        if not self._depth.stop():
            logger.log("[无限驾驶] 深度观测线程停止超时", "WARNING")

    def begin(self, *, allow_view_switch: bool = True) -> None:
        self._view: ViewGuard | None = None
        self._allow_view_switch = allow_view_switch
        self._reset_geometry(time.perf_counter_ns())

    def _reset_geometry(self, start_ns: int) -> None:
        self._phase_start_ns = start_ns
        self._last_frame_ns = 0
        self._last_fid = -1
        self._view_fid = None
        self._anchor_sample: tuple[int, float] | None = None
        self._anchor: tuple[int, float] | None = None
        self._target: float | None = None
        self.release()

    def release(self) -> None:
        self._control.reset()

    def steer(self, frame, fid: int, ts_ns: int) -> tuple[float, bool]:
        """处理采集帧，返回方向杆和是否请求切换视角。"""
        from maaracing_master.core import dml_lock

        if ts_ns < self._phase_start_ns:
            return 0.0, False
        now_ns = time.perf_counter_ns()
        if self._view is None:
            self._view = ViewGuard(self._view_config, now_ns / 1e9,
                                   allow_switch=self._allow_view_switch)
        lane = self._lines.latest() or {}
        det_ns = lane.get("det_ts_ns") or 0
        fresh_view = (det_ns >= self._phase_start_ns
                      and 0 <= now_ns - det_ns <= _MAX_VIEW_AGE_S * 1e9)
        cam_h = lane.get("cam_h_lane") if fresh_view else None
        det_fid = lane.get("det_fid") if fresh_view else None
        new_view = det_fid is not None and det_fid != self._view_fid
        if new_view:
            self._view_fid = det_fid
        previous_view = self._view.state
        view = self._view.update(now_ns / 1e9, cam_h, new_view)
        if view != previous_view:
            if view == ViewGuard.SWITCH:
                height = self._view.median
                evidence = "尚无高度读数" if height is None else f"相机高中位 {height:.2f}m"
                logger.log(f"[无限驾驶] {evidence}，尝试从追尾切换保险杠视角", "DEBUG")
                self._reset_geometry(round(self._view.settle_until * 1e9))
            elif view == ViewGuard.OK:
                logger.log(f"[无限驾驶] 已确认保险杠视角（相机高中位 {self._view.median:.2f}m）",
                           "DEBUG")
            elif view == ViewGuard.FAIL:
                logger.log("[无限驾驶] 保险杠视角未确认，本阶段仅保持直行", "WARNING")
        if view == ViewGuard.SWITCH:
            return 0.0, True

        anchor = (self._anchor[1] if self._anchor is not None
                  and now_ns - self._anchor[0] <= _ANCHOR_PAIR_S * 1e9 else None)
        reading, _, new_depth = self._depth.take()
        if new_depth and self._target is None and anchor is None:
            depth_ts = self._depth.last_frame_ts_ns or 0
            pos = road_anchor(reading) if depth_ts >= self._phase_start_ns else None
            prev = self._anchor_sample
            self._anchor_sample = None if pos is None else (depth_ts, pos)
            if (pos is not None and prev is not None
                    and 0 < depth_ts - prev[0] <= _ANCHOR_PAIR_S * 1e9
                    and abs(pos - prev[1]) <= 0.3):
                self._anchor = (depth_ts, pos)
                anchor = pos

        new_frame = fid != self._last_fid
        if new_frame and view != ViewGuard.FAIL:
            gap = (ts_ns - self._last_frame_ns) / 1e9
            dt = gap / max(1, fid - self._last_fid) if self._last_frame_ns else 0.05
            y_v = None
            if (lane.get("det_ts_ns") or 0) >= self._phase_start_ns:
                y_v = lane.get("y_v")
            self._lines.push(frame, fid, self._camera_height, 0.0, dt,
                             not self._last_frame_ns or gap > _ANCHOR_PAIR_S,
                             focal=self._cal.focal_px, anchor=anchor,
                             frame_ts_ns=ts_ns, pre_note=True)
            self._last_fid, self._last_frame_ns = fid, ts_ns
            # 相机高度反解要先用深度路心给线定道号。已有锚时只给车道线确认定位。
            if self._target is None and anchor is None:
                self._depth.push(frame, frame_ts_ns=ts_ns, fid=fid, y_v=y_v)
            dml_lock.TICK_BEAT.note()

        x = lane.get("x")
        heading = lane.get("theta")
        heading_fid = lane.get("theta_src_fid")
        heading_ns = lane.get("theta_src_ts_ns") or 0
        camera_in_band = (cam_h is not None and self._view_config.cam_h_lo_m
                          <= cam_h <= self._view_config.cam_h_hi_m)
        position_measured = x is not None and math.isfinite(x) and not lane.get("hold")
        track_ready = (view == ViewGuard.OK and fresh_view and position_measured
                       and lane.get("usable") and (cam_h is None or camera_in_band))
        usable = (track_ready
                  and heading is not None and math.isfinite(heading)
                  and heading_fid == lane.get("det_fid")
                  and heading_ns >= self._phase_start_ns
                  and 0 <= now_ns - heading_ns <= _MAX_HEADING_AGE_S * 1e9)
        # 深度已为线定道号。稳定检测到当前车道的两边时，直接锁定这两条线的中心。
        if self._target is None and track_ready:
            self._target = _lane_center(x, lane.get("lines", ()))
            if self._target is not None:
                self._anchor = None
                logger.log(f"[无限驾驶] 已锁定当前车道（中心 {self._target:+.1f}）", "DEBUG")
        if usable and self._target is not None:
            steer = self._control.update(self._target - x, heading, heading_ns)
            target_heading, yaw_rate = self._control.desired_heading, self._control.yaw_rate
        else:
            self.release()
            steer = 0.0
            target_heading = yaw_rate = None

        self._trace.append({"fid": fid, "ts_ns": ts_ns, "anchor_lane": anchor,
                            "ego_lane": x, "target_lane": self._target, "heading_rad": heading,
                            "target_heading_rad": target_heading, "yaw_rate_rad_s": yaw_rate,
                            "lane_fid": det_fid, "lane_ms": lane.get("ms"),
                            "lane_state": lane.get("state"), "lane_locked": lane.get("n_locked"),
                            "heading_fid": heading_fid, "heading_ts_ns": heading_ns or None,
                            "usable": bool(usable), "cam_h": cam_h, "view": view, "steer": steer})
        return steer, False
