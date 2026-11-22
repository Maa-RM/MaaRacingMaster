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


def test_lane_drive_constructs_without_depth_model(monkeypatch) -> None:
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


def test_lane_locks_from_confirmed_lines_and_reacts_to_blocked_road(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d
    from maaracing_master.plugins.infinite_drive.perception import Detection, PerceptionResult

    now = 1_100_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.LaneDrive.__new__(d.LaneDrive)
    driver._control = d.HeadingControl(3.5)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    rows = []
    driver._trace = SimpleNamespace(append=rows.append)
    obs = {"x": 0.6, "theta": 0.09, "n_locked": 2, "usable": True,
           "cam_h_lane": 1.25, "lines": [
               {"accepted": True, "k": 0, "side": -1},
               {"accepted": True, "k": 1, "side": 1},
           ]}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    driver.begin()

    now += 100_000_000
    obs.update(det_fid=8, det_ts_ns=now, theta_src_fid=8, theta_src_ts_ns=now)
    assert driver.steer(None, 8, now) < 0
    assert rows[-1]["target_lane"] == 0.5
    assert driver.throttle_scale == 1.0

    obs.update(x=0.5, theta=0., y_v=365., lines=[
        {"accepted": True, "k": k, "side": -1 if k <= 0 else 1, "L": k - 0.5,
         "fits": [{"s": 0., "a": 640 + 100 * (k - 0.5), "y0": 380., "y1": 700., "rms": 1.}]}
        for k in range(-2, 3)])
    now += 100_000_000
    traffic = PerceptionResult(9, now, cars=[Detection(640 + 100 * k, 404, 44, 40, .9)
                                             for k in range(-2, 2)])
    obs.update(det_fid=9, det_ts_ns=now, theta_src_fid=9, theta_src_ts_ns=now, traffic=traffic)
    driver.steer(None, 9, now)
    assert driver.throttle_scale == 0.0
    assert rows[-1]["road_path"] == ((0.0, 0.5),)

    now += 100_000_000
    obs.update(det_fid=10, det_ts_ns=now, theta_src_fid=10, theta_src_ts_ns=now,
               traffic=PerceptionResult(10, now))
    driver.steer(None, 10, now)
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


def test_lane_center_requires_two_adjacent_boundaries() -> None:
    from maaracing_master.plugins.infinite_drive.driving import _lane_center

    lines = [{"accepted": True, "k": -1, "side": -1},
             {"accepted": True, "k": 1, "side": 1},
             {"accepted": False, "k": 0, "side": -1}]
    assert _lane_center(0.49, lines) is None
    lines[-1]["accepted"] = True
    assert _lane_center(0.49, lines) == 0.5


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


def test_opening_camera_waits_for_consensus_after_one_switch() -> None:
    from maaracing_master.plugins.infinite_drive.view_guard import ViewGuard, ViewGuardConfig

    guard = ViewGuard(ViewGuardConfig(empty_sample_s=2.0), 0.0)
    assert guard.update(0.1, None, True) == ViewGuard.WAIT
    assert guard.update(2.6, None, True) == ViewGuard.SWITCH
    assert guard.update(2.9, 1.095, True) == ViewGuard.WAIT
    assert guard.update(4.1, None, True) == ViewGuard.WAIT
    assert guard.update(4.2, 1.19, True) == ViewGuard.WAIT
    assert guard.update(4.3, 1.20, True) == ViewGuard.OK


def test_phase_two_checks_view_without_cycling_it() -> None:
    from maaracing_master.plugins.infinite_drive.view_guard import ViewGuard, ViewGuardConfig

    guard = ViewGuard(ViewGuardConfig(empty_sample_s=2.0), 0.0, allow_switch=False)
    assert guard.update(0.1, None, True) == ViewGuard.WAIT
    assert guard.update(2.6, None, True) == ViewGuard.WAIT
    assert guard.update(8.1, None, True) == ViewGuard.FAIL


def test_opening_view_allows_geometry_warmup_and_then_confirms_promptly() -> None:
    from maaracing_master.plugins.infinite_drive.view_guard import ViewGuard, ViewGuardConfig

    guard = ViewGuard(ViewGuardConfig(empty_sample_s=2.0), 0.0)
    assert guard.update(0.1, None, True) == ViewGuard.WAIT
    assert guard.update(1.3, None, True) == ViewGuard.WAIT
    assert guard.update(1.8, 1.25, True) == ViewGuard.WAIT
    assert guard.update(2.0, 1.20, True) == ViewGuard.WAIT
    assert guard.update(2.6, 1.25, True) == ViewGuard.OK


def test_one_high_camera_reading_does_not_block_the_opening_switch() -> None:
    from maaracing_master.plugins.infinite_drive.view_guard import ViewGuard, ViewGuardConfig

    guard = ViewGuard(ViewGuardConfig(empty_sample_s=2.0), 0.0)
    assert guard.update(0.1, None, True) == ViewGuard.WAIT
    assert guard.update(2.28, 1.706, True) == ViewGuard.WAIT
    assert guard.update(2.6, None, True) == ViewGuard.WAIT
    assert guard.update(3.0, None, True) == ViewGuard.SWITCH
    assert guard.update(3.3, 1.706, True) == ViewGuard.WAIT
    assert guard.update(6.0, None, True) == ViewGuard.WAIT


def test_early_bumper_reading_keeps_camera_still_while_waiting_for_consensus() -> None:
    from maaracing_master.plugins.infinite_drive.view_guard import ViewGuard, ViewGuardConfig

    guard = ViewGuard(ViewGuardConfig(empty_sample_s=2.0), 0.0)
    assert guard.update(0.1, None, True) == ViewGuard.WAIT
    assert guard.update(2.0, 1.25, True) == ViewGuard.WAIT
    assert guard.update(2.2, 1.706, True) == ViewGuard.WAIT
    assert guard.update(3.0, None, True) == ViewGuard.WAIT
    assert guard.update(3.2, 1.24, True) == ViewGuard.OK
