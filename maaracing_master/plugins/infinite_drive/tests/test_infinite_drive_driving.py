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


def test_depth_anchor_rejects_vehicle_sized_gap_and_uses_ego_sign() -> None:
    from maaracing_master.plugins.infinite_drive.depth_geo import DepthRoadReading
    from maaracing_master.plugins.infinite_drive.driving import road_anchor

    assert road_anchor(DepthRoadReading(-2.5, 1.5, None, None, 2, 0.0)) == 0.5
    assert road_anchor(DepthRoadReading(-0.7, 0.8, None, None, 2, 0.0)) is None
    assert road_anchor(DepthRoadReading(-2.5, None, None, None, 1, 0.0)) is None


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


@pytest.mark.parametrize("camera_height, switch", [(None, True), (1.25, False), (1.9, True)])
def test_opening_view_uses_completed_detections_even_without_height(
        monkeypatch, camera_height, switch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d

    driver = d.DepthDrive.__new__(d.DepthDrive)
    driver._control = d.HeadingControl(3.5)
    driver._view_config = d.ViewGuardConfig(enabled=True)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    driver._trace = SimpleNamespace(append=lambda _: None)
    driver._depth = SimpleNamespace(take=lambda: (None, None, False), push=lambda *a, **kw: None)
    obs = {}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    now = 1_000_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver.begin()

    events = []
    for fid in range(12):
        now = 1_000_000_000 + fid * 120_000_000
        obs.update(det_fid=fid, det_ts_ns=now, cam_h_lane=camera_height)
        events.append(driver.steer(None, fid, now)[1])
    assert any(events) is switch


def test_lane_locks_from_confirmed_lines_after_depth_anchor_expires(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import driving as d

    now = 1_100_000_000
    monkeypatch.setattr(d.time, "perf_counter_ns", lambda: now)
    driver = d.DepthDrive.__new__(d.DepthDrive)
    driver._control = d.HeadingControl(3.5)
    driver._view_config = d.ViewGuardConfig(enabled=True)
    driver._camera_height = 1.25
    driver._cal = SimpleNamespace(focal_px=880)
    rows = []
    driver._trace = SimpleNamespace(append=rows.append)
    driver._depth = SimpleNamespace(take=lambda: (None, None, False), push=lambda *a, **kw: None)
    obs = {"x": 0.6, "theta": 0.09, "n_locked": 2, "usable": True,
           "cam_h_lane": 1.25, "lines": [
               {"accepted": True, "k": 0, "side": -1},
               {"accepted": True, "k": 1, "side": 1},
           ]}
    driver._lines = SimpleNamespace(latest=lambda: obs, push=lambda *a, **kw: None)
    driver.begin()

    for fid in range(1, 8):
        now += 100_000_000
        obs.update(det_fid=fid, det_ts_ns=now, theta_src_fid=fid, theta_src_ts_ns=now)
        assert driver.steer(None, fid, now) == (0.0, False)
    assert rows[-1]["target_lane"] is None
    now += 150_000_000
    obs.update(det_fid=8, det_ts_ns=now, theta_src_fid=8, theta_src_ts_ns=now)
    steer, switch = driver.steer(None, 8, now)
    assert steer < 0 and not switch
    assert rows[-1]["target_lane"] == 0.5


def test_lane_center_requires_two_adjacent_boundaries() -> None:
    from maaracing_master.plugins.infinite_drive.driving import _lane_center

    lines = [{"accepted": True, "k": -1, "side": -1},
             {"accepted": True, "k": 1, "side": 1},
             {"accepted": False, "k": 0, "side": -1}]
    assert _lane_center(0.49, lines) is None
    lines[-1]["accepted"] = True
    assert _lane_center(0.49, lines) == 0.5


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
