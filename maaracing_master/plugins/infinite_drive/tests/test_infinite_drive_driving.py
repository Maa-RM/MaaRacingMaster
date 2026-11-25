from __future__ import annotations

import pytest


def test_heading_control_turns_toward_target_and_damps_on_approach() -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    right = HeadingControl(3.5)
    left = HeadingControl(3.5)
    start = right.update(0.5, 0.0, 1_000_000_000)
    mirror = left.update(-0.5, 0.0, 1_000_000_000)
    assert start > 0.0
    assert mirror == pytest.approx(-start)
    assert abs(right.update(0.5, 0.035, 1_100_000_000)) < start


def test_heading_control_reuses_measurement_without_reintegrating() -> None:
    from maaracing_master.plugins.infinite_drive.driving import HeadingControl

    controller = HeadingControl(3.5)
    assert controller.update(0.0, 0.0, 1_000_000_000) == 0.0
    command = controller.update(0.8, -0.03, 1_100_000_000)
    assert command > 0.0
    assert controller.update(0.8, -0.03, 1_100_000_000) == command
    controller.reset()
    assert controller.update(0.02, 0.0, 2_000_000_000) == 0.0


def test_steer_command_is_smoothed_by_lowpass_and_rate_limit() -> None:
    """执行链平滑：首拍不给满、相邻拍变化不超过上限、随后逼近稳态。"""
    from maaracing_master.plugins.infinite_drive import driving as d

    controller = d.HeadingControl(3.5)
    t = 1_000_000_000
    first = controller.update(-0.5, 0.0, t, preview_m=12.0)
    # 未滤波的命令饱和到 -0.45；首拍只出一部分。
    assert -0.25 < first < 0.0
    prev = first
    for i in range(1, 25):
        out = controller.update(-0.5, 0.0, t + i * 50_000_000, preview_m=12.0)
        assert abs(out - prev) <= d._STEER_STEP_MAX + 1e-9
        prev = out
    assert prev == pytest.approx(-0.45, abs=0.02)


def test_sustain_boost_grows_on_persistent_error_and_decays() -> None:
    """浅杆补偿：持续同向偏差抬升杆值，偏差消失后按时间常数衰减。"""
    from maaracing_master.plugins.infinite_drive import driving as d

    controller = d.HeadingControl(3.5)
    t = 1_000_000_000
    for i in range(30):
        controller.update(0.4, 0.0, t + i * 50_000_000)
    held = controller.boost
    assert held == pytest.approx(d._BOOST_MAX, abs=1e-6)
    for i in range(30, 60):
        controller.update(0.0, 0.0, t + i * 50_000_000)
    assert abs(controller.boost) < held * 0.5


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


def test_debug_snapshot_reports_why_points_are_missing() -> None:
    """调试图快照区分缺席原因：车道原点不可用 vs 同拍车账缺席 vs 有点位。"""
    from maaracing_master.plugins.infinite_drive import driving as d

    driver = d.LaneDrive.__new__(d.LaneDrive)
    base = {"lane": {}, "ego_x": 0.0, "target": 0.2, "cars": None,
            "walled": False, "steer": 0.0, "throttle_scale": 1.0,
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


def test_lane_holds_nearest_edge_point_and_steers_toward_it(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d

    now = 1_100_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    rows = []
    driver._trace = SimpleNamespace(append=rows.append)
    obs = {"x": 0.6, "theta": 0.0, "usable": True}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    driver.begin()

    now += 100_000_000
    obs.update(det_fid=8, det_ts_ns=now, theta_src_fid=8, theta_src_ts_ns=now)
    assert driver.steer(None, 8, now) > 0
    assert rows[-1]["target_lane"] == 0.8          # 离 0.6 最近的擦边点
    assert rows[-1]["edge_event"] == "pick"
    assert driver.throttle_scale == 1.0


def test_blocked_point_switches_once_and_holds_after_clearing(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d
    from maaracing_master.plugins.infinite_drive.perception import PerceptionResult

    now = 1_100_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    rows = []
    driver._trace = SimpleNamespace(append=rows.append)
    obs = {"x": 0.8, "theta": 0.0, "usable": True, "y_v": 365., "cam_h": 1.25,
           "lines": _line_fits()}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    driver.begin()

    now += 100_000_000
    obs.update(det_fid=8, det_ts_ns=now, theta_src_fid=8, theta_src_ts_ns=now)
    driver.steer(None, 8, now)
    assert rows[-1]["target_lane"] == 0.8

    # 前方 10m 一辆横位 0.7 的车：堵住 0.2/0.8/1.2，换到最近的净点 −0.2。
    now += 100_000_000
    traffic = PerceptionResult(9, now, cars=[_car_x(0.7, 0.8)])
    obs.update(det_fid=9, det_ts_ns=now, theta_src_fid=9, theta_src_ts_ns=now,
               traffic=traffic)
    assert driver.steer(None, 9, now) < 0
    assert rows[-1]["target_lane"] == -0.2
    assert rows[-1]["edge_event"] == "switch"
    assert driver.throttle_scale == 1.0

    # 车还在：新点位净，保持不重估；车走了也不切回。
    now += 100_000_000
    obs.update(det_fid=10, det_ts_ns=now, theta_src_fid=10, theta_src_ts_ns=now)
    driver.steer(None, 10, now)
    assert rows[-1]["target_lane"] == -0.2
    assert rows[-1]["edge_event"] is None
    now += 100_000_000
    obs.update(det_fid=11, det_ts_ns=now, theta_src_fid=11, theta_src_ts_ns=now,
               traffic=PerceptionResult(11, now))
    driver.steer(None, 11, now)
    assert rows[-1]["target_lane"] == -0.2
    assert driver.throttle_scale == 1.0


def test_walled_road_cuts_throttle_until_any_point_clears(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d
    from maaracing_master.plugins.infinite_drive.perception import PerceptionResult

    now = 1_100_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    rows = []
    driver._trace = SimpleNamespace(append=rows.append)
    obs = {"x": 0.5, "theta": 0.0, "usable": True, "y_v": 365., "cam_h": 1.25,
           "lines": _line_fits()}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    driver.begin()

    # 四条车道各有居中车（z≈10m）：六个擦边点全堵 → 收油，无点可选。
    now += 100_000_000
    obs.update(det_fid=8, det_ts_ns=now, theta_src_fid=8, theta_src_ts_ns=now,
               traffic=PerceptionResult(8, now, cars=[_car_x(p, 0.5) for p in
                                                      (-1.5, -0.5, 0.5, 1.5)]))
    driver.steer(None, 8, now)
    assert driver.throttle_scale == 0.0
    assert rows[-1]["target_lane"] is None
    assert rows[-1]["walled"] is True

    # 车流散开：恢复油门，锁最近净点（0.5 与 0.2/0.8 等距，靠路中心优先）。
    now += 100_000_000
    obs.update(det_fid=9, det_ts_ns=now, theta_src_fid=9, theta_src_ts_ns=now,
               traffic=PerceptionResult(9, now))
    driver.steer(None, 9, now)
    assert driver.throttle_scale == 1.0
    assert rows[-1]["target_lane"] == 0.2


def test_far_car_does_not_trigger_a_switch(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d
    from maaracing_master.plugins.infinite_drive.perception import PerceptionResult

    now = 1_100_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    rows = []
    driver._trace = SimpleNamespace(append=rows.append)
    obs = {"x": 0.8, "theta": 0.0, "usable": True, "y_v": 365., "cam_h": 1.25,
           "lines": _line_fits()}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    driver.begin()

    # 45m 外同横位的车在换点视界（40m）之外：照常锁点、满油。
    now += 100_000_000
    obs.update(det_fid=8, det_ts_ns=now, theta_src_fid=8, theta_src_ts_ns=now,
               traffic=PerceptionResult(8, now, cars=[_car_x(0.8, 0.8, z_m=45.2)]))
    driver.steer(None, 8, now)
    assert rows[-1]["target_lane"] == 0.8
    assert rows[-1]["edge_event"] == "pick"
    assert driver.throttle_scale == 1.0


def test_second_round_steers_when_road_recovers_without_camera_height(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d

    now = 1_000_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
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
               x=0.55, theta=0.09, usable=True, lines=[
                   {"accepted": True, "k": 0, "side": -1},
                   {"accepted": True, "k": 1, "side": 1},
               ])
    assert driver.steer(None, 85, now) < 0.0
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
