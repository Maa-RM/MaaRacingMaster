# -*- coding: utf-8 -*-
"""开局视角守卫回归锁：正常带确认、坏视角切换、无几何切换、三视角失败、截止。"""

from __future__ import annotations

from maaracing_master.plugins.speedrush.view_guard import (
    ViewGuard, ViewGuardConfig)

CFG = ViewGuardConfig(enabled=True, sample_s=0.7, settle_s=0.25,
                      max_views=3, min_samples=2, deadline_s=2.9)


def test_correct_view_confirms_after_window():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, 1.33, True) == ViewGuard.WAIT
    assert g.update(0.50, 1.35, True) == ViewGuard.WAIT
    assert g.update(0.85, None, False) == ViewGuard.OK
    assert g.median == 1.34
    assert g.views == 1


def test_wrong_view_switches_then_confirms():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, 1.90, True) == ViewGuard.WAIT
    assert g.update(0.85, 1.92, True) == ViewGuard.SWITCH
    assert g.views == 2
    assert g.update(0.95, None, False) == ViewGuard.WAIT      # settle
    assert g.update(1.15, 1.35, True) == ViewGuard.WAIT       # 新窗口开始
    assert g.update(1.60, 1.37, True) == ViewGuard.WAIT
    assert g.update(1.90, None, False) == ViewGuard.OK


def test_no_geometry_is_bad_view():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, None, True) == ViewGuard.WAIT
    assert g.update(0.50, None, True) == ViewGuard.WAIT
    assert g.update(0.85, None, True) == ViewGuard.WAIT   # 宽限一次
    assert g.update(1.25, None, True) == ViewGuard.SWITCH
    assert g.median is None


def test_single_sample_is_not_enough():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(0.10, 1.35, True) == ViewGuard.WAIT
    assert g.update(0.85, None, False) == ViewGuard.WAIT  # 样本不足，宽限
    assert g.update(1.25, None, False) == ViewGuard.SWITCH


def test_three_bad_views_fail():
    g = ViewGuard(ViewGuardConfig(enabled=True, sample_s=0.7, settle_s=0.25,
                                  max_views=3, min_samples=2,
                                  deadline_s=10.0), t0=0.0)
    t = 0.10
    for expected_views in (2, 3):
        assert g.update(t, 1.90, True) == ViewGuard.WAIT
        assert g.update(t + 0.75, 1.90, True) == ViewGuard.SWITCH
        assert g.views == expected_views
        t += 0.80 + 0.25
    assert g.update(t, 1.90, True) == ViewGuard.WAIT
    assert g.update(t + 0.75, 1.90, True) == ViewGuard.FAIL


def test_arm_timeout_fails_without_samples():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(7.90, None, False) == ViewGuard.WAIT
    assert g.update(8.10, None, False) == ViewGuard.FAIL


def test_deadline_runs_from_first_sample():
    g = ViewGuard(CFG, t0=0.0)
    assert g.update(3.00, 1.90, True) == ViewGuard.WAIT
    assert g.update(5.85, 1.90, True) == ViewGuard.SWITCH
    assert g.update(6.10, None, False) == ViewGuard.FAIL


def test_three_samples_required_by_production_config():
    from maaracing_master.plugins.speedrush.config import load_decision
    cfg = load_decision().view_guard
    assert cfg.enabled is True
    assert cfg.min_samples == 3
    g = ViewGuard(cfg, t0=0.0)
    assert g.update(0.10, 1.33, True) == ViewGuard.WAIT
    assert g.update(0.50, 1.35, True) == ViewGuard.WAIT
    assert g.update(0.80, None, False) == ViewGuard.WAIT   # 2 个样本：宽限
    assert g.update(1.05, 1.34, True) == ViewGuard.OK      # 第 3 个样本到位


def test_press_view_button_always_releases(monkeypatch):
    import maaracing_master.core.vgamepad_lazy as vgl
    import maaracing_master.plugins.speedrush.module as smod

    class _Btn:
        XUSB_GAMEPAD_RIGHT_SHOULDER = "RB"

    class _Vg:
        XUSB_BUTTON = _Btn

    monkeypatch.setattr(vgl, "vg", _Vg())

    class Pad:
        def __init__(self):
            self.calls = []

        def press_button(self, b):
            self.calls.append(("press", b))

        def release_button(self, b):
            self.calls.append(("release", b))

        def update(self):
            self.calls.append(("update",))

    pad = Pad()
    smod.SpeedRushModule(None)._press_view_button(pad)
    kinds = [c[0] for c in pad.calls]
    assert kinds == ["press", "update", "release", "update"]
    assert pad.calls[0][1] == "RB"


# ---------- 模块层接线（2026-11-12）：判据改吃车道线反解的相机高 ----------

def _mod(monkeypatch):
    """裸模块实例 + 静音 logger（只测 _tick_view_guard 的接线）。"""
    import types
    import maaracing_master.plugins.speedrush.module as smod
    monkeypatch.setattr(smod, "logger",
                        types.SimpleNamespace(log=lambda *a, **k: None))
    return smod, smod.SpeedRushModule(None)


def test_guard_releases_when_lane_instrument_absent(monkeypatch):
    """线观测没启用 → 没有判据：放行、不切视角、不中止阶段。"""
    import time
    smod, m = _mod(monkeypatch)
    vg = ViewGuard(ViewGuardConfig(enabled=True), time.perf_counter())
    chain = {"view_guard": vg, "lane_obs": None, "abort_phase": False}
    m._tick_view_guard(chain, None, None)
    assert chain["view_guard"] is None
    assert m._view_ok is True
    assert chain["abort_phase"] is False


def test_guard_releases_when_lane_never_yields_cam_h(monkeypatch):
    """线在跑但一直给不出相机高：按「无法判定」放行，不走三视角失败。"""
    import time
    smod, m = _mod(monkeypatch)
    cfg = ViewGuardConfig(enabled=True, sample_s=0.01, min_samples=2,
                          arm_timeout_s=0.01, deadline_s=1.0)
    vg = ViewGuard(cfg, time.perf_counter() - 1.0)
    chain = {"view_guard": vg, "lane_obs": object(), "abort_phase": False}
    m._tick_view_guard(chain, None, {"fid": 1, "cam_h_lane": None})
    assert chain["view_guard"] is None
    assert m._view_ok is True
    assert chain["abort_phase"] is False


def test_guard_takes_cam_h_from_lane_and_confirms(monkeypatch):
    """判据来自车道线反解（lane.cam_h_lane），按线观测帧号去重计样本。"""
    import time
    smod, m = _mod(monkeypatch)
    cfg = ViewGuardConfig(enabled=True, sample_s=0.01, min_samples=2,
                          settle_s=0.0, arm_timeout_s=5.0, deadline_s=5.0)
    vg = ViewGuard(cfg, time.perf_counter())
    chain = {"view_guard": vg, "lane_obs": object(), "abort_phase": False}
    m._tick_view_guard(chain, None, {"fid": 7, "cam_h_lane": 1.25})
    m._tick_view_guard(chain, None, {"fid": 7, "cam_h_lane": 1.25})  # 同帧不重复计
    assert len(vg.samples) == 1 and chain["view_guard"] is vg
    time.sleep(0.02)
    m._tick_view_guard(chain, None, {"fid": 8, "cam_h_lane": 1.27})
    assert chain["view_guard"] is None and m._view_ok is True
    assert vg.median == 1.26


def test_guard_does_not_read_depth_plane_cam_h(monkeypatch):
    """合成点图的平面 c 恒等于 1.25（贴地像素被钉在 Y≡cam_h），拿它当判据
    等于读常数——模块层不得再把 dgeo 传进守卫（签名里没有这个入口）。"""
    import inspect
    smod, _m = _mod(monkeypatch)
    params = list(inspect.signature(smod.SpeedRushModule._tick_view_guard)
                  .parameters)
    assert params == ["self", "chain", "gpad", "lane"]
