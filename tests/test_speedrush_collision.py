# -*- coding: utf-8 -*-
"""正面撞车事件回归锁：近距正面 + 仍在接近 + 观察窗内 pass/拉远否决。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.collision import CollisionObserver
from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.object_table import ObjectTable
from maaracing_master.plugins.speedrush.tracking import (
    DistanceSource, MetricCarState, PerceptionHealth, TrackedTarget,
    WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    KIND_CAR, KIND_COIN)

EGO_HALF = 0.95
CAR_HALF = 0.95
MARGIN = 0.05
TICK_NS = 50_000_000          # 20Hz


def _ts(fid: int) -> int:
    return fid * TICK_NS


def _obs_observer():
    return CollisionObserver(z_max_m=5.0, ego_half_m=EGO_HALF,
                             car_half_m=CAR_HALF, margin_m=MARGIN,
                             pass_window_s=1.5, recede_m=0.5)


def _t(tid, z, *, kind=KIND_CAR, x_m=0.5, half_w_m=0.9, src="metric",
       conf=0.9, age=0.0, v_close=10.0, rel=2.0):
    return SimpleNamespace(id=tid, kind=kind, z_m=z, x_m=x_m,
                           half_w_m=half_w_m, src=src, conf=conf,
                           age_ms=age, v_close_mps=v_close,
                           rel_approach=rel)


def _obs(fid, *, targets=(), metric=()):
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=_ts(fid),
        frame_age_ms=10.0, stage=1,
        health=PerceptionHealth(True, True, True, bool(targets), False),
        targets=tuple(targets), far_targets=(), coin_groups=(),
        metric_cars=tuple(metric), fid_rate_hz=20.0)


def _car(tid, x, *, kind=KIND_CAR, cy=500, last=1, first=1):
    return TrackedTarget(
        id=tid, kind=kind, x_lane=x, x_sigma=0.1, cy=cy, w=30, h=30,
        conf=0.9, rel_approach=2.0, first_seen_fid=first,
        last_seen_fid=last, validity_until_fid=last + 15)


def _step(obs, fid, tracks, *, pass_ids=()):
    return obs.update(tracks, fid, ts_ns=_ts(fid), pass_ids=pass_ids)


def test_pending_then_settles_once_per_track_id():
    obs = _obs_observer()
    assert _step(obs, 100, [_t(7, 3.2)]) == ()          # 进候审窗
    assert obs.pending_ids == frozenset({7})
    got = _step(obs, 130, [_t(7, 3.2)])                 # 1.5s 窗满落定
    assert len(got) == 1
    e = got[0]
    assert (e.track_id, e.fid, e.settle_fid, e.z_m) == (7, 100, 130, 3.2)
    # |x|-（自车半宽+目标半宽+余量）= 0.5 − 1.90 = −1.40：横向重叠
    assert e.x_clear_m == pytest.approx(-1.40)
    # 同一辆车继续贴近/抖动：不再记第二条
    assert _step(obs, 131, [_t(7, 1.9)]) == ()
    assert obs.fired_ids == frozenset({7})


def test_only_frontal_yolo_car_with_anchor_depth_and_approach():
    obs = _obs_observer()
    tracks = [
        _t(1, None),                       # 无纵深：像素/远场轨道
        _t(2, 6.0),                        # 超出近距带
        _t(3, -1.0),                       # 已过车头=掠过
        _t(4, 2.0, kind=KIND_COIN),        # 非街车
        _t(5, 0.0),                        # 边界：车头正贴
        _t(6, 2.0, src="metric_coarse"),   # 框高粗距：近场不用
        _t(7, 2.0, x_m=None),              # 无米制横位：证明不了在正面
        _t(8, 2.0, x_m=2.5),               # 横向包络不重叠：侧边车
        _t(9, 2.0, v_close=-1.0, rel=-1.0),  # 在拉远：不记
    ]
    _step(obs, 10, tracks)
    got = _step(obs, 40, tracks)            # 窗满：只有 5 落定
    assert [e.track_id for e in got] == [5]


def test_measured_half_width_widens_frontal_envelope():
    obs = _obs_observer()
    # 大车实测半宽 2.0m：横向包络 0.95+2.0+0.05=3.0m，x=2.2 也算正面
    _step(obs, 3, [_t(1, 4.0, x_m=2.2, half_w_m=2.0)])
    got = _step(obs, 33, [_t(1, 4.0, x_m=2.2, half_w_m=2.0)])
    assert [e.track_id for e in got] == [1]
    assert got[0].x_clear_m == pytest.approx(2.2 - 3.0)


def test_pass_inside_window_vetoes_candidate():
    obs = _obs_observer()
    assert _step(obs, 1, [_t(1, 2.0)]) == ()
    # 同 track 在窗内产生 pass（超车完成）：擦过，不是撞车
    assert _step(obs, 5, [_t(1, 2.0)], pass_ids={1}) == ()
    assert _step(obs, 40, [_t(1, 2.0)]) == ()
    assert obs.fired_ids == frozenset({1})   # 已触发过，但不落成事件


def test_receding_depth_vetoes_candidate():
    obs = _obs_observer()
    assert _step(obs, 1, [_t(1, 2.0)]) == ()
    # 纵深回升 > recede_m：在拉远，不记
    assert _step(obs, 5, [_t(1, 3.0)]) == ()
    assert _step(obs, 40, [_t(1, 3.0)]) == ()


def test_track_disappearing_settles_immediately():
    obs = _obs_observer()
    assert _step(obs, 1, [_t(1, 2.0)]) == ()
    got = _step(obs, 3, [])                  # 消失且无 pass：立即落定
    assert [(e.track_id, e.settle_fid) for e in got] == [(1, 3)]


def test_rel_approach_fallback_when_no_closing_speed():
    obs = _obs_observer()
    assert _step(obs, 1, [_t(1, 2.5, v_close=None, rel=2.0)]) == ()
    got = _step(obs, 31, [_t(1, 2.5, v_close=None, rel=2.0)])
    assert [e.track_id for e in got] == [1]


def test_reset_clears_dedupe_for_new_stage():
    obs = _obs_observer()
    _step(obs, 1, [_t(1, 2.5)])
    obs.reset()
    assert obs.pending_ids == frozenset()
    _step(obs, 2, [_t(1, 2.5)])
    assert obs.pending_ids == frozenset({1})


def test_object_table_anchor_car_triggers_and_coarse_does_not():
    table = ObjectTable(safety_age_s=0.6, plan_age_s=2.0)
    m = MetricCarState(target_id=1, source=DistanceSource.ANCHOR, z_m=3.0,
                       x_m=0.4, v_close_mps=10.0, t_meet_s=0.3,
                       anchor_age_ms=20.0)
    table.step(obs=_obs(1, targets=(_car(1, 0.1),), metric=(m,)),
               metric_cars=(m,), fps=20.0)
    obs = _obs_observer()
    _step(obs, 1, table.tracks)
    got = _step(obs, 31, table.tracks)
    assert len(got) == 1
    assert got[0].track_id == 1 and got[0].src == "metric"
    # 框高粗距（metric_coarse）：近场不可靠，不进事件
    m2 = MetricCarState(target_id=2, source=DistanceSource.BOX_SIZE, z_m=4.2,
                        x_m=0.3, v_close_mps=None, t_meet_s=None,
                        anchor_age_ms=None)
    table.step(obs=_obs(2, targets=(_car(1, 0.1, last=1),
                                    _car(2, 0.2, first=2, last=2)),
                        metric=(m2,)), metric_cars=(m2,), fps=20.0)
    assert _step(obs, 32, table.tracks) == ()


def test_config_section_is_wired():
    cfg = _read_decision(DECISION_FILE)
    assert cfg.collision.enabled is True
    assert cfg.collision.z_max_m == pytest.approx(5.0)
    assert cfg.collision.pass_window_s == pytest.approx(1.5)
    assert cfg.collision.recede_m == pytest.approx(0.5)
