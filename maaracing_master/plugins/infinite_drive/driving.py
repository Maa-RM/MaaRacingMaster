# -*- coding: utf-8 -*-
"""无限驾驶的道路定位、贴边超车与车头角闭环。"""

from __future__ import annotations

import math
import time

from maaracing_master.core.logger import logger
from maaracing_master.core.template_match import load_template, match_template_cs
from maaracing_master.core.trace_sink import get_trace_sink
from maaracing_master.plugins.infinite_drive import IMAGE_DIR
from maaracing_master.plugins.infinite_drive.lane_obs import lane_membership, lane_pos_nearest
from maaracing_master.plugins.infinite_drive.ranges import FAR_MAX_M
from maaracing_master.plugins.infinite_drive.road_path import LOOKAHEAD_M, RoadPath, plan_road_path
from maaracing_master.plugins.infinite_drive.world_model import BUMPER_CAM_H_M, GROUND_FOCAL_PX, load_calib
from maaracing_master.plugins.infinite_drive.yellow_line import ROAD_HALF_LANES

_DRIVE_HUD = (
    ("inf_drive_score_rate.png", (0.7539, 0.0333, 0.0672, 0.0458), 0.80),
    ("inf_drive_gear.png", (0.0344, 0.0333, 0.0281, 0.0528), 0.72),
)
_MAX_HEADING_AGE_S = 0.22
_FRAME_GAP_S = 0.8
_LANE_CENTERS = tuple(k + 0.5 for k in range(-ROAD_HALF_LANES, ROAD_HALF_LANES))
_PATH_AGE_NS = 350_000_000


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


def _lane_center(x: float, lines) -> float | None:
    """从已锁的左右相邻线选当前车道中心。"""
    left = math.floor(x)
    center = left + 0.5
    if center not in _LANE_CENTERS:
        return None
    boundaries = {(line.get("k"), line.get("side")) for line in lines if line.get("accepted")}
    return center if {(left, -1), (left + 1, 1)} <= boundaries else None


def _road_cars(lane: dict, ego_x: float, camera_height: float) -> list[tuple[float, float]] | None:
    """同拍框底映射到路面横位与前向距离，剔除路缘外停车。"""
    traffic = lane.get("traffic")
    horizon = lane.get("y_v")
    if (traffic is None or horizon is None or traffic.frame_id != lane.get("det_fid")
            or traffic.ts_ns != lane.get("det_ts_ns")):
        return None
    lines = lane["lines"]
    height = lane.get("cam_h") or camera_height
    cars = []
    for box in traffic.cars:
        bottom = box.cy + box.h / 2
        if bottom - horizon < 12:
            continue
        distance = GROUND_FOCAL_PX * height / (bottom - horizon)
        if distance > FAR_MAX_M:
            continue
        relative = lane_membership(box.cx, bottom, lines)
        if relative is None:
            relative = lane_pos_nearest(box.cx, bottom, lines, cam_h=height, y_v=horizon)
        if relative is None:
            return None
        if abs(ego_x + relative) < ROAD_HALF_LANES:
            cars.append((ego_x + relative, distance))
    return cars


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

    def update(self, lateral_error_lane: float, heading: float, ts_ns: int, *,
               preview_m: float | None = None) -> float:
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
        lookahead = 45.0 if preview_m is None else preview_m
        desired = math.atan2(lateral_error_lane * self._lane_w_m, lookahead)
        self.desired_heading = max(-0.10, min(0.10, desired))
        steer = 3.5 * (self.desired_heading - heading) - 0.18 * self.yaw_rate
        # 小指令归真零，留给游戏自己的回正。限幅避开卡牌触发的满杆换道。
        deadzone = 0.10 if preview_m is None else 0.05
        self._steer = 0.0 if abs(steer) < deadzone else max(-0.45, min(0.45, steer))
        return self._steer


class LaneDrive:
    """用黄线路缘给白线定位，顺着同帧街车间的可行路线转向。"""

    def __init__(self) -> None:
        from maaracing_master.plugins.infinite_drive import (
            LANE_FP16_MODEL_REL, LANE_MODEL_REL, PERCEPTION_MODEL_REL, model_assets,
        )
        from maaracing_master.plugins.infinite_drive.lane_obs import LaneObsConfig, LaneObsWorker
        from maaracing_master.plugins.infinite_drive.perception import StreetPerception
        self._cal = load_calib()
        self._camera_height = BUMPER_CAM_H_M
        self._control = HeadingControl(self._cal.lane_w_m)
        car_weights, car_error = model_assets.select(PERCEPTION_MODEL_REL)
        perception = None
        if car_weights is not None:
            try:
                perception = StreetPerception(str(car_weights))
            except Exception as exc:
                car_error = repr(exc)
        if perception is None:
            logger.log(f"[无限驾驶] 街车模型不可用，本次只保持车道：{car_error}", "WARNING")
        self._lines = LaneObsWorker(LaneObsConfig(
            enabled=True, provider="dml", weights=LANE_MODEL_REL, weights_fp16=LANE_FP16_MODEL_REL,
        ), drive_observations=True, perception=perception)
        self._trace = get_trace_sink("infinite_drive")
        self.begin()

    def start(self) -> None:
        self._lines.start()
        if self._lines.error:
            raise RuntimeError(self._lines.error)
        if self._lines.note:
            logger.log(f"[无限驾驶] {self._lines.note}", "WARNING")

    def stop(self) -> None:
        self._lines.stop()

    def begin(self, *, settle_s: float = 0.0) -> None:
        self._phase_start_ns = time.perf_counter_ns() + round(settle_s * 1e9)
        self._last_frame_ns = 0
        self._last_fid = -1
        self._target: float | None = None
        self._gross_reanchor = 0
        self.release()

    def release(self) -> None:
        self._control.reset()
        self._road_path: RoadPath | None = None
        self._road_path_ns = 0
        self.throttle_scale = 1.0

    def steer(self, frame, fid: int, ts_ns: int) -> float:
        """处理采集帧，返回方向杆。"""
        if ts_ns < self._phase_start_ns:
            return 0.0
        now_ns = time.perf_counter_ns()
        lane = self._lines.latest() or {}
        det_ns = lane.get("det_ts_ns") or 0
        fresh = (det_ns >= self._phase_start_ns
                 and 0 <= now_ns - det_ns <= _MAX_HEADING_AGE_S * 1e9)

        new_frame = fid != self._last_fid
        if new_frame:
            gap = (ts_ns - self._last_frame_ns) / 1e9
            dt = gap / max(1, fid - self._last_fid) if self._last_frame_ns else 0.05
            self._lines.push(frame, fid, self._camera_height, 0.0, dt,
                             not self._last_frame_ns or gap > _FRAME_GAP_S,
                             focal=self._cal.focal_px,
                             frame_ts_ns=ts_ns)
            self._last_fid, self._last_frame_ns = fid, ts_ns

        x = lane.get("x")
        heading = lane.get("theta")
        heading_fid = lane.get("theta_src_fid")
        heading_ns = lane.get("theta_src_ts_ns") or 0
        position_measured = x is not None and math.isfinite(x) and not lane.get("hold")
        track_ready = fresh and position_measured and lane.get("usable")
        usable = (track_ready
                  and heading is not None and math.isfinite(heading)
                  and heading_fid == lane.get("det_fid")
                  and heading_ns >= self._phase_start_ns
                  and 0 <= now_ns - heading_ns <= _MAX_HEADING_AGE_S * 1e9)
        # 线已有绝对道号。稳定检测到当前车道的两边时，锁定其中心作为保持目标。
        if fresh and self._gross_reanchor != lane.get("gross_reanchor", 0):
            self._gross_reanchor = lane["gross_reanchor"]
            self._target = None
            self.release()
        if self._target is None and track_ready and x is not None:
            self._target = _lane_center(x, lane.get("lines", ()))
            if self._target is not None:
                logger.log(f"[无限驾驶] 已锁定当前车道（中心 {self._target:+.1f}）", "DEBUG")
        route = None
        preview = None
        cars = None
        target = self._target
        if now_ns - self._road_path_ns > _PATH_AGE_NS:
            self._road_path = None
        self.throttle_scale = 1.0 if self._road_path is None else self._road_path.throttle
        if usable and target is not None and x is not None and heading is not None:
            cars = _road_cars(lane, x, self._camera_height)
            if cars is not None and abs(x) < ROAD_HALF_LANES:
                if heading_ns != self._road_path_ns:
                    previous = None if self._road_path is None else self._road_path.x_at(LOOKAHEAD_M)
                    self._road_path = plan_road_path(x, target, cars, previous) if cars else None
                    self._road_path_ns = heading_ns
                route = self._road_path
                self.throttle_scale = 1.0 if route is None else route.throttle
            if route is not None:
                preview = route.x_at(LOOKAHEAD_M)
                # 已进入下一道且近段仍在这条道里，之后以这条道作为无车时的保持目标。
                if math.floor(preview) == math.floor(x):
                    self._target = math.floor(x) + 0.5
            goal = target if preview is None else preview
            steer = self._control.update(goal - x, heading, heading_ns,
                                         preview_m=LOOKAHEAD_M if route is not None else None)
            target_heading, yaw_rate = self._control.desired_heading, self._control.yaw_rate
        else:
            self._control.reset()
            steer = 0.0
            target_heading = yaw_rate = None

        self._trace.append({"fid": fid, "ts_ns": ts_ns,
                            "ego_lane": x, "target_lane": self._target, "heading_rad": heading,
                            "road_path": None if route is None else route.points, "path_preview_lane": preview,
                            "road_cars": cars, "throttle_scale": self.throttle_scale,
                            "yellow_anchor_lane": lane.get("yellow_anchor"),
                            "traffic_error": lane.get("traffic_error"),
                            "target_heading_rad": target_heading, "yaw_rate_rad_s": yaw_rate,
                            "lane_fid": lane.get("det_fid"), "lane_ms": lane.get("ms"),
                            "lane_state": lane.get("state"), "lane_locked": lane.get("n_locked"),
                            "heading_fid": heading_fid, "heading_ts_ns": heading_ns or None,
                            "usable": bool(usable), "steer": steer})
        return steer
