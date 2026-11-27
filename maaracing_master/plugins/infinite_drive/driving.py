# -*- coding: utf-8 -*-
"""无限驾驶的道路定位、净空路线选择与横向控制。"""

from __future__ import annotations

import math
import time

from maaracing_master.core.logger import logger
from maaracing_master.core.template_match import load_template, match_template_cs
from maaracing_master.core.trace_sink import get_trace_sink
from maaracing_master.plugins.infinite_drive import IMAGE_DIR
from maaracing_master.plugins.infinite_drive.lane_obs import lane_membership, lane_pos_nearest
from maaracing_master.plugins.infinite_drive.ranges import FAR_MAX_M, MID_MAX_M, NEAR_MAX_M
from maaracing_master.plugins.infinite_drive.world_model import BUMPER_CAM_H_M, GROUND_FOCAL_PX, load_calib
from maaracing_master.plugins.infinite_drive.yellow_line import ROAD_HALF_LANES

_DRIVE_HUD = (
    ("inf_drive_score_rate.png", (0.7539, 0.0333, 0.0672, 0.0458), 0.80),
    ("inf_drive_gear.png", (0.0344, 0.0333, 0.0281, 0.0528), 0.72),
)
_MAX_HEADING_AGE_S = 0.22
_FRAME_GAP_S = 0.8
# 满杆脉宽按现有 20 Hz 控制拍取整。角速度标尺和等待时长取自历史杆响应，
# 用于初始控制参数，按后续观测反馈校正方向。
DRIVE_TICK_S = 0.05
_MAX_PULSE_TICKS = 3
_FULL_STICK_YAW_RAD_S = 0.70
_STEER_RESPONSE_S = 0.20
_PWM_COAST_NS = 150_000_000
_HEADING_DEADBAND_RAD = 0.020
_MAX_TARGET_HEADING_RAD = 0.10
_LATERAL_PREVIEW_S = 0.50
_VLAT_TAU_S = 0.25
# 擦边点位：三条白线两侧各偏 0.2 道共六个横向维持点。0.2 道偏置使本车与
# 相邻车道居中车的横向中心距 ≈2.45m，落入极限超车判定阈（两车半宽和 +
# 0.8m ≈ 2.6m，RULES §2.1）——贴线保持本身就是擦边超车姿态，点位只在
# 被堵时更换，不在道心与擦边之间来回切换。
_EDGE_OFFSET = 0.20
_EDGE_POINTS = tuple(round(b + s * _EDGE_OFFSET, 2) for b in (-1, 0, 1) for s in (-1, 1))
_LANE_CENTERS = tuple(k + 0.5 for k in range(-ROAD_HALF_LANES, ROAD_HALF_LANES))
_CLEARANCE = 0.60                    # 横向车身净空（道）
_ROAD_EDGE_MARGIN = _CLEARANCE / 2   # 从路缘给自车预留半个车身净空
_CENTER_OVERLAP_TOLERANCE = 0.08     # 街车投影在自车中心附近的抖动容差（道）
_DECISION_HORIZON_M = MID_MAX_M       # 给绕行留出起手距离
_WALL_DIAGNOSTIC_HORIZON_M = 2 * NEAR_MAX_M
_DISTANT_ROUTE_CONFIRMATIONS = 3      # 同一绕行目标须由独立检测帧确认
LOOKAHEAD_M = 12.0                   # 前视转向预瞄（米）


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


def _point_blocked(point: float, cars: list[tuple[float, float]], horizon_m: float) -> bool:
    """点位净空判定：视界内任一街车与点位横向中心距小于净空即堵。"""
    return any(0.0 < z <= horizon_m and abs(point - car_x) < _CLEARANCE
               for car_x, z in cars)


def _path_blocked(ego_x: float, target: float, cars: list[tuple[float, float]],
                  horizon_m: float = _DECISION_HORIZON_M) -> bool:
    """用自车到目标的横向扫掠带检查视界内街车。"""
    left, right = sorted((ego_x, target))
    for car_x, distance in cars:
        if not 0.0 < distance <= horizon_m:
            continue
        if abs(target - car_x) < _CLEARANCE:
            return True
        if not left - _CLEARANCE < car_x < right + _CLEARANCE:
            continue
        # 起点已贴近街车时可向外退出。两车中心几乎重合时，投影噪声足以
        # 翻转左右判定，按有净空的落点脱离；其余重叠仍须朝远离街车的一侧。
        offset = car_x - ego_x
        if abs(offset) >= _CLEARANCE or (
            abs(offset) > _CENTER_OVERLAP_TOLERANCE and (target - ego_x) * offset > 0
        ):
            return True
    return False


class HeadingControl:
    """根据预估横位与车头角，用满杆脉冲调整朝向。"""

    def __init__(self, lane_w_m: float) -> None:
        self._lane_w_m = lane_w_m
        self.reset()

    def reset(self) -> None:
        self._ts_ns = 0
        self._heading_ts_ns = 0
        self._heading = 0.0
        self._x_prev: float | None = None
        self.v_lat = 0.0
        self.yaw_rate = 0.0
        self.desired_heading = 0.0
        self.heading_error = 0.0
        self._steer = 0.0
        self._pulse_end_ns = 0
        self._next_pulse_ns = 0
        self._feedback_after_ns = 0

    def neutralize(self, now_ns: int) -> None:
        """松杆后给车辆留出响应时间，包括观测短暂中断的情况。"""
        if self._steer:
            self._steer = 0.0
            self._next_pulse_ns = now_ns + _PWM_COAST_NS

    def update(self, lateral_error_lane: float, heading: float, ts_ns: int, *,
               lateral_x: float | None = None, heading_ts_ns: int | None = None,
               now_ns: int | None = None, near_edge: bool = False) -> float:
        now_ns = ts_ns if now_ns is None else now_ns
        angle_ns = ts_ns if heading_ts_ns is None else heading_ts_ns
        self._observe(lateral_x, heading, ts_ns, angle_ns)

        # 已在横移时，按预计到达的位置收小期望角。偏航率补偿松杆后
        # 仍会发生的转动，反打前后都按预计角度判定，避免等过点才动作。
        lateral_error = lateral_error_lane - self.v_lat * _LATERAL_PREVIEW_S
        desired = math.atan2(lateral_error * self._lane_w_m, LOOKAHEAD_M)
        self.desired_heading = max(-_MAX_TARGET_HEADING_RAD, min(_MAX_TARGET_HEADING_RAD, desired))
        predicted_heading = heading + self.yaw_rate * _STEER_RESPONSE_S
        self.heading_error = error = self.desired_heading - predicted_heading

        # 脉冲按下发时钟结束，复用检测帧也能松杆。路缘只允许向内出杆。
        direction = math.copysign(1.0, error)
        if near_edge and direction * lateral_error_lane <= 0:
            direction = 0.0
        if self._steer:
            if (now_ns < self._pulse_end_ns and direction == self._steer
                    and abs(error) > _HEADING_DEADBAND_RAD / 2):
                return self._steer
            self.neutralize(now_ns)
        if (now_ns < self._next_pulse_ns or angle_ns < self._feedback_after_ns
                or abs(error) <= _HEADING_DEADBAND_RAD or direction == 0):
            return 0.0

        width_ticks = round(abs(error) / (_FULL_STICK_YAW_RAD_S * DRIVE_TICK_S))
        width_s = DRIVE_TICK_S * max(1, min(_MAX_PULSE_TICKS, width_ticks))
        self._pulse_end_ns = now_ns + round(width_s * 1e9)
        self._feedback_after_ns = now_ns + round(_STEER_RESPONSE_S * 1e9)
        self._steer = direction
        return direction

    def _observe(self, lateral_x: float | None, heading: float, ts_ns: int, angle_ns: int) -> None:
        # 横位与车头角各按真实采集时钟更新。沿用的车头角不参与差分。
        if angle_ns > self._heading_ts_ns:
            dt = (angle_ns - self._heading_ts_ns) / 1e9
            if self._heading_ts_ns and dt <= _MAX_HEADING_AGE_S:
                rate = (heading - self._heading) / dt
                self.yaw_rate += dt / (0.10 + dt) * (rate - self.yaw_rate)
            else:
                self.yaw_rate = 0.0
            self._heading_ts_ns, self._heading = angle_ns, heading
        if ts_ns > self._ts_ns:
            dt = (ts_ns - self._ts_ns) / 1e9
            if self._x_prev is not None and lateral_x is not None and dt <= _MAX_HEADING_AGE_S:
                speed = (lateral_x - self._x_prev) / dt
                self.v_lat += dt / (_VLAT_TAU_S + dt) * (speed - self.v_lat)
            else:
                self.v_lat = 0.0
            self._ts_ns, self._x_prev = ts_ns, lateral_x


class LaneDrive:
    """优先保持可通行的路线，在净空允许时贴线超车。"""

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
        self._traffic_enabled = perception is not None
        # hz=0：按控制拍提交（此前按 10Hz 节流，控制拍 20Hz 下一半拍拿不到新
        # 检测，是可用率的大头）。实测 DML+FP16 单帧约 9~11ms（前向 4~6ms、
        # 后处理约 5ms）；是否跟得上以 trace 的弃帧计数为准。
        self._lines = LaneObsWorker(LaneObsConfig(
            enabled=True, provider="dml", weights=LANE_MODEL_REL,
            weights_fp16=LANE_FP16_MODEL_REL, hz=0.0,
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
        self._dbg_refs = None
        self.release()

    def release(self) -> None:
        self._control.reset()
        self._last_route_fid = None
        self._pending_target: float | None = None
        self._pending_count = 0

    def debug_snapshot(self) -> dict | None:
        """本拍调试图快照（只给观测量，不做任何决策计算）。

        由控制拍在 ``steer`` 之后取一次、调试图落盘线程延迟渲染；内容全是
        本拍新建对象或不可变值（``lane`` 是观测线程发布的浅拷贝，``lines``
        为每拍重建的 dict），交出去之后不再改写。无车账时点位状态显式
        ``None``（未测），不把「没证据」画成「净空」。
        """
        refs = self._dbg_refs
        if refs is None:
            return None
        cars = refs["cars"]
        if cars is None:
            # 缺席原因是可读信息：车道原点不可用（决定层本就不出车账）与
            # 「车道可用但同拍车账没跟上」是两回事，调试图分开展示。
            states, reason = None, ("lane" if not refs["track_ready"]
                                    else "traffic")
        else:
            states = [(p, _point_blocked(p, cars, _DECISION_HORIZON_M))
                      for p in _EDGE_POINTS]
            reason = None
        return dict(refs, edge_states=states, edge_reason=reason,
                    clearance=_CLEARANCE, horizon_m=_DECISION_HORIZON_M)

    def _hold_edge_point(self, ego_x: float, cars: list[tuple[float, float]]) -> str | None:
        """沿用净空路线，被堵时按自车位置选最近的可达点。"""
        previous = self._target
        if previous is not None and not _path_blocked(ego_x, previous, cars):
            self._pending_count = 0
            return None
        urgent = _path_blocked(ego_x, ego_x if previous is None else previous, cars, NEAR_MAX_M)
        horizons = (_DECISION_HORIZON_M, NEAR_MAX_M) if urgent else (_DECISION_HORIZON_M,)
        clear = []
        for horizon in horizons:
            clear = [p for p in _EDGE_POINTS if not _path_blocked(ego_x, p, cars, horizon)]
            if not clear:
                clear = [p for p in _LANE_CENTERS if not _path_blocked(ego_x, p, cars, horizon)]
            if clear:
                break
        picked = min(clear, key=lambda p: (abs(p - ego_x), abs(p)), default=None)
        if previous is not None and not urgent:
            self._pending_count = self._pending_count + 1 if picked == self._pending_target else 1
            self._pending_target = picked
            if self._pending_count < _DISTANT_ROUTE_CONFIRMATIONS:
                return None
        self._pending_count = 0
        self._target = picked
        if self._target is None:
            return "blocked" if previous is not None else None
        if previous is None:
            logger.log(f"[无限驾驶] 巡航点位锁定（{self._target:+.1f}）", "DEBUG")
            return "pick"
        logger.log(f"[无限驾驶] 路线被堵，换点 {previous:+.1f} → {self._target:+.1f}", "DEBUG")
        return "switch"

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
        # hold=本拍横位被步长门限速（x 已按物理步长限幅，仍是平滑后的位置
        # 原点，lane_obs 的 usable 也不因它减分）。此前控制侧额外把 hold 判为
        # 不可用，实测占不可用原因的 69%，方向输出被切成一段一段；这里按
        # 「限速值可用」消费，hold 只保留为 trace 的「非新测量」标记。
        position_measured = x is not None and math.isfinite(x)
        track_ready = fresh and position_measured and lane.get("usable")
        usable = (track_ready
                  and heading is not None and math.isfinite(heading)
                  and self._phase_start_ns <= heading_ns <= det_ns
                  and 0 <= now_ns - heading_ns <= _MAX_HEADING_AGE_S * 1e9)
        # 线已有绝对道号，点位用同一坐标系。道号锚重定（黄线复核/丢失）时
        # 绝对坐标整体平移，旧点位作废重选。
        if fresh and self._gross_reanchor != lane.get("gross_reanchor", 0):
            self._gross_reanchor = lane["gross_reanchor"]
            self._target = None
            self.release()
        cars = _road_cars(lane, x, self._camera_height) if track_ready else None
        det_fid = lane.get("det_fid")
        edge_event = None
        if track_ready and (cars is not None or not self._traffic_enabled):
            if det_fid != self._last_route_fid:
                edge_event = self._hold_edge_point(x, cars or [])
                self._last_route_fid = det_fid
        else:
            self._pending_count = 0
            # 刚锁定位置时可先保持贴线点。已有车流证据判定无路可走时，等新证据再选。
            if track_ready and self._last_route_fid is None and self._target is None:
                edge_event = self._hold_edge_point(x, [])
        target = self._target
        near_edge = fresh and position_measured and abs(x) >= ROAD_HALF_LANES - _ROAD_EDGE_MARGIN
        walled = cars is not None and all(
            _point_blocked(p, cars, _WALL_DIAGNOSTIC_HORIZON_M) for p in _EDGE_POINTS
        )

        if usable and target is not None:
            steer = self._control.update(target - x, heading, det_ns, lateral_x=x,
                                         heading_ts_ns=heading_ns, now_ns=now_ns, near_edge=near_edge)
            target_heading = self._control.desired_heading
            yaw_rate = self._control.yaw_rate
            heading_error = self._control.heading_error
        else:
            self._control.neutralize(now_ns)
            steer = 0.0
            target_heading = yaw_rate = heading_error = None

        # 调试图快照只留引用（lane 快照与车账均为本拍新建对象），点位堵/净
        # 在 debug_snapshot 里按需算一次，调试关闭时零额外开销。
        self._dbg_refs = {
            "lane": lane, "ego_x": x, "target": target, "cars": cars,
            "walled": walled, "steer": steer,
            "heading": heading,
            "target_heading": target_heading, "yaw_rate": yaw_rate,
            "usable": bool(usable), "fresh": bool(fresh),
            "track_ready": bool(track_ready),
            "half_lanes": ROAD_HALF_LANES,
        }

        # 本拍不可用原因（诊断）：与 usable 的判定顺序逐条对应，便于离线把
        # 「没出杆」拆成 预测拍/超龄/无位置/未锁/单侧/角度旧。
        if not fresh:
            reason = "stale_det"
        elif x is None:
            reason = "no_x"
        elif not lane.get("usable"):
            reason = lane.get("usable_reason") or "unknown"
        elif not usable:
            reason = "theta"
        else:
            reason = "ok"
        line_meta = [
            {key: ln.get(key) for key in
             ("k", "L", "resid", "accepted", "reject", "side", "color", "n")
             if ln.get(key) is not None}
            for ln in (lane.get("lines") or []) if isinstance(ln, dict)
        ]

        self._trace.append({"fid": fid, "ts_ns": ts_ns,
                            "ego_lane": x, "target_lane": target, "heading_rad": heading,
                            "edge_event": edge_event, "walled": walled,
                            "road_cars": cars,
                            "gross_reanchor": self._gross_reanchor,
                            "yellow_anchor_lane": lane.get("yellow_anchor"),
                            "traffic_error": lane.get("traffic_error"),
                            "target_heading_rad": target_heading, "yaw_rate_rad_s": yaw_rate,
                            "lane_fid": lane.get("det_fid"), "lane_ms": lane.get("ms"),
                            "lane_state": lane.get("state"), "lane_locked": lane.get("n_locked"),
                            "heading_fid": heading_fid, "heading_ts_ns": heading_ns or None,
                            "usable": bool(usable), "steer": steer,
                            "heading_error_rad": heading_error,
                            "v_lat": round(self._control.v_lat, 3) if target_heading is not None else None,
                            "usable_reason": reason, "pred": lane.get("pred"),
                            "hold": lane.get("hold"), "lock_count": lane.get("locked"),
                            "det_age_ms": (round((now_ns - det_ns) / 1e6, 1)
                                           if det_ns else None),
                            "queue_ms": lane.get("queue_ms"), "cap_ms": lane.get("cap_ms"),
                            "slot_ms": lane.get("slot_wait_ms"),
                            "busy_skips": lane.get("busy_skips"),
                            "slot_skips": lane.get("slot_skips"),
                            "run_ms": lane.get("hold_ms"),
                            "line_meta": line_meta})
        return steer
