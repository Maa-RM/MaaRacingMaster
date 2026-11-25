# -*- coding: utf-8 -*-
"""无限驾驶的道路定位、擦边点位维持与车头角闭环。"""

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
# 执行链平滑（自 v1 下发链搬来的最小集，2026-11-25）：
# - 杆值一阶低通（v1 tau_steer_s=0.1）：去掉逐拍阶跃，让执行延迟吃掉突变；
# - 每拍变化上限（v1 rate_limit_raw=22000/32767≈0.67 归一化）：满行程约 3 拍，
#   本侧杆幅上限只有 0.45，同比例取 0.30；
# - 浅杆段补偿：v1 按「命令车头角 − 观测车头角」缺口积分。本侧观测是线角
#   （含道路方向，无慢基线，见 lane_obs `theta` 定义），直接积分会把曲率积进去，
#   故改为对持续横位偏差积分，目的相同（补浅杆段响应不足），上限保守。
_STEER_TAU_S = 0.10
_STEER_STEP_MAX = 0.30
_BOOST_GAIN = 0.8          # 杆/(道·s)
_BOOST_MAX = 0.12          # 杆
_BOOST_DECAY_S = 1.0
# 混合横向控制（2026-11-25，由两局实测定口径）：
# - 杆 → 横速不是即时量：打杆后 0.25/0.5/1.0s 的横速增益约 0.39/0.75/1.21 道/s，
#   存在约 0.5s 的执行滞后，纯位置 P 在这条链上必然振荡；
# - 车头角对打杆的响应只有 0.2°/0.5s，且三成检测拍沿用旧角，不能单独当阻尼；
#   但它与未来 0.5s 横移的相关系数 0.61，仍可作趋势项。
# 因此：位置误差 → 期望横速 → 内环按「在线估计的杆增益」归一化成杆；车头角
# 只作弱趋势，角速度作弱阻尼。换车/换速时杆增益在线跟随，增益失配不会失稳。
_K_POS = 0.5               # 位置环：期望横速 = 0.5 × 横位误差（道/s）；误差 1 道约 0.25 杆
_K_HEAD = 2.0              # 车头角趋势项（原 3.5，降权，避开弱观测）
_K_R = 0.3                 # 车头角速度阻尼
_VLAT_TAU_S = 0.40         # 横位速度 EMA（检测间隔 0.1s，0.2 压不住差分噪声）
_GAIN_DEFAULT = 1.0        # 杆→横速初始增益（道/s per 归一杆）
_GAIN_MIN, _GAIN_MAX = 0.6, 3.0
_GAIN_TAU_S = 3.0          # 增益在线估计的时间常数
_GAIN_PROBE_OUT = 0.35     # 只在未饱和段做增益辨识（饱和时观测横速被限幅，会低估增益）
# 擦边点位：三条白线两侧各偏 0.2 道共六个横向维持点。0.2 道偏置使本车与
# 相邻车道居中车的横向中心距 ≈2.45m，落入极限超车判定阈（两车半宽和 +
# 0.8m ≈ 2.6m，RULES §2.1）——贴线保持本身就是擦边超车姿态，点位只在
# 被堵时更换，不在道心与擦边之间来回切换。
_EDGE_OFFSET = 0.20
_EDGE_POINTS = tuple(round(b + s * _EDGE_OFFSET, 2) for b in (-1, 0, 1) for s in (-1, 1))
_CLEARANCE = 0.60                    # 车身净空（道）：点位与街车中心距小于此视为堵
_DECISION_HORIZON_M = MID_MAX_M      # 换点判定视界：40m ≈ 一次变道来得及起手的下限
_THROTTLE_HORIZON_M = 2 * NEAR_MAX_M  # 收油视界：30m 内六点全堵才收油
# 点位承诺（2026-11-25）：实测车流密时目标每 0.42s 换一次、二成跨 2~4 个点，
# 车被要求来回横跳。选定后至少保持，只有近处车逼到本位才重估；重估优先相邻
# 擦边点，相邻都堵才允许跨道回退（安全兜底，仍受承诺期约束）。
_TARGET_HOLD_S = 1.0                 # 最短驻留
_NEAR_THREAT_M = 25.0                # 近处威胁视界：这个距离内的车才许可提前换点
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


def _pick_edge_point(anchor: float, cars: list[tuple[float, float]]) -> float | None:
    """选离锚点最近的净擦边点；六点全堵返回 None。并列时靠路中心优先。"""
    clear = [p for p in _EDGE_POINTS if not _point_blocked(p, cars, _DECISION_HORIZON_M)]
    if not clear:
        return None
    return min(clear, key=lambda p: (abs(p - anchor), abs(p)))


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
        self._out = 0.0          # 上一拍下发值（低通与变化限幅的起点）
        self._boost = 0.0        # 浅杆补偿量
        self.boost = 0.0         # 只读诊断
        self._x_prev: float | None = None
        self.v_lat = 0.0         # 横位速度 EMA（道/s）
        self.gain = _GAIN_DEFAULT  # 杆→横速在线增益（道/s per 归一杆）

    def update(self, lateral_error_lane: float, heading: float, ts_ns: int, *,
               preview_m: float | None = None,
               lateral_x: float | None = None) -> float:
        if ts_ns <= self._ts_ns:
            return self._steer
        dt = (ts_ns - self._ts_ns) / 1e9
        if self._ts_ns and dt <= _MAX_HEADING_AGE_S:
            rate = (heading - self._heading) / dt
            self.yaw_rate += dt / (0.16 + dt) * (rate - self.yaw_rate)
        else:
            self.yaw_rate = 0.0
        self._ts_ns, self._heading = ts_ns, heading
        # 可用拍之间可能隔着不可用拍，步长按控制拍兜底，避免低通/积分吃进长空窗。
        dt_eff = dt if 0.0 < dt <= 0.10 else 0.05
        dt_meas = dt if 0.0 < dt <= 0.20 else 0.10

        # 横位速度：位置环的阻尼源。车头角只作趋势，不用它算微分。
        if lateral_x is not None:
            if self._x_prev is not None:
                raw_v = (lateral_x - self._x_prev) / dt_meas
                a_v = 1.0 - math.exp(-dt_meas / _VLAT_TAU_S)
                self.v_lat += a_v * (raw_v - self.v_lat)
            self._x_prev = lateral_x

        # 空间预瞄生成温和的目标角，D 项只看测得的角速度，目标更新不踢杆。
        lookahead = 45.0 if preview_m is None else preview_m
        desired = math.atan2(lateral_error_lane * self._lane_w_m, lookahead)
        self.desired_heading = max(-0.10, min(0.10, desired))

        # 浅杆补偿：对持续横位偏差慢速积分，偏差消失后按时间常数衰减。
        self._boost += _BOOST_GAIN * lateral_error_lane * dt_eff
        self._boost *= math.exp(-dt_eff / _BOOST_DECAY_S)
        self._boost = max(-_BOOST_MAX, min(_BOOST_MAX, self._boost))
        self.boost = self._boost

        # 位置环 → 期望横速 → 用在线杆增益换回杆；车头角趋势与角速度阻尼只做修正。
        v_des = _K_POS * lateral_error_lane
        raw = ((v_des - self.v_lat) / self.gain
               + _K_HEAD * (self.desired_heading - heading)
               - _K_R * self.yaw_rate + self._boost)
        # 换车/换速时对象增益会变（同杆值横速不同）：用「上一拍实际下发值 → 当前
        # 横速」慢速更新。只在未饱和段辨识——杆打到限幅时观测横速被限住，会把
        # 增益估计拉低，下一拍就要求更大杆值，形成饱和正反馈（实测杆幅中位已顶
        # 到 0.45 上限）。
        if (abs(self._out) < _GAIN_PROBE_OUT
                and abs(self._out) > 0.08
                and abs(self.v_lat) > 0.05):
            g_sample = self.v_lat / self._out
            if _GAIN_MIN <= g_sample <= _GAIN_MAX:
                a_g = 1.0 - math.exp(-dt_meas / _GAIN_TAU_S)
                self.gain += a_g * (g_sample - self.gain)
        # 小指令归真零，留给游戏自己的回正。死区放在低通**之前**：低通首拍只
        # 给到 39%，若先低通再判死区，正常修正会被首拍衰减吞掉（杆幅上限
        # 0.45、死区 0.05，占比远大于 v1 的 0.008）。
        deadzone = 0.10 if preview_m is None else 0.05
        cmd = 0.0 if abs(raw) < deadzone else raw
        # 杆值低通 → 每拍变化上限：执行链上的两道平滑，再进杆幅限幅。
        alpha = 1.0 - math.exp(-dt_eff / _STEER_TAU_S)
        smooth = self._out + alpha * (cmd - self._out)
        step = max(self._out - _STEER_STEP_MAX,
                   min(self._out + _STEER_STEP_MAX, smooth))
        # 限幅避开卡牌触发的满杆换道。
        self._out = max(-0.45, min(0.45, step))
        self._steer = self._out
        return self._steer


class LaneDrive:
    """用黄线路缘给白线定位，维持六个擦边点位之一，被堵才换点。"""

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
        self._target_since_ns = 0
        self._gross_reanchor = 0
        self._dbg_refs = None
        self.release()

    def release(self) -> None:
        self._control.reset()
        self.throttle_scale = 1.0

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
        """点位决策：初选最近的净点，之后带承诺期，只有近车逼到才重估。

        - 初选：同过去，按 40m 视界选离自车最近的净擦边点。
        - 承诺期（``_TARGET_HOLD_S``）内不换，除非目标点被 25m 内的车堵住；
          远处的车不算——它们进入近处时再判，避免车流密集时目标每拍变。
        - 重估优先相邻擦边点；相邻都堵才跨道回退到最近净点（安全兜底）。
        - 保持中的点不因净空恢复切回，切换只由「近车逼到 + 承诺期已过」触发。
        返回本拍点位事件（"pick"/"switch"），无变化返回 None。"""
        now_ns = time.perf_counter_ns()
        if self._target is None:
            picked = _pick_edge_point(ego_x, cars)
            if picked is not None:
                self._target = picked
                self._target_since_ns = now_ns
                logger.log(f"[无限驾驶] 擦边点位锁定（{picked:+.1f}）", "DEBUG")
                return "pick"
            return None
        if not _point_blocked(self._target, cars, _NEAR_THREAT_M):
            return None
        if now_ns - self._target_since_ns < _TARGET_HOLD_S * 1e9:
            return None
        anchor = self._target
        near = [p for p in _EDGE_POINTS
                if abs(_EDGE_POINTS.index(p) - _EDGE_POINTS.index(anchor)) == 1
                and not _point_blocked(p, cars, _DECISION_HORIZON_M)]
        clear = near or [p for p in _EDGE_POINTS
                         if not _point_blocked(p, cars, _DECISION_HORIZON_M)]
        if not clear:
            return None
        picked = min(clear, key=lambda p: abs(p - ego_x))
        if picked == self._target:
            return None
        logger.log(f"[无限驾驶] 点位被堵，换点 {self._target:+.1f} → {picked:+.1f}"
                   + ("" if near else "（跨道回退）"), "DEBUG")
        self._target = picked
        self._target_since_ns = now_ns
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
                  and heading_fid == lane.get("det_fid")
                  and heading_ns >= self._phase_start_ns
                  and 0 <= now_ns - heading_ns <= _MAX_HEADING_AGE_S * 1e9)
        # 线已有绝对道号，点位用同一坐标系。道号锚重定（黄线复核/丢失）时
        # 绝对坐标整体平移，旧点位作废重选。
        if fresh and self._gross_reanchor != lane.get("gross_reanchor", 0):
            self._gross_reanchor = lane["gross_reanchor"]
            self._target = None
            self.release()
        cars = None
        edge_event = None
        if track_ready and x is not None:
            cars = _road_cars(lane, x, self._camera_height)
            # 同拍车账缺席（traffic 未跟上检测拍）按无证据处理：初选照常，
            # 换点判定不做——拿旧车账换点会切进真实存在的车。
            evidence: list[tuple[float, float]] = cars if cars is not None else []
            if cars is None or abs(x) < ROAD_HALF_LANES:
                edge_event = self._hold_edge_point(x, evidence)
        walled = False
        if cars is not None and x is not None and abs(x) < ROAD_HALF_LANES:
            walled = all(_point_blocked(p, cars, _THROTTLE_HORIZON_M)
                         for p in _EDGE_POINTS)
        self.throttle_scale = 0.0 if walled else 1.0

        target = self._target
        if usable and target is not None and x is not None and heading is not None:
            steer = self._control.update(target - x, heading, heading_ns,
                                         preview_m=LOOKAHEAD_M, lateral_x=x)
            target_heading, yaw_rate = self._control.desired_heading, self._control.yaw_rate
        else:
            self._control.reset()
            steer = 0.0
            target_heading = yaw_rate = None

        # 调试图快照只留引用（lane 快照与车账均为本拍新建对象），点位堵/净
        # 在 debug_snapshot 里按需算一次，调试关闭时零额外开销。
        self._dbg_refs = {
            "lane": lane, "ego_x": x, "target": target, "cars": cars,
            "walled": walled, "steer": steer,
            "throttle_scale": self.throttle_scale, "heading": heading,
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
                            "road_cars": cars, "throttle_scale": self.throttle_scale,
                            "yellow_anchor_lane": lane.get("yellow_anchor"),
                            "traffic_error": lane.get("traffic_error"),
                            "target_heading_rad": target_heading, "yaw_rate_rad_s": yaw_rate,
                            "lane_fid": lane.get("det_fid"), "lane_ms": lane.get("ms"),
                            "lane_state": lane.get("state"), "lane_locked": lane.get("n_locked"),
                            "heading_fid": heading_fid, "heading_ts_ns": heading_ns or None,
                            "usable": bool(usable), "steer": steer,
                            "boost": self._control.boost,
                            "v_lat": round(self._control.v_lat, 3),
                            "gain": round(self._control.gain, 3),
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
