from __future__ import annotations

import pytest


@pytest.mark.parametrize(("ego", "target", "direction"), [(0.484, 0.2, -1), (0.502, 1.2, 1)])
def test_selected_route_starts_with_full_stick(ego, target, direction) -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    controller = HeadingControl(3.5)
    assert controller.update(target - ego, 0.0, 1_000_000_000, lateral_x=ego) == direction


def test_full_stick_pulse_ends_even_when_detector_reuses_the_same_frame() -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    controller = HeadingControl(3.5)
    t = 1_000_000_000
    assert controller.update(0.7, 0.0, t, now_ns=t) == 1
    assert controller.update(0.7, 0.0, t, now_ns=t + 200_000_000) == 0
    assert controller.update(0.7, 0.0, t, now_ns=t + 500_000_000) == 0


@pytest.mark.parametrize("side", [-1, 1])
def test_countersteer_starts_before_crossing_and_releases_before_heading_crosses(side) -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    controller = HeadingControl(3.5)
    t = 1_000_000_000
    # 横位还没过目标，但已在快速接近，车头也还朝向横移方向。
    assert controller.update(side * 0.23, side * 0.066, t, lateral_x=side * 0.22) == 0
    command = controller.update(side * 0.11, side * 0.085, t + 200_000_000,
                                lateral_x=side * 0.34)
    assert command == -side
    # 反打已让车头朝路向回转。有剩余偏航率时要提前松杆。
    command = controller.update(side * 0.10, side * 0.040, t + 300_000_000,
                                lateral_x=side * 0.35)
    assert command == 0


def test_heading_control_turns_toward_target_and_damps_on_approach() -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    right = HeadingControl(3.5)
    left = HeadingControl(3.5)
    start = right.update(0.5, 0.0, 1_000_000_000)
    mirror = left.update(-0.5, 0.0, 1_000_000_000)
    assert start > 0.0
    assert mirror == pytest.approx(-start)
    # 位置环：误差缩到 0 时输出回落（车头角只作弱趋势，不再靠它压杆）。
    assert abs(right.update(0.0, 0.035, 1_100_000_000)) < start


def test_heading_control_reuses_measurement_without_reintegrating() -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    controller = HeadingControl(3.5)
    assert controller.update(0.0, 0.0, 1_000_000_000) == 0.0
    command = controller.update(0.8, -0.03, 1_100_000_000)
    assert command > 0.0
    assert controller.update(0.8, -0.03, 1_100_000_000) == command
    controller.reset()
    assert controller.update(0.02, 0.0, 2_000_000_000) == 0.0


def test_rejected_line_angle_keeps_its_original_timestamp() -> None:
    from maaracing_master.plugins.infinite_drive.lane_obs import LineTracker

    def lines(angle):
        return [{"L": side * 0.5, "side": side, "theta": angle} for side in (-1, 1)]

    tracker = LineTracker()
    initial = tracker.update(lines(0.0), 1, 0.05, anchor=0.5, frame_ts_ns=1_000_000_000)
    assert initial["theta_updated"]
    rejected = tracker.update(lines(0.25), 2, 0.05, anchor=0.5, frame_ts_ns=1_050_000_000)
    assert not rejected["theta_updated"]
    assert rejected["theta_src_fid"] == initial["theta_src_fid"]
    assert rejected["theta_src_ts_ns"] == initial["theta_src_ts_ns"]


def test_larger_heading_error_produces_longer_full_stick_pulse() -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    t = 1_000_000_000
    short = HeadingControl(3.5)
    long = HeadingControl(3.5)
    assert short.update(0.12, 0.0, t) == long.update(0.7, 0.0, t) == 1.0
    assert short.update(0.12, 0.0, t + 50_000_000) == 0.0
    assert long.update(0.7, 0.0, t + 50_000_000) == 1.0
    assert long.update(0.7, 0.0, t + 150_000_000) == 0.0


@pytest.mark.parametrize(("yaw_gain", "delay_ticks", "speed"), [
    (0.4, 4, 4.0), (0.4, 4, 8.0), (0.65, 2, 8.0), (0.65, 4, 12.0),
])
def test_pwm_lateral_control_settles_with_delayed_yaw_response(yaw_gain, delay_ticks, speed) -> None:
    from collections import deque

    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    controller = HeadingControl(3.5)
    delayed_commands = deque([0.0] * delay_ticks)
    position = heading = 0.0
    positions = []
    for tick in range(140):
        command = controller.update(0.8 - position, heading, 1_000_000_000 + tick * 50_000_000,
                                    lateral_x=position)
        delayed_commands.append(command)
        heading += 0.05 * (yaw_gain * delayed_commands.popleft() - heading)
        position += 0.05 * speed * heading
        positions.append(position)
    assert max(positions) < 0.95
    assert max(abs(0.8 - x) for x in positions[80:]) < 0.15


def test_lane_drive_constructs_without_street_model(monkeypatch) -> None:
    from maaracing_master.plugins.infinite_drive import PERCEPTION_MODEL_REL, model_assets
    from maaracing_master.plugins.infinite_drive.driving import LaneDrive

    def select(rel):
        assert rel == PERCEPTION_MODEL_REL
        return None, "模型缺失"

    monkeypatch.setattr(model_assets, "select", select)
    LaneDrive()


def test_drive_hud_uses_plugin_template_at_its_screen_position() -> None:
    import numpy as np

    from maaracing_master.core.template_match import load_template
    from maaracing_master.plugins.infinite_drive import IMAGE_DIR
    from maaracing_master.plugins.infinite_drive.driving import drive_hud_visible

    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    template = load_template("inf_drive_gear.png", [IMAGE_DIR])
    assert template is not None
    height, width = template.shape[:2]
    image[28:28 + height, 47:47 + width] = template
    assert drive_hud_visible(image)
    assert not drive_hud_visible(np.roll(image, 100, axis=1))


def test_driving_score_bar_recognizes_hud_when_gear_is_unreadable() -> None:
    import numpy as np

    from maaracing_master.core.template_match import load_template
    from maaracing_master.plugins.infinite_drive import IMAGE_DIR
    from maaracing_master.plugins.infinite_drive.driving import drive_hud_visible

    frame = np.full((720, 1280, 3), 235, dtype=np.uint8)
    template = load_template("inf_drive_score_rate.png", [IMAGE_DIR])
    assert template is not None
    h, w = template.shape[:2]
    frame[29:29 + h, 971:971 + w] = template
    assert drive_hud_visible(frame)
    assert not drive_hud_visible(np.roll(frame, 120, axis=0))


@pytest.mark.parametrize("camera_height", [None, 1.25, 1.9])
def test_opening_observation_never_requests_camera_input(monkeypatch, camera_height) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d

    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    driver._trace = SimpleNamespace(append=lambda _: None)
    obs = {}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    now = 1_000_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver.begin()

    for fid in range(12):
        now = 1_000_000_000 + fid * 120_000_000
        obs.update(det_fid=fid, det_ts_ns=now, cam_h_lane=camera_height)
        assert driver.steer(None, fid, now) == 0.0


def _line_fits() -> list[dict]:
    """五条竖直线（100px/道，自车像素 640）：L=k−0.5，像素=640+100·L。

    obs 里 L 是线的自车相对道坐标（绝对道号 k 减自车横位），所以车框绝对
    横位 p、自车 x 下的框心像素 = 640 + 100·(p − x)。"""
    return [{"accepted": True, "k": k, "side": -1 if k <= 0 else 1, "L": k - 0.5,
             "fits": [{"s": 0., "a": 640 + 100 * (k - 0.5), "y0": 380., "y1": 700., "rms": 1.}]}
            for k in range(-2, 3)]


def _car_x(p: float, ego_x: float, z_m: float = 9.96):
    from maaracing_master.plugins.infinite_drive.perception import Detection
    from maaracing_master.plugins.infinite_drive.world_model import GROUND_FOCAL_PX

    bottom = round(GROUND_FOCAL_PX * 1.25 / z_m) + 365
    return Detection(round(640 + 100 * (p - ego_x)), bottom - 20, 44, 40, .9)


@pytest.fixture
def drive_tick(monkeypatch):
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d
    from maaracing_master.plugins.infinite_drive.perception import PerceptionResult

    now = 1_000_000_000
    fid = 0
    heading_fid = heading_ns = 0
    obs = {}
    rows = []
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    driver._traffic_enabled = True
    driver._trace = SimpleNamespace(append=rows.append)
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver.begin()

    def tick(x, cars=(), *, usable=True, hold=False, new_angle=True, elapsed_s=0.05):
        nonlocal now, fid, heading_fid, heading_ns
        now += round(elapsed_s * 1e9)
        fid += 1
        if new_angle:
            heading_fid, heading_ns = fid, now
        traffic = (None if cars is None else
                   PerceptionResult(fid, now, cars=[_car_x(p, x, z) for p, z in cars]))
        # 横位尺随自车平移，保持框和白线在同一个道路坐标系。
        lines = _line_fits()
        for line in lines:
            line["L"] = line["k"] - x
            line["fits"][0]["a"] = 640 + 100 * line["L"]
        obs.update(x=x, theta=0.0, usable=usable, hold=hold, y_v=365., cam_h=1.25, lines=lines,
                   det_fid=fid, det_ts_ns=now, theta_src_fid=heading_fid, theta_src_ts_ns=heading_ns,
                   traffic=traffic)
        driver.steer(None, fid, now)
        return rows[-1]

    return tick


def test_immediate_threat_overrides_recent_target(drive_tick) -> None:
    assert drive_tick(-0.4)["steer"] == 1
    row = drive_tick(-0.4, [(0.0, 10)])
    assert row["target_lane"] == -0.8
    assert row["steer"] == 0  # 立即撤掉朝向障碍的脉冲，等转向反馈再发反向杆。
    assert drive_tick(-0.4, [(0.0, 9)], elapsed_s=0.15)["steer"] == -1
    assert drive_tick(-0.8)["target_lane"] == -0.8


def test_clear_destination_does_not_justify_crossing_traffic(drive_tick) -> None:
    assert drive_tick(0.8)["target_lane"] == 0.8
    # 左侧 -1.2 是净点，但抵达前须横穿 -0.3 附近的车。
    row = drive_tick(0.8, [(1.48, 10), (0.7, 12), (-0.3, 14)])
    assert row["target_lane"] is None
    assert row["steer"] == 0
    assert row["walled"] is False


def test_close_threat_can_use_escape_blocked_only_farther_ahead(drive_tick) -> None:
    assert drive_tick(-0.4)["target_lane"] == -0.2
    row = drive_tick(-0.4, [(0.0, 10), (-1.0, 30)])
    assert row["target_lane"] == -0.8
    assert row["steer"] <= 0


def test_right_escape_stays_selected_when_same_lane_truck_projection_shifts(drive_tick) -> None:
    # 22:45 第一阶段 #1337–#1350。右侧落点始终有净空，货车中心在自车附近抖动。
    frames = [(0.500, 0.454, -0.352, 22.9), (0.470, 0.544, -0.346, 22.1),
              (0.497, 0.554, -0.526, 17.4), (0.500, 0.483, -0.607, 15.6),
              (0.462, 0.468, -0.597, 13.4), (0.497, 0.485, -0.488, 10.4)]
    for ego, truck, other, distance in frames:
        row = drive_tick(ego, [(truck, distance), (other, distance)])
        assert row["target_lane"] == 1.2
        assert row["steer"] >= 0
    assert drive_tick(0.497, [(0.554, 10), (-0.526, 9), (1.2, 8)])["target_lane"] != 1.2


def test_clear_escape_path_keeps_speed_when_traffic_is_still_ahead(drive_tick) -> None:
    assert drive_tick(-0.4)["target_lane"] == -0.2
    assert drive_tick(-0.4, [(0.0, 33)])["target_lane"] == -0.2
    assert drive_tick(-0.4, [(0.0, 32)])["target_lane"] == -0.2
    row = drive_tick(-0.4, [(0.0, 31)])
    assert row["target_lane"] == -0.8
    assert row["steer"] <= 0


def test_far_car_coordinate_flips_do_not_flip_target(drive_tick) -> None:
    assert drive_tick(1.36)["target_lane"] == 1.2
    # 22:03 局第二阶段 #2722–#2729，同一辆货车的横位跳来跳去。
    frames = [(0.902, 0.95, 32.3), (0.687, -0.02, 33.4), (0.604, 0.64, 34.6),
              (0.544, 0.52, 34.8), (0.484, 0.50, 35.0)]
    for ego_x, car_x, distance in frames:
        assert drive_tick(ego_x, [(car_x, distance)])["target_lane"] == 1.2


def test_clear_lane_center_keeps_speed_when_edge_points_are_blocked(drive_tick) -> None:
    assert drive_tick(0.5)["target_lane"] == 0.2
    row = drive_tick(0.5, [(-0.2, 10), (1.2, 10)])
    assert row["target_lane"] == 0.5


def test_road_lock_dropout_neutralizes_steering_until_recovery(drive_tick) -> None:
    assert drive_tick(0.4)["steer"] < 0
    assert drive_tick(0.4, usable=False)["steer"] == 0
    assert drive_tick(0.4, usable=False, elapsed_s=0.30)["steer"] == 0
    assert drive_tick(0.4)["steer"] < 0


def test_fresh_position_can_use_recent_heading_but_not_expired_heading(drive_tick) -> None:
    assert drive_tick(0.8)["steer"] == 0
    steering = drive_tick(0.65, new_angle=False)
    assert steering["usable"]
    assert steering["steer"] == 1
    expired = drive_tick(0.65, new_angle=False, elapsed_s=0.45)
    assert not expired["usable"]
    assert expired["steer"] == 0


@pytest.mark.parametrize("side", [-1, 1])
def test_at_road_edge_only_steers_inward_on_valid_observation(drive_tick, side) -> None:
    drive_tick(side * 1.2)
    assert drive_tick(side * 1.8, usable=False)["steer"] == 0
    assert side * drive_tick(side * 1.8)["steer"] < 0


def test_lane_tracking_can_start_and_continue_with_missing_traffic(drive_tick) -> None:
    initial = drive_tick(0.6, None)
    assert initial["target_lane"] == 0.8
    assert initial["steer"] > 0
    assert drive_tick(0.6, None, elapsed_s=0.1)["steer"] == 0
    missing = drive_tick(0.6, None, elapsed_s=0.2)
    assert missing["target_lane"] == 0.8
    assert missing["steer"] == 1


def test_missing_traffic_does_not_reselect_an_already_blocked_route(drive_tick) -> None:
    assert drive_tick(0.8)["target_lane"] == 0.8
    assert drive_tick(0.8, [(1.48, 10), (0.7, 12), (-0.3, 14)])["target_lane"] is None
    missing = drive_tick(0.8, None)
    assert missing["target_lane"] is None
    assert missing["steer"] == 0


def test_debug_snapshot_reports_why_points_are_missing() -> None:
    """调试图快照区分缺席原因：车道原点不可用 vs 同拍车账缺席 vs 有点位。"""
    from maaracing_master.plugins.infinite_drive import driving as d

    driver = d.LaneDrive.__new__(d.LaneDrive)
    base = {"lane": {}, "ego_x": 0.0, "target": 0.2, "cars": None,
            "walled": False, "steer": 0.0,
            "heading": 0.0, "target_heading": 0.0, "yaw_rate": 0.0,
            "usable": False, "fresh": True, "half_lanes": 2}

    driver._dbg_refs = dict(base, track_ready=False)
    snap = driver.debug_snapshot()
    assert snap["edge_states"] is None and snap["edge_reason"] == "lane"

    driver._dbg_refs = dict(base, track_ready=True)
    snap = driver.debug_snapshot()
    assert snap["edge_states"] is None and snap["edge_reason"] == "traffic"

    driver._dbg_refs = dict(base, track_ready=True, cars=[(0.2, 10.0)])
    snap = driver.debug_snapshot()
    assert snap["edge_reason"] is None
    assert dict(snap["edge_states"])[0.2] is True

    driver._dbg_refs = None
    assert driver.debug_snapshot() is None


def test_edge_points_hug_each_white_line_on_both_sides() -> None:
    from maaracing_master.plugins.infinite_drive.driving import _EDGE_OFFSET, _EDGE_POINTS

    assert _EDGE_OFFSET == 0.20
    assert set(_EDGE_POINTS) == {-1.2, -0.8, -0.2, 0.2, 0.8, 1.2}


def test_lane_holds_nearest_edge_point_and_steers_toward_it(drive_tick) -> None:
    row = drive_tick(0.6)
    assert row["steer"] > 0
    assert row["target_lane"] == 0.8
    assert row["edge_event"] == "pick"


def test_rate_limited_position_still_steers(drive_tick) -> None:
    """限速跟随拍（hold）的横位是平滑后的原点，可以继续转向。"""
    row = drive_tick(0.6, hold=True)
    assert row["steer"] > 0
    assert row["usable"] is True
    assert row["hold"] is True


def test_blocked_point_switches_once_and_holds_after_clearing(drive_tick) -> None:
    assert drive_tick(0.8)["target_lane"] == 0.8
    row = drive_tick(0.8, [(0.58, 9.96)])
    assert row["target_lane"] == 1.2
    assert row["edge_event"] == "switch"
    row = drive_tick(0.8)
    assert row["target_lane"] == 1.2
    assert row["edge_event"] is None


def test_walled_road_keeps_steering_neutral_until_any_point_clears(drive_tick) -> None:
    row = drive_tick(0.5, [(p, 9.96) for p in (-1.5, -0.5, 0.5, 1.5)])
    assert row["target_lane"] is None
    assert row["walled"] is True
    assert row["steer"] == 0
    recovered = drive_tick(0.5)
    assert recovered["target_lane"] == 0.2


def test_far_car_does_not_trigger_a_switch(drive_tick) -> None:
    assert drive_tick(0.8)["target_lane"] == 0.8
    row = drive_tick(0.8, [(0.8, 45.2)])
    assert row["target_lane"] == 0.8
    assert row["edge_event"] is None


def test_second_round_steers_when_road_recovers_without_camera_height(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d

    now = 1_000_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._traffic_enabled = False
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    driver._trace = SimpleNamespace(append=lambda _: None)
    pushed = []
    obs = {}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda _, fid, *a, **kw: pushed.append(fid))
    driver.begin(settle_s=0.25)
    assert driver.steer(None, 0, now) == 0.0
    assert pushed == []
    for fid in range(1, 85):
        now = 1_250_000_000 + fid * 100_000_000
        obs.update(det_fid=fid, det_ts_ns=now, cam_h_lane=None)
        assert driver.steer(None, fid, now) == 0.0
    assert pushed[-1] == 84

    now += 100_000_000
    obs.update(det_fid=85, det_ts_ns=now, theta_src_fid=85, theta_src_ts_ns=now,
               x=0.55, theta=0.02, usable=True, lines=[
                   {"accepted": True, "k": 0, "side": -1},
                   {"accepted": True, "k": 1, "side": 1},
               ])
    # 恢复首拍：车头近似朝前，横位误差足以向右修正。
    assert driver.steer(None, 85, now) > 0.0
    now += 300_000_000
    assert driver.steer(None, 86, now) == 0.0


def test_road_projection_uses_box_bottom_and_excludes_roadside_parking() -> None:
    from maaracing_master.plugins.infinite_drive.driving import _road_cars
    from maaracing_master.plugins.infinite_drive.perception import Detection, PerceptionResult
    from maaracing_master.plugins.infinite_drive.world_model import GROUND_FOCAL_PX

    def line(k, pixel):
        return {"accepted": True, "L": k - 0.5, "fits": [
            {"s": 0., "a": pixel, "y0": 375., "y1": 710., "rms": 1.}]}

    result = PerceptionResult(12, 100, cars=[
        Detection(750, 415, 35, 30, .9), Detection(950, 415, 35, 30, .9)])
    lane = {"lines": [line(0, 600), line(1, 700), line(2, 800)],
            "x": 0.5, "y_v": 365., "cam_h": 1.25, "det_fid": 12, "det_ts_ns": 100,
            "traffic": result}
    cars = _road_cars(lane, 0.5, 1.25)
    assert cars == [pytest.approx((1.5, GROUND_FOCAL_PX * 1.25 / 65.0))]
    assert _road_cars(dict(lane, det_fid=13), 0.5, 1.25) is None
    assert _road_cars(dict(lane, det_ts_ns=101), 0.5, 1.25) is None
    assert _road_cars(dict(lane, lines=[]), 0.5, 1.25) is None
