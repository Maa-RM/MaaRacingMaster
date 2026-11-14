# -*- coding: utf-8 -*-
"""四层选择层回归锁（2026-10-18 走廊重建）。

锁定：奖励车>擦边>金币>中心带的严格字典序、中心带不追正中、路点顺路
插入与可行性、层切换即时/同层驻留。
"""
from __future__ import annotations

import pytest

from maaracing_master.plugins.speedrush.selection import (
    LayeredSelector, Obj, RoadBand, RoadFrame, SelectionParams, select)

LW = 3.38


def _road(center=0.0, width_lanes=4.0, *, curve=0.0):
    half = width_lanes * LW / 2.0
    bands = tuple(
        RoadBand(z0, z0 + 3.0, center + curve * z0 - half,
                 center + curve * z0 + half)
        for z0 in range(-2, 25, 3))
    return RoadFrame(bands, width_lanes * LW, center - half,
                     center + half, True)


def _p(**kw):
    return SelectionParams(lane_w_m=LW, **kw)


def test_reward_is_layer_one():
    rep = select(_road(), [Obj("reward", 2.0, 2.0)],
                 [], [Obj("coin", 0.0, 1.0)], 0.0, _p())
    assert rep.layer == 1 and rep.why == "reward"
    assert rep.target_x_m == pytest.approx(2.0)
    assert rep.maneuver is True


def test_reward_infeasible_falls_to_coin():
    """2 道外、0.1s 到站：来不及，降到金币层（不是硬追奖励车）。"""
    rep = select(_road(), [Obj("reward", 6.0, 0.1)],
                 [], [Obj("coin", 0.3, 2.0)], 0.0, _p())
    assert rep.layer == 3 and rep.why == "coin"
    assert rep.target_x_m == pytest.approx(0.0)    # 币心在车身带内：不动线


def test_pass_picks_roomier_side():
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, _p())
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)


def test_blocking_car_without_t_enters_pass():
    """挡道即候选（2026-11-07）：同道纵近车算不出到站时间也进擦边层。"""
    rep = select(_road(), [], [Obj("car", 0.0, None, near=True)], [], 0.0, _p())
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)


def test_imminent_same_lane_car_still_dodges():
    """同道临门不弃权（2026-11-09，闸 pass_no_surrender_enabled）：0.5s 到站
    的同道车走不满 0.68 道，开闸时仍给同侧躲避目标，由执行层按同一条律走到
    能到的位置。"""
    rep = select(_road(), [], [Obj("car", 0.5 * LW, 0.5)], [], 0.0,
                 _p(pass_no_surrender_enabled=True))
    assert rep.layer == 2 and rep.why == "pass"
    # 目标在车的另一侧、离车心 0.68 道：不是回中，也不是往车里挤
    assert rep.target_x_m < 0.0 < 0.5 * LW
    assert abs(rep.target_x_m - 0.5 * LW) == pytest.approx(0.68 * LW, abs=0.05)


def test_no_surrender_gate_off_returns_center():
    """闸关（默认，隔离 A/B 基线）：同道全量不可达弃候选→回中。"""
    rep = select(_road(), [], [Obj("car", 0.5 * LW, 0.5)], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_imminent_adjacent_car_still_requires_full_lean():
    """邻道贴边（得分动作）保留全量可达门：临时起意贴车=撞车。"""
    rep = select(_road(), [], [Obj("car", 1.0 * LW, 0.5)], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_far_car_without_t_stays_center():
    """远车 t=None 不放行——它在接近过程中会攒出到站时间。"""
    rep = select(_road(), [], [Obj("car", 0.0, None, near=False)],
                 [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_adjacent_near_car_without_t_not_candidate():
    """挡道语义只覆盖同道：邻道车 t=None 仍不进候选。"""
    rep = select(_road(), [], [Obj("car", 0.7 * LW, None, near=True)],
                 [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_both_sides_blocked_holds_center():
    cars = [Obj("car", 0.0, 2.0),
            Obj("car", 0.65 * LW, 2.0),
            Obj("car", -0.65 * LW, 2.0)]
    rep = select(_road(), [], cars, [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_layer_and_center_fallback():
    rep = select(_road(), [], [], [Obj("coin", 0.35, 2.5)], 0.0, _p())
    assert rep.layer == 3 and rep.target_x_m == pytest.approx(0.0)


def test_coin_not_centered_car_has_width():
    """币不必正中：车身带内的币保持原线，带外的只走到带边。"""
    p = _p()
    band = p.ego_half_lane * LW
    # 带内 0.2 道：目标 = 自车横位
    rep = select(_road(), [], [], [Obj("coin", 0.2 * LW, 2.0)], 0.0, p)
    assert rep.layer == 3 and rep.target_x_m == pytest.approx(0.0)
    # 带外 0.8 道：目标 = 币心 − 车身半宽（不是币心）
    rep = select(_road(), [], [], [Obj("coin", 0.8 * LW, 2.0)], 0.0, p)
    assert rep.layer == 3
    assert rep.target_x_m == pytest.approx(0.8 * LW - band)
    rep = select(_road(), [], [], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_off_line_not_selected():
    """无承诺道时只做本道/相邻一道：1.5 道外的散币不直接拽。"""
    rep = select(_road(), [], [], [Obj("coin", 1.5 * LW, 3.0)], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_coin_adjacent_lane_selected():
    """相邻道（0.9 道）的币可以成为主目标；目标按车身带收，不对币心。"""
    p = _p()
    rep = select(_road(), [], [], [Obj("coin", 0.9 * LW, 2.5)], 0.0, p)
    assert rep.layer == 3 and rep.why == "coin"
    assert rep.target_x_m == pytest.approx(0.9 * LW - p.ego_half_lane * LW)


def test_aligned_coin_needs_no_move_time():
    """已经对齐的币只留反应时间：0.2s 到站也接（旧规则要 0.58s）。"""
    rep = select(_road(), [], [], [Obj("coin", 0.05 * LW, 0.2)], 0.0, _p())
    assert rep.layer == 3 and rep.why == "coin"


def test_aligned_reward_needs_no_move_time():
    """正前方已对齐的奖励车：到站 0.2s 仍是层 1（旧规则被可达门丢掉）。"""
    rep = select(_road(), [Obj("reward", 0.1 * LW, 0.2)], [], [], 0.0, _p())
    assert rep.layer == 1 and rep.why == "reward"


def test_center_band_no_correction_inside():
    p = _p()
    half = (0.5 - p.ego_half_lane - p.lane_clearance_lane) * LW
    inside = 0.8 * half
    rep = select(_road(), [], [], [], inside, p)
    assert rep.target_x_m == pytest.approx(inside)   # 区间内不修正
    assert rep.maneuver is False
    outside = 1.2 * LW
    rep = select(_road(), [], [], [], outside, p)
    assert rep.target_x_m == pytest.approx(half, abs=0.05)  # 只去最近边界
    assert rep.target_x_m != pytest.approx(0.0, abs=0.05)
    assert rep.maneuver is True


def test_center_anchor_uses_current_station():
    """中心锚点=当前站中心：弯道 8m 外的中心不参与（避免带边左偏）。"""
    rep = select(_road(curve=0.1), [], [], [], 0.0, _p())
    assert rep.layer == 4
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_coarse_lane_takes_center_layer_only():
    """粗层方向只接管 L4：无机会时目标=选定车道的近侧带边（道号相对
    当前中心线），不再原地贴回中心线。机会层不受影响。"""
    half = (0.5 - _p().ego_half_lane - _p().lane_clearance_lane) * LW
    rep = select(_road(), [], [], [], 0.0, _p(), coarse_lane=-1.0)
    assert rep.layer == 4 and rep.why == "center:coarse"
    assert rep.target_x_m == pytest.approx(-LW + half, abs=0.05)
    assert rep.maneuver is True
    rep = select(_road(), [Obj("reward", 2.0, 2.0)], [], [], 0.0, _p(),
                 coarse_lane=-1.0)
    assert rep.layer == 1 and rep.why == "reward"


def test_coarse_lane_zero_keeps_center_band():
    """粗层选中心道（lane=0）不改回中语义：区间内不修正。"""
    x = 0.15 * LW
    rep = select(_road(), [], [], [], x, _p(), coarse_lane=0.0)
    assert rep.why == "center"
    assert rep.target_x_m == pytest.approx(x)
    assert rep.maneuver is False


def test_preposition_biases_toward_future_coin():
    """视界外 1.2 道的币：无即时目标时锚点朝币侧预置，最多 1 道。"""
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 5.0)], 0.0, _p())
    assert rep.layer == 4
    assert rep.target_x_m == pytest.approx(1.0 * LW, abs=0.05)


def test_preposition_ignores_near_and_far_sides():
    # 视界内但来不及的币不参与预置（仍守中心）
    rep = select(_road(), [], [], [Obj("coin", 2.0 * LW, 2.5)], 0.0, _p())
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)
    # 2.5 道外的币不参与预置
    rep = select(_road(), [], [], [Obj("coin", 3.0 * LW, 5.0)], 0.0, _p())
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_preposition_prefers_reward_over_coin():
    rep = select(_road(), [Obj("reward", 0.5 * LW, 5.0)],
                 [], [Obj("coin", -1.0 * LW, 4.0)], 0.0, _p())
    assert rep.target_x_m == pytest.approx(0.5 * LW, abs=0.05)


def test_preposition_disabled_holds_center():
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 5.0)], 0.0,
                 _p(preposition_enabled=False))
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


# ---------- 擦边预判式（2026-11-02，闸 pass_preposition_enabled） ----------

def _pp():
    return _p(pass_preposition_enabled=True)


def test_pass_preposition_biases_away_from_adjacent_car():
    """0141 复刻：右邻道带来车（1.0 道、1.5s 到站）→ 向远车侧（左）安全
    区间边界起手，why=center:pp。"""
    rep = select(_road(), [], [Obj("car", 1.0 * LW, 1.5)], [], 0.0, _pp())
    assert rep.layer == 4 and rep.why == "center:pp"
    assert rep.target_x_m == pytest.approx(-0.17 * LW, abs=0.02)


def test_pass_preposition_gates_window_side_and_safe():
    """窗外车/t=None/unsafe 车/两侧都有车：不偏；同道贴身仍归 pass 层。"""
    far = select(_road(), [], [Obj("car", 1.0 * LW, 3.5)], [], 0.0, _pp())
    assert far.why == "center"                      # t > horizon
    no_t = select(_road(), [], [Obj("car", 1.0 * LW, None)], [], 0.0, _pp())
    assert no_t.why == "center"                     # 仪器缺位不触发
    unsafe = select(_road(), [],
                    [Obj("car", 1.0 * LW, 1.5, safe=False)], [], 0.0, _pp())
    assert unsafe.why == "center"                   # 不可安全不触发
    both = select(_road(), [], [Obj("car", 1.0 * LW, 1.5),
                                Obj("car", -1.0 * LW, 1.5)], [], 0.0, _pp())
    assert both.why == "center"                     # 无「更宽一侧」
    near = select(_road(), [], [Obj("car", 0.3 * LW, 1.5)], [], 0.0, _pp())
    assert near.layer == 2                          # 同道贴身仍归 pass 层


def test_pass_preposition_gate_off_and_committed():
    """闸关=逐位回今天；并线承诺期无偏置。"""
    rep = select(_road(), [], [Obj("car", 1.0 * LW, 1.5)], [], 0.0, _p())
    assert rep.why == "center"                      # 闸关
    rep = select(_road(), [], [Obj("car", 1.0 * LW, 1.5)], [], 0.0, _pp(),
                 committed=True)
    assert rep.why == "commit"                      # committed 关


# ---------- 预排视界延长（6→10s，2026-11-02） ----------

def test_preposition_extends_to_long_horizon_coin():
    """6~10s 带内的币也参与预置（视界延长后）：币组首检 ~8s 到站不再漏预排。"""
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 8.0)], 0.0,
                 _p(preposition_horizon_s=10.0))
    assert rep.layer == 4
    assert rep.target_x_m == pytest.approx(1.0 * LW, abs=0.05)


def test_preposition_beyond_extended_horizon_excluded():
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 10.5)], 0.0,
                 _p(preposition_horizon_s=10.0))
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_preposition_far_side_gate_holds_in_extended_band():
    """横距门在延长视界内照旧生效：2.5 道外的币不预置。"""
    rep = select(_road(), [], [], [Obj("coin", 3.0 * LW, 8.0)], 0.0,
                 _p(preposition_horizon_s=10.0))
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)


def test_waypoints_include_pass_and_coin():
    """奖励车在 5s 外，途中有同车道车和顺路币：先擦边、再收币、最后奖励车。"""
    rewards = [Obj("reward", 2.0, 5.0)]
    cars = [Obj("car", 0.0, 2.0)]
    coins = [Obj("coin", 1.8, 3.5)]
    rep = select(_road(), rewards, cars, coins, 0.0, _p(horizon_s=5.0))
    assert rep.layer == 1
    assert len(rep.waypoints) <= 4
    srcs = [w.src for w in rep.waypoints]
    assert srcs[-1] == "reward"
    assert "pass" in srcs
    assert "coin" in srcs
    ts = [w.t for w in rep.waypoints]
    assert ts == sorted(ts)


def test_waypoint_cap_and_monotone_coins():
    """反向金币不绕路；路点最多 4 个。"""
    rewards = [Obj("reward", 2.0, 3.0)]
    coins = [Obj("coin", -2.0, 1.0), Obj("coin", 1.0, 1.5),
             Obj("coin", 1.5, 2.0), Obj("coin", 1.8, 2.5)]
    rep = select(_road(), rewards, [], coins, 0.0, _p())
    assert len(rep.waypoints) <= 4
    assert all(w.x >= -0.2 for w in rep.waypoints)   # 反向币被丢
    assert rep.waypoints[-1].src == "reward"


def test_layer_change_immediate_same_layer_dwell():
    sel = LayeredSelector(_p())
    sel.update(_road(), [], [], [Obj("coin", -2.0, 2.0)], 0.0, 0.05)
    # 同层目标跳 >0.5 道：驻留未到，目标保持（币按车身带收，目标=币心∓带）
    rep = sel.update(_road(), [], [], [Obj("coin", 2.0, 2.0)], 0.0, 0.05)
    assert rep.target_x_m == pytest.approx(-(2.0 - _p().ego_half_lane * LW),
                                           abs=0.05)
    for _ in range(5):
        rep = sel.update(_road(), [], [], [Obj("coin", 2.0, 2.0)], 0.0, 0.05)
    assert rep.target_x_m == pytest.approx(2.0 - _p().ego_half_lane * LW,
                                           abs=0.05)
    # 层变化：奖励车出现立即切
    rep = sel.update(_road(), [Obj("reward", 1.0, 2.0)], [],
                     [Obj("coin", 1.0, 2.0)], 0.0, 0.05)
    assert rep.layer == 1 and rep.target_x_m == pytest.approx(1.0)


def test_coin_hold_then_release_and_override():
    """币组消失后沿最后币线再走 0.6s；奖励车出现立即覆盖。"""
    sel = LayeredSelector(_p())
    sel.update(_road(), [], [], [Obj("coin", 0.3, 2.0)], 0.0, 0.05)
    rep = sel.update(_road(), [], [], [], 0.0, 0.1)
    assert rep.layer == 3 and rep.why == "coin_hold"
    assert rep.target_x_m == pytest.approx(0.0, abs=0.05)   # 币在车身带内

    for _ in range(7):
        rep = sel.update(_road(), [], [], [], 0.0, 0.1)
    assert rep.layer == 4 and rep.why == "center"
    # 再选一次币，然后奖励车覆盖
    sel.update(_road(), [], [], [Obj("coin", 0.3, 2.0)], 0.0, 0.05)
    sel.update(_road(), [], [], [], 0.0, 0.1)
    rep = sel.update(_road(), [Obj("reward", 0.8, 2.0)], [], [], 0.0, 0.05)
    assert rep.layer == 1 and rep.target_x_m == pytest.approx(0.8)


def test_pass_latch_off_by_default():
    """闸关：pass 选中后下一拍下层接管，不续（旧行为）。"""
    sel = LayeredSelector(_p())
    rep = sel.update(_road(), [], [Obj("car", 0.0, 2.0, oid=7)], [], 0.0, 0.05)
    assert rep.why == "pass"
    rep = sel.update(_road(), [], [], [], 0.0, 0.1)
    assert rep.layer == 4


def test_pass_latch_holds_through_lower_layer():
    """闸开：pass 选中后，车读数丢失/下层接管时按车 id 续住，宽限后释放。"""
    sel = LayeredSelector(_p(pass_latch_enabled=True))
    rep = sel.update(_road(), [], [Obj("car", 0.0, 2.0, oid=7)], [], 0.0, 0.05)
    assert rep.why == "pass"
    x0 = rep.target_x_m
    # 车表还在但已出候选窗（t=10），下层本会回中；保持闩续住原线
    for _ in range(3):
        rep = sel.update(_road(), [], [Obj("car", 0.0, 10.0, oid=7)], [],
                         0.0, 0.1)
        assert rep.why == "pass" and rep.layer == 2
        assert rep.target_x_m == pytest.approx(x0, abs=0.05)
    # 车读数完全丢失：1s 宽限内仍续，超时回中
    for _ in range(8):
        rep = sel.update(_road(), [], [], [], 0.0, 0.1)
        assert rep.why == "pass"
    for _ in range(4):
        rep = sel.update(_road(), [], [], [], 0.0, 0.1)
    assert rep.layer == 4


def test_pass_latch_keeps_original_side():
    """pass 保持原选侧：车横位变化后不换到空间更大的一侧。"""
    p = _p(pass_latch_enabled=True)
    sel = LayeredSelector(p)
    rep = sel.update(_road(), [], [Obj("car", 0.3, 2.0, oid=7)], [],
                     0.0, 0.05)
    assert rep.why == "pass"
    assert rep.target_x_m < 0.3            # 初始选左（左侧空间更大）
    car = Obj("car", -0.3, 10.0, oid=7)    # 车移到左边，右侧反而更空
    rep = sel.update(_road(), [], [car], [], 0.0, 0.1)
    assert rep.why == "pass"
    assert rep.target_x_m < car.x          # 仍保持左侧，不横穿目标车


def test_pass_latch_releases_on_commit_and_reward():
    """承诺期不续 pass（并线目标由路线定）；奖励车更高层立即接管。"""
    sel = LayeredSelector(_p(pass_latch_enabled=True))
    sel.update(_road(), [], [Obj("car", 0.0, 2.0, oid=7)], [], 0.0, 0.05)
    rep = sel.update(_road(), [], [Obj("car", 0.0, 2.0, oid=7)], [],
                     0.0, 0.05, coarse_lane=0.0, committed=True)
    assert rep.why == "commit" and rep.layer == 4
    sel.update(_road(), [], [Obj("car", 0.0, 2.0, oid=7)], [], 0.0, 0.05)
    rep = sel.update(_road(), [Obj("reward", 1.0, 2.0)], [],
                     [Obj("car", 0.0, 2.0, oid=7)], 0.0, 0.05)
    assert rep.layer == 1


def test_side_pin_default_off():
    """闸关：目标车横位抖过中线，原生会按「空间更大的一侧」翻到对面。"""
    sel = LayeredSelector(_p())
    for _ in range(3):
        rep = sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7)],
                         [], 0.0, 0.05)
    assert rep.target_x_m < 0.2
    for _ in range(8):
        rep = sel.update(_road(), [], [Obj("car", -0.2, 2.0, oid=7)],
                         [], 0.0, 0.05)
    assert rep.target_x_m > -0.2           # 翻到右侧（旧行为）


def test_side_pin_keeps_side_across_jitter():
    """闸开：同车贴边期内锁侧，横位抖动不再把目标翻到车另一侧。"""
    sel = LayeredSelector(_p(pass_side_pin_enabled=True))
    for _ in range(3):
        rep = sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7)],
                         [], 0.0, 0.05)
    assert rep.target_x_m < 0.2            # 初始锁左侧
    for _ in range(8):
        rep = sel.update(_road(), [], [Obj("car", -0.2, 2.0, oid=7)],
                         [], 0.0, 0.05)
    assert rep.why == "pass"
    assert rep.target_x_m < -0.2           # 仍锁左侧，不横穿目标车


def test_side_pin_yields_when_locked_side_blocked():
    """锁定侧被别车占住：让位给原生换侧。"""
    sel = LayeredSelector(_p(pass_side_pin_enabled=True))
    for _ in range(3):
        rep = sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7)],
                         [], 0.0, 0.05)
    assert rep.target_x_m < 0.2
    blocker = Obj("car", -2.1, 2.0, oid=8)   # 压住锁定侧通过位
    for _ in range(10):
        rep = sel.update(_road(), [], [Obj("car", -0.2, 2.0, oid=7),
                                       blocker], [], 0.0, 0.05)
    assert rep.target_x_m > -0.2           # 锁定侧不可用：允许换到另一侧


def test_side_pin_escape_when_car_imminent():
    """车 1s 内到站且锁定侧更远：走短侧逃生，不强行绕到锁定侧；
    远车（≥1s）不给同一个理由换边。"""
    from maaracing_master.plugins.speedrush.selection import SelectionReport
    sel = LayeredSelector(_p(pass_side_pin_enabled=True))
    sel._side_oid = 7
    sel._hold_side = -1.0                  # 已锁左侧
    ego = 0.0
    raw = SelectionReport(2, (), 1.2, "pass", True, True, hit_oid=7)
    near = [Obj("car", -0.5, 0.4, oid=7)]  # 锁定侧目标 −2.80，比原生远
    out = sel._pin_pass_side(raw, _road(), near, [], ego, sel.p)
    assert out.target_x_m == pytest.approx(1.2)     # 逃生走短侧
    far = [Obj("car", -0.5, 2.0, oid=7)]   # 远车：仍锁左侧
    out = sel._pin_pass_side(raw, _road(), far, [], ego, sel.p)
    assert out.target_x_m < -0.5


def test_target_hold_keeps_line_on_lateral_jump():
    """闸开：命中车横位跳 >0.5 道时沿用上一有效贴边目标，不追坏读数。"""
    sel = LayeredSelector(_p(pass_target_hold_enabled=True))
    for _ in range(3):
        rep = sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7, z=20.0)],
                         [], 0.0, 0.05)
    held = rep.target_x_m
    assert rep.why == "pass"
    # 相邻拍跳 1.9 m（>0.5 道 = 1.69 m，仍在邻道贴边可达带内）：目标保持
    rep = sel.update(_road(), [], [Obj("car", -1.7, 2.0, oid=7, z=20.0)],
                     [], 0.0, 0.05)
    assert rep.why == "pass"
    assert rep.target_x_m == pytest.approx(held)


def test_target_hold_default_off_follows_jump():
    """闸关：同一组读数下目标跟着车走（逐位回今天）。"""
    sel = LayeredSelector(_p())
    for _ in range(3):
        rep = sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7, z=20.0)],
                         [], 0.0, 0.05)
    held = rep.target_x_m
    rep = sel.update(_road(), [], [Obj("car", -1.7, 2.0, oid=7, z=20.0)],
                     [], 0.0, 0.05)
    assert abs(rep.target_x_m - held) > 0.5 * LW


def test_target_hold_keeps_line_when_depth_lost():
    """纵深断供（z→None）按不可信处理：保持上一有效目标。"""
    sel = LayeredSelector(_p(pass_target_hold_enabled=True))
    for _ in range(3):
        rep = sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7, z=20.0)],
                         [], 0.0, 0.05)
    held = rep.target_x_m
    rep = sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7, z=None)],
                     [], 0.0, 0.05)
    assert rep.target_x_m == pytest.approx(held)


def test_target_hold_released_after_long_absence():
    """命中车缺席超过 miss_ticks 拍：清保持值，重新出现按新读数走。"""
    sel = LayeredSelector(_p(pass_target_hold_enabled=True))
    for _ in range(3):
        sel.update(_road(), [], [Obj("car", 0.2, 2.0, oid=7, z=20.0)],
                   [], 0.0, 0.05)
    for _ in range(6):
        sel.update(_road(), [], [], [], 0.0, 0.05)
    rep = sel.update(_road(), [], [Obj("car", -1.7, 2.0, oid=7, z=20.0)],
                     [], 0.0, 0.05)
    assert rep.why == "pass"
    assert rep.target_x_m > -1.7          # 新读数生效（压在车右侧）


def test_committed_ignores_opportunities_and_preposition():
    """并线承诺期：奖励车/擦边/金币/预置都不许改道，只按承诺道回中。"""
    rep = select(_road(), [Obj("reward", 2.0, 2.0)],
                 [Obj("car", 0.0, 2.0)],
                 [Obj("coin", 1.2 * LW, 2.0)], 0.0, _p(),
                 coarse_lane=1.0, committed=True)
    half = (0.5 - _p().ego_half_lane - _p().lane_clearance_lane) * LW
    assert rep.layer == 4 and rep.why == "commit"
    assert rep.target_x_m == pytest.approx(LW - half, abs=0.05)
    assert [w.src for w in rep.waypoints] == ["commit"]


def test_committed_selector_bypasses_dwell():
    """承诺期换目标立即生效：同层驻留不能拖住并线目标。"""
    sel = LayeredSelector(_p())
    sel.update(_road(), [], [], [], 0.0, 0.05,
               coarse_lane=-1.0, committed=True)
    rep = sel.update(_road(), [], [], [], 0.0, 0.05,
                     coarse_lane=1.0, committed=True)
    half = (0.5 - _p().ego_half_lane - _p().lane_clearance_lane) * LW
    assert rep.why == "commit"
    assert rep.target_x_m == pytest.approx(LW - half, abs=0.05)


def test_no_road_holds():
    road = RoadFrame((), None, None, None, False)
    rep = select(road, [], [], [], 0.5, _p())
    assert rep.layer == 0 and rep.why == "hold:no_road"
    assert rep.target_x_m == pytest.approx(0.5)


# ---------- 跨线唯一入口（2026-10-27 请求生成契约） ----------

HALF = (0.5 - 0.27 - 0.06) * LW    # 承诺道内可活动半径（车心安全区间）


def test_out_of_lane_coin_becomes_request_not_drag():
    """邻道币组：目标回夹本道区间边界，越线意图变请求——车不停在两条道中间。"""
    rep = select(_road(), [], [], [Obj("coin", 1.0 * LW, 2.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.why == "center"
    assert rep.target_x_m == pytest.approx(HALF, abs=0.02)
    assert rep.lane_request == 1.0


def test_in_lane_coin_keeps_line():
    """车身带内的币保持原线：不为一枚偏 0.1 道的币拽方向盘。"""
    rep = select(_road(), [], [], [Obj("coin", 0.1 * LW, 2.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 3 and rep.lane_request is None
    assert rep.target_x_m == pytest.approx(0.0)


def test_two_lane_coin_requests_when_reachable():
    """请求口径 ±2（2026-11-02）：隔一条道的币时间够（光锥可达）→ 发请求，
    判据是光锥不是道距；三道外仍不请求。"""
    rep = select(_road(), [], [], [Obj("coin", 2.0 * LW, 3.0)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request == 2.0
    rep = select(_road(), [], [], [Obj("coin", 3.0 * LW, 3.0)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request is None


def test_out_of_lane_reward_becomes_request():
    rep = select(_road(), [Obj("reward", 1.0 * LW, 2.0)], [], [], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.lane_request == 1.0
    assert rep.target_x_m == pytest.approx(0.0, abs=HALF + 0.02)


def test_same_lane_coin_aims_band_edge():
    """本道内偏一侧的币（0.4 道）：只走到车身带边，不对币心；
    收集判据（0.35 道）比车身带（0.27 道）宽，压在带边仍能吃到。"""
    rep = select(_road(), [], [], [Obj("coin", 0.4 * LW, 2.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 3 and rep.lane_request is None
    assert rep.target_x_m == pytest.approx((0.4 - _p().ego_half_lane) * LW)


def test_pass_keeps_full_graze_authority():
    """擦边/让位**不进门**：正前方 0 道上的车要让到 0.65 道才出得了车半宽
    （0.27）——夹进安全区间（0.17）等于迎面撞上去（用户红线：抵消微调＝撞车）。"""
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)
    assert rep.lane_request is None


def test_pending_request_does_not_preempt_yielding():
    """出跨度的机会**不占目标位**：邻道奖励车/邻道币组与一辆正压自车的车同拍
    出现时，本拍仍让位（±0.65 道），请求延后一拍再说。

    复核打出的真 bug 形态：字典序里让「本拍追不到的机会」占住目标位，等于
    用一条邻道奖励车取消掉让位动作——最长持续整个起意计分窗（6 拍 0.3 s）。"""
    rw = [Obj("reward", 1.0 * LW, 2.0)]
    coins = [Obj("coin", 1.0 * LW, 2.5)]
    cars = [Obj("car", 0.0, 2.0)]
    for opp_kw in ({"rewards": rw, "coins": []}, {"rewards": [], "coins": coins}):
        rep = select(_road(), opp_kw.get("rewards", []), cars,
                     opp_kw.get("coins", []), 0.0, _p(), commit_x_m=0.0)
        assert rep.layer == 2 and rep.why == "pass", opp_kw
        assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)
        assert rep.lane_request is None


def test_request_resumes_after_yielding():
    """让位拍过去后机会仍在 → 请求照常发出（计分只是漏一拍，不清零）。"""
    rw = [Obj("reward", 1.0 * LW, 2.0)]
    rep = select(_road(), rw, [], [], 0.0, _p(), commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request == 1.0


def test_preposition_bias_stays_in_interval():
    """预置偏移同样不越线：未来机会在邻道 → 请求 + 偏移回夹区间。"""
    rep = select(_road(), [], [], [Obj("coin", 1.2 * LW, 5.0)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.lane_request == 1.0
    assert rep.target_x_m == pytest.approx(HALF, abs=0.02)


def test_committed_emits_no_request():
    """并线期不发新请求：横向意图由承诺态持有。"""
    rep = select(_road(), [Obj("reward", 2.0 * LW, 2.0)], [], [], 0.0, _p(),
                 committed=True, commit_x_m=0.0)
    assert rep.why == "commit" and rep.lane_request is None


def test_reach_slack_is_the_same_gate_in_seconds():
    """观测面与判据同源：slack≥0 必须逐点等价于原可达门，负值=本拍来不及。"""
    from maaracing_master.plugins.speedrush.selection import (
        _reachable, reach_slack)
    p = _p()
    for dx_lane in (-2.0, -1.0, -0.4, -0.1, 0.0, 0.1, 0.4, 1.0, 2.0):
        for t in (0.0, 0.2, 0.5, 1.0, 1.5, 2.0, 3.0):
            x = dx_lane * LW
            s = reach_slack(x, 0.0, t, p)
            assert (s is not None and s >= 0.0) == _reachable(x, 0.0, t, p)
    assert reach_slack(0.0, 0.0, None, p) is None
    assert reach_slack(0.0, 0.0, 0.1, p) == pytest.approx(0.1 - p.reaction_s)


def test_unsafe_car_is_ignored_by_safety_consumers():
    """对象表判为不安全的车（超龄/退役/无横位）不参与擦边、让位、占用
    与路点插入；同一辆车新鲜时允许改变结果（1b 注入锁）。"""
    from maaracing_master.plugins.speedrush.selection import _blocked
    p = _p()
    assert _blocked([Obj("car", 0.0, 1.0)], 0.0, 1.0, p) is True
    assert _blocked([Obj("car", 0.0, 1.0, safe=False)], 0.0, 1.0, p) is False
    # 擦边层：不安全车不再触发让位
    rep = select(_road(), [], [Obj("car", 0.0, 2.0, safe=False)], [], 0.0, p)
    assert rep.layer == 4 and rep.why == "center"
    # 路点：顺路的擦边点只在车安全时插入
    from maaracing_master.plugins.speedrush.selection import _waypoints
    wp_safe = _waypoints(1, 1.5, 4.0, [Obj("car", 0.65, 2.0)], [],
                         _road(), 0.0, p)
    wp_unsafe = _waypoints(1, 1.5, 4.0,
                           [Obj("car", 0.65, 2.0, safe=False)], [],
                           _road(), 0.0, p)
    assert "pass" in [w.src for w in wp_safe]
    assert "pass" not in [w.src for w in wp_unsafe]


def test_old_fixed_gate_replaced_by_cone_and_adjacent_rule():
    """1.4 道外的币：旧 1.2 道固定门会丢；有承诺道时按锥+相邻请求裁决。"""
    coin = Obj("coin", 1.4 * LW, 2.5)
    rep = select(_road(), [], [], [coin], 0.0, _p(), commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request == 1.0
    # 无承诺道：只做本道/相邻一道，不直接拽 1.4 道
    rep2 = select(_road(), [], [], [coin], 0.0, _p())
    assert rep2.layer == 4 and rep2.target_x_m == pytest.approx(0.0, abs=0.05)


def test_multi_lane_coin_time_budget_gates_request():
    """±2 请求的资格是光锥：时间不够（到站 0.5s、横移 2 道来不及）不请求；
    时间够由 test_two_lane_coin_requests_when_reachable 覆盖。"""
    rep = select(_road(), [], [], [Obj("coin", 2.0 * LW, 0.5)], 0.0, _p(),
                 commit_x_m=0.0)
    assert rep.layer == 4 and rep.lane_request is None


def test_scoring_hit_carries_pending_adjacent_request():
    """命中拍不再吞请求：另一辆邻道奖励车的挂起请求随报告带出。"""
    rewards = [Obj("reward", 1.0 * LW, 2.2), Obj("reward", 0.1 * LW, 2.5)]
    rep = select(_road(), rewards, [], [], 0.0, _p(), commit_x_m=0.0)
    assert rep.layer == 1 and rep.why == "reward"
    assert rep.lane_request == 1.0


def test_safety_hit_discards_pending_request():
    """擦边/让位命中时挂起请求本拍丢弃（安全动作优先，路线 pass_hold）。"""
    rep = select(_road(), [Obj("reward", 1.0 * LW, 2.2)],
                 [Obj("car", 0.0, 2.0)], [], 0.0, _p(), commit_x_m=0.0)
    assert rep.why == "pass" and rep.lane_request is None


def test_selector_horizon_override_changes_layer():
    """4b 接线：本拍视界覆盖只作用于选择/预置，不改驻留参数。"""
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    sel = LayeredSelector(_p())
    coin = [Obj("coin", 0.0, 2.5)]
    assert sel.update(_road(), [], [], coin, 0.0, 0.05).layer == 3
    sel.reset()
    rep = sel.update(_road(), [], [], coin, 0.0, 0.05, horizon_s=2.0)
    assert rep.layer == 4        # 2.5s 机会出 2s 视界，只剩预置（x=0）


def test_layer_dwell_delays_scoring_layer_but_not_pass():
    """4c：计分层切换需 0.2s；擦边/让位层永远立即。"""
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    sel = LayeredSelector(_p(layer_dwell=True))
    reward = [Obj("reward", 0.0, 2.0)]
    assert sel.update(_road(), reward, [], [], 0.0, 0.05).layer == 1
    coin = [Obj("coin", 0.0, 1.0)]
    for _ in range(4):                   # 0.15s（含首拍 0）：仍在旧层
        assert sel.update(_road(), [], [], coin, 0.0, 0.05).layer == 1
    assert sel.update(_road(), [], [], coin, 0.0, 0.05).layer == 3
    # 擦边层立即接管，不等驻留
    car = [Obj("car", 0.0, 2.0)]
    assert sel.update(_road(), [], car, [], 0.0, 0.05).layer == 2
    # 离开擦边层也立即
    assert sel.update(_road(), reward, [], [], 0.0, 0.05).layer == 1


def test_layer_dwell_default_off_switches_immediately():
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    sel = LayeredSelector(_p())
    sel.update(_road(), [Obj("reward", 0.0, 2.0)], [], [], 0.0, 0.05)
    assert sel.update(_road(), [], [], [Obj("coin", 0.0, 1.0)],
                      0.0, 0.05).layer == 3


def test_event_replan_small_step_and_safety_override():
    """4c：无输入事件时目标 0.1 道/拍小步修正；replan 与擦边层立即采纳。"""
    from maaracing_master.plugins.speedrush.selection import LayeredSelector
    p = _p(event_replan=True, switch_dwell_s=0.0)
    sel = LayeredSelector(p)
    r1 = sel.update(_road(), [Obj("reward", 0.0, 2.0)], [], [], 0.0, 0.05)
    assert r1.layer == 1 and r1.target_x_m == pytest.approx(0.0)
    r2 = sel.update(_road(), [Obj("reward", 0.8 * LW, 2.0)], [], [],
                    0.0, 0.05)
    assert r2.layer == 1
    assert r2.target_x_m == pytest.approx(0.1 * LW)
    r3 = sel.update(_road(), [Obj("reward", 0.8 * LW, 2.0)], [], [],
                    0.0, 0.05, replan=True)
    assert r3.target_x_m == pytest.approx(0.8 * LW)
    r4 = sel.update(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, 0.05)
    assert r4.layer == 2            # 擦边层不等事件


def test_pass_hold_gate_off_drops_past_car():
    """闸关：t<0 的车（已并排/略过）不进擦边候选——逐位回现行行为。"""
    rep = select(_road(), [], [Obj("car", 0.0, -0.3)], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_pass_hold_keeps_graze_until_clear():
    """闸开：并排/刚脱离（t∈[−hold,0]）保持擦边目标，免可达/阻挡检查。

    极限超车判定在"脱离并排那一帧"——层提前塌回中心撤销回避，交付
    横距散布 0.08~1.35 道（窗口 [0.54, 0.77]），就是交付失败的主体。"""
    rep = select(_road(), [], [Obj("car", 0.0, -0.3)], [], 0.0,
                 _p(pass_hold_enabled=True))
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m) == pytest.approx(0.68 * LW, abs=0.05)


def test_pass_hold_pending_car_still_beats_past_car():
    """闸开排序：来得及的未到车（t≥0）优先于已并排的（t<0），不倒挂。"""
    cars = [Obj("car", 0.0, -0.2), Obj("car", 0.0, 2.5)]
    rep = select(_road(), [], cars, [], 0.0, _p(pass_hold_enabled=True))
    assert rep.layer == 2 and rep.why == "pass"
    wp_t = [w.t for w in rep.waypoints if w.src == "pass"]
    assert wp_t and wp_t[0] == pytest.approx(2.5)


def test_pass_min_gap_rejects_squeeze_band():
    """闸开：走廊夹取把目标压进 [半宽和, min_gap) 危险带即弃车。

    窄路（1 道）里车偏一侧，另一侧被夹回只剩 0.57 道——闸关时接受
    （>半宽和合法但离碰撞线只剩 0.03 道，横移噪声 ±0.15 道半数撞上）；
    闸开时两侧都给不出 0.6 道 → 不擦，回中心。"""
    car_x = 0.42 * LW
    rep = select(_road(width_lanes=1.0), [], [Obj("car", car_x, 2.0)],
                 [], 0.0, _p())
    assert rep.layer == 2 and rep.why == "pass"
    assert abs(rep.target_x_m - car_x) == pytest.approx(0.57 * LW, abs=0.02)
    rep2 = select(_road(width_lanes=1.0), [], [Obj("car", car_x, 2.0)],
                  [], 0.0, _p(pass_hold_enabled=True))
    assert rep2.layer == 4 and rep2.why == "center"


def test_pass_in_commit_gate_off_stays_commit():
    """闸关：承诺期擦边不运行（现行行为——承诺期 1~3 层关闭）。"""
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0, _p(),
                 commit_x_m=0.0, committed=True)
    assert rep.layer == 4 and rep.why == "commit"


def test_pass_in_commit_dodges_during_merge():
    """闸开：承诺道内的擦边候选（帧 #1530 实景：出租车在承诺道车表）
    擦边层放行——安全动作不被并线锁死（2.0s=可达窗口内）。"""
    rep = select(_road(), [], [Obj("car", 0.0, 2.0)], [], 0.0,
                 _p(pass_in_commit_enabled=True),
                 commit_x_m=0.0, committed=True)
    assert rep.layer == 2 and rep.why == "pass"


def test_graze_in_merge_respects_commit_span():
    """并线期贴边跨度（2026-11-08）：承诺道中心 0 时，1.3 道外的邻道车
    贴边目标 0.62 道出跨度——不贴（commit）；0.9 道外目标 0.22 道在
    跨度内放行。修前承诺期 commit_x 不喂，sp=None 使小口变全开。"""
    p = _p(pass_in_commit_enabled=True, pass_graze_adjacent_enabled=True)
    out = select(_road(), [], [Obj("car", 1.3 * LW, 3.0)], [], 0.0, p,
                 commit_x_m=0.0, committed=True)
    assert out.layer == 4 and out.why == "commit"
    inside = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)], [], 0.0, p,
                    commit_x_m=0.0, committed=True)
    assert inside.layer == 2 and inside.why == "graze"


def test_pass_tries_other_side_when_roomier_blocked():
    """贴边两侧逐侧过三关（2026-11-08）：首选（空间大）侧通过位被占，
    另一侧净空够且横移路径空——改贴另一侧，不弃车。"""
    p = _p(pass_graze_adjacent_enabled=True)
    ego = 1.5 * LW
    target = Obj("car", 1.0 * LW, 2.5)
    blocker = Obj("car", 0.3 * LW, 1.0)     # 占首选侧通过位（0.32 道）
    rep = select(_road(), [], [target, blocker], [], ego, p)
    assert rep.layer == 2
    assert rep.target_x_m > ego             # 改贴右（另一）侧


def test_graze_hold_keeps_across_commit_span_when_not_merging():
    """保持态贴边不被承诺道跨度夹（2026-11-08）：贴边/让位不进跨线门
    （_pick_pass 契约）。1.3 道外车的贴边目标 0.62 道出本道跨度，闩仍续住；
    修前 keep 态也做跨度检查，闩静默断、下一拍回 center。"""
    sel = LayeredSelector(_p(pass_graze_adjacent_enabled=True))
    r1 = sel.update(_road(), [], [Obj("car", 1.25 * LW, 3.0, oid=11)],
                    [], 0.0, 0.05, committed=False, commit_x_m=0.0)
    assert r1.why == "graze"
    # 到站 0.5s 已不可达：机会层不再命中，闩按车 id 续住
    r2 = sel.update(_road(), [], [Obj("car", 1.25 * LW, 0.5, oid=11)],
                    [], 0.0, 0.05, committed=False, commit_x_m=0.0)
    assert r2.why == "graze"


def test_pass_horizon_extends_candidates():
    """视界放宽：3.15s 出头的刹车主车（帧 #2017 实景）闸关被 3.0s 排除、
    闸开进入候选——躲不躲仍由可达性判据把关。"""
    far = Obj("car", 0.0, 3.15)
    rep = select(_road(), [], [far], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"
    rep2 = select(_road(), [], [Obj("car", 0.0, 3.15)], [], 0.0,
                  _p(pass_horizon_s=4.5))
    assert rep2.layer == 2 and rep2.why == "pass"


def test_graze_adjacent_car_leans_toward_it():
    """主动贴边（2026-11-03）：邻道 0.9 道的车、3s 到站——不再只回中吃 30
    分，目标贴到车心近侧 graze_gap（极限超车 150 分的入口）。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.layer == 2 and rep.why == "graze"
    assert rep.target_x_m == pytest.approx(0.9 * LW - 0.68 * LW, abs=0.06)


def test_graze_same_lane_emergency_wins_over_adjacent():
    """同道贴身车按到站排序先于邻道贴边：躲车（pass）优先于擦车（graze）。
    邻道车摆在 1.3 道（贴边带外缘）——0.9 道会占住同道车的闪避位（占用
    否决正确，不是本用例要验的东西）；同道车 2.0s=可达窗口内。"""
    cars = [Obj("car", 1.3 * LW, 3.0), Obj("car", 0.0, 2.0)]
    rep = select(_road(), [], cars, [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.layer == 2 and rep.why == "pass"


def test_graze_adjacent_needs_ample_time():
    """时门：邻道车 1s 到站太急（lean_min=2s）不起意贴边——临时起意=撞车，
    落回中心带（紧急躲避仍由同道带与车进半宽后接管）。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 1.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.layer == 4 and rep.why == "center"


def test_graze_target_blocked_by_other_car():
    """贴边位被别车占住（到站相近）时不贴：graze 候选让位，不挤进车缝。"""
    cars = [Obj("car", 0.9 * LW, 3.0), Obj("car", 0.25 * LW, 3.1)]
    rep = select(_road(), [], cars, [], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.why != "graze"


def test_graze_gate_off_keeps_reactive_only():
    """闸关=逐位回今天：邻道车不进擦边候选，落回中心带。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)], [], 0.0, _p())
    assert rep.layer == 4 and rep.why == "center"


def test_graze_hold_state_traced():
    """保持闩观测面（2026-11-04 补，只读）：命中带 hit_oid、闩状态 [车 id,
    宽限余量秒] 随选择器面世——贴车段为什么断离线可回答（新局 120417
    0 段并行 >0.35s，闩是否啮合/何时释放无从判断）。"""
    sel = LayeredSelector(_p(pass_graze_adjacent_enabled=True))
    car = Obj("car", 0.9 * LW, 3.0, oid=7)
    rep = sel.update(_road(), [], [car], [], 0.0, 0.05)
    assert rep.why == "graze" and rep.hit_oid == 7
    oid, miss = sel.hold_state
    assert oid == 7 and miss > 0.0
    sel2 = LayeredSelector(_p())
    sel2.update(_road(), [], [], [], 0.0, 0.05)
    assert sel2.hold_state == (None, 0.0)


def test_graze_in_commit_within_span():
    """承诺期贴边开小口（2026-11-03 人工对照）：承诺期占实跑 30% 拍，贴边
    被整体禁掉＝人工策略 1/3 时间不存在。目标不出承诺道跨度（±半道）的
    贴边只是道内横移，不构成改道——放行。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True,
                    pass_in_commit_enabled=True),
                 commit_x_m=0.0, committed=True)
    assert rep.layer == 2 and rep.why == "graze"
    assert rep.target_x_m == pytest.approx(0.9 * LW - 0.68 * LW, abs=0.06)


def test_graze_in_commit_out_of_span_stays_commit():
    """贴边目标出承诺道跨度＝要跨线，承诺语义不动：仍按承诺道回中。"""
    rep = select(_road(), [], [Obj("car", 1.3 * LW, 3.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True,
                    pass_in_commit_enabled=True),
                 commit_x_m=0.0, committed=True)
    assert rep.layer == 4 and rep.why == "commit"


def test_graze_outranks_coin():
    """贴边压金币（2026-11-03 人工对照）：极限超车 150 > 币组 90，人工局
    55% 超车落 150 分带——邻道有可贴车时不收币。"""
    rep = select(_road(), [], [Obj("car", 0.9 * LW, 3.0)],
                 [Obj("coin", 0.3 * LW, 2.5)], 0.0,
                 _p(pass_graze_adjacent_enabled=True))
    assert rep.layer == 2 and rep.why == "graze"


def test_graze_hit_drops_pending_lane_request():
    """贴边命中拍丢弃挂起请求：贴着一辆车的同时不再起并线（意图打架源）。"""
    rep = select(_road(), [Obj("reward", 1.5 * LW, 2.5)],
                 [Obj("car", 0.9 * LW, 3.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True), commit_x_m=0.0)
    assert rep.why == "graze" and rep.lane_request is None


def _sel_car(oid, x, t, safe=True):
    return Obj("car", x, t, oid=oid, safe=safe)


def test_graze_hold_survives_time_reading_dropout():
    """贴边保持闩（20261103_125755 车51 帧证）：纵向时间读数拍拍乱跳，
    贴边一旦成立就按车 id 续住——时间读数丢失/闪断翻负不再 1 拍闪断。"""
    sel = LayeredSelector(_p(pass_graze_adjacent_enabled=True,
                             pass_horizon_s=4.5, pass_hold_enabled=True,
                             pass_hold_s=0.5))
    road = _road()
    # 邻道 0.9 道、2.2s：贴边成立
    rep = sel.update(road, [], [_sel_car(51, 0.9 * LW, 2.2)], [], 0.0, 0.05)
    assert rep.why == "graze" and rep.hit_oid == 51
    # 下一拍时间读数丢失（None）：闩续住，目标刷新为车最新横位
    rep = sel.update(road, [], [_sel_car(51, 0.92 * LW, None)], [], 0.0, 0.05)
    assert rep.why == "graze"
    assert rep.target_x_m == pytest.approx(0.92 * LW - 0.68 * LW, abs=0.06)
    # 时间读数闪断翻负但在保持窗内（−0.4 > −0.5）：闩仍续住
    rep = sel.update(road, [], [_sel_car(51, 0.92 * LW, -0.4)], [], 0.0, 0.05)
    assert rep.why == "graze"


def test_graze_hold_releases_when_car_gone():
    """车读数消失超过宽限（1s）闩释放，回中/收币接管。"""
    sel = LayeredSelector(_p(pass_graze_adjacent_enabled=True,
                             pass_horizon_s=4.5))
    road = _road()
    rep = sel.update(road, [], [_sel_car(51, 0.9 * LW, 2.2)], [], 0.0, 0.05)
    assert rep.why == "graze"
    rep = None
    for _ in range(21):                       # 1.05s 无读数 > 1s 宽限
        rep = sel.update(road, [], [], [], 0.0, 0.05)
    assert rep.why != "graze"


def test_graze_hold_releases_when_car_passes():
    """车明确落到身后（t < −hold_s 且持续）：闩释放，不再贴已过的车。"""
    sel = LayeredSelector(_p(pass_graze_adjacent_enabled=True,
                             pass_horizon_s=4.5, pass_hold_enabled=True,
                             pass_hold_s=0.5))
    road = _road()
    rep = sel.update(road, [], [_sel_car(51, 0.9 * LW, 2.2)], [], 0.0, 0.05)
    assert rep.why == "graze"
    rep = None
    for _ in range(15):                       # 0.75s 身后 > 0.5s 保持窗
        rep = sel.update(road, [], [_sel_car(51, 0.9 * LW, -0.6)], [], 0.0, 0.05)
    assert rep.why != "graze"


def test_graze_hold_yields_to_emergency_pass():
    """紧急躲避（同道贴身车出现）立即接管，闩不硬拖。"""
    sel = LayeredSelector(_p(pass_graze_adjacent_enabled=True,
                             pass_horizon_s=4.5))
    road = _road()
    rep = sel.update(road, [], [_sel_car(51, 0.9 * LW, 3.0)], [], 0.0, 0.05)
    assert rep.why == "graze"
    rep = sel.update(road, [], [_sel_car(51, 0.9 * LW, 3.0),
                                _sel_car(52, 0.0, 2.0)], [], 0.0, 0.05)
    assert rep.why == "pass"


def test_graze_hold_yields_to_reward_car():
    """奖励车（更高价值）出现立即接管。"""
    sel = LayeredSelector(_p(pass_graze_adjacent_enabled=True,
                             pass_horizon_s=4.5))
    road = _road()
    rep = sel.update(road, [], [_sel_car(51, 0.9 * LW, 3.0)], [], 0.0, 0.05)
    assert rep.why == "graze"
    rep = sel.update(road, [Obj("reward", 1.0, 2.0)],
                     [_sel_car(51, 0.9 * LW, 3.0)], [], 0.0, 0.05)
    assert rep.layer == 1 and rep.why == "reward"


def test_pass_window_queue_blocks_van_type_target():
    """通过时窗占用（20261104 局2 f3605 帧证）：首选侧通过线上有排队车
    → 绕另一侧；两侧都占 → 不贴（方向选择不破占用判定）。"""
    target = Obj("car", 0.0, 2.2, v_close=15.0)
    queue = Obj("car", 0.68 * LW, 2.5)      # 恰在贴边线上（邻道带）
    rep = select(_road(), [], [target], [], 0.0, _p())
    assert rep.layer == 2 and rep.why == "pass"
    rep2 = select(_road(), [], [target, queue], [], 0.0, _p())
    assert rep2.layer == 2 and rep2.target_x_m < 0     # 绕右被占 → 绕左
    queue2 = Obj("car", -0.68 * LW, 1.0)    # 左也占（t<lean 门，自身不进候选）
    rep3 = select(_road(), [], [target, queue, queue2], [], 0.0, _p())
    assert rep3.why != "pass" and rep3.why != "graze"


def test_pass_window_slow_target_long_alongside():
    """速度近同的目标并排段最长（f3775 磨车 4.8s 形态）：首选侧通过线
    被晚到车占 → 绕另一侧；两侧都占 → 不贴。"""
    target = Obj("car", 0.0, 2.5, v_close=2.0)
    late = Obj("car", 0.68 * LW, 4.0)
    rep = select(_road(), [], [target, late], [], 0.0, _p())
    assert rep.layer == 2 and rep.target_x_m < 0       # 绕左
    late2 = Obj("car", -0.68 * LW, 1.0)
    rep3 = select(_road(), [], [target, late, late2], [], 0.0, _p())
    assert rep3.why != "pass" and rep3.why != "graze"
    rep2 = select(_road(), [], [Obj("car", 0.0, 2.5)], [], 0.0, _p())
    assert rep2.layer == 2 and rep2.why == "pass"


def test_pass_hold_beside_rejects_occupied_window():
    """并排保持（t≤0 免可达）不再免占用：贴边线前方通过窗内被别的车
    占住就不保持（追尾慢车后夹停滞的入口封死）。无 v_close 逐位回今天
    （test_pass_hold_keeps_graze_until_clear 锁无条件保持）。"""
    beside = Obj("car", 0.0, -0.3, v_close=0.5)
    queue = Obj("car", 0.68 * LW, 1.0)
    rep = select(_road(), [], [beside, queue], [], 0.0,
                 _p(pass_hold_enabled=True))
    assert rep.why != "pass" and rep.layer != 2


def test_guard_moves_target_out_of_arriving_car_band():
    """目标占用守卫（2026-11-09）：目标点落在「2 秒内到站车」的车体带上时
    推到车体带外。旧行为下回中目标与来车同线，等于把车摆在它的路上。"""
    from maaracing_master.plugins.speedrush.selection import _clear_of_arrivals
    road = _road()
    p = _p(arrival_guard_enabled=True)
    x = _clear_of_arrivals(0.17 * LW, 0.45 * LW,
                           [Obj("car", 0.5 * LW, 1.0)], road, p)
    assert abs(x - 0.5 * LW) >= 0.54 * LW
    # 3 秒后才到的车不在守卫视界内：目标原样保留
    assert _clear_of_arrivals(0.17 * LW, 0.45 * LW,
                              [Obj("car", 0.5 * LW, 3.0)], road, p)         == pytest.approx(0.17 * LW)
    # 闸关（默认，隔离 A/B 基线）：守卫直通，目标原样
    assert _clear_of_arrivals(0.17 * LW, 0.45 * LW,
                              [Obj("car", 0.5 * LW, 1.0)], road,
                              _p()) == pytest.approx(0.17 * LW)


def test_guard_ignores_grid_pseudo_cars():
    """深占据伪车源（src=grid）不驱动到车守卫：该源有已知假阳性（HUD 投影
    /路面），守卫若跟它走会把目标拽离 1~2 道（20261109_134818 离线重放
    fid 1217：有伪车 +3.52m、无伪车 +0.59m，录制值 0.59m）。真车照常守卫。"""
    from maaracing_master.plugins.speedrush.selection import _clear_of_arrivals
    road = _road()
    p = _p(arrival_guard_enabled=True)
    ghost = Obj("car", 0.5 * LW, 1.0, src="grid")
    assert _clear_of_arrivals(0.17 * LW, 0.45 * LW, [ghost], road, p)         == pytest.approx(0.17 * LW)
    real = Obj("car", 0.5 * LW, 1.0, src="metric")
    assert abs(_clear_of_arrivals(0.17 * LW, 0.45 * LW, [real], road, p)
               - 0.5 * LW) >= 0.54 * LW


def test_graze_target_stays_outside_guard():
    """擦边目标离车心 0.68 道（> 半宽和），守卫与擦边层本身不该改动它。"""
    rep = select(_road(), [], [Obj("car", 1.2 * LW, 2.0)], [], 0.0,
                 _p(pass_graze_adjacent_enabled=True,
                    arrival_guard_enabled=True))
    assert rep.why == "graze"
    assert abs(rep.target_x_m - 1.2 * LW) == pytest.approx(0.68 * LW, abs=0.06)


def test_pass_x_unknown_drops_adjacent_and_same_lane():
    """横位未知态（2026-11-10 真值小集；2026-11-14 扩到同道）：发布横位
    相邻跳超物理上限、锚点换锚，或硬跳变门正把旧 x 续着的拍，贴边/让位
    一律弃车——旧 x 正是坏读数，拿它判同道/选侧会把目标翻到车另一侧
    （20261114_094558 车51：−1.79m 被旧 x 0.628 道遮住，通过侧从右翻左）。
    上一有效目标由贴边保持闩续过坏读数拍；闸关=逐位回今天。"""
    p_on = _p(pass_graze_adjacent_enabled=True, pass_x_unknown_enabled=True)
    # 邻道 unknown 车不进贴边
    rep = select(_road(), [], [Obj("car", 1.2 * LW, 2.5, x_unknown=True)],
                 [], 0.0, p_on)
    assert rep.why not in ("pass", "graze")
    # 同一车读数可信（unknown=False）：照常贴
    rep = select(_road(), [], [Obj("car", 1.2 * LW, 2.5)], [], 0.0, p_on)
    assert rep.why in ("pass", "graze")
    # 闸关：unknown 标记不生效
    rep = select(_road(), [], [Obj("car", 1.2 * LW, 2.5, x_unknown=True)],
                 [], 0.0, _p(pass_graze_adjacent_enabled=True))
    assert rep.why in ("pass", "graze")
    # 同道 unknown 车也弃（旧 x 可能把邻道车伪装成同道，躲避方向不可信）
    rep = select(_road(), [],
                 [Obj("car", 0.0, None, near=True, x_unknown=True)],
                 [], 0.0, p_on)
    assert rep.why not in ("pass", "graze")


def test_pass_same_lane_prefers_ego_side_and_measured_width():
    """20261114_094558 车51 碰撞回归：锚点 −1.79m（绝对 0.30 道）、实测
    半宽 1.18m、自车 0.811 道。旧口径按「空间更大」选左、按名义半宽夹
    目标，输出 0.84 道落在车体包络内；新口径走自车所在右侧，且离车心
    不小于 ego_half + 实测半宽 + 碰撞余量。"""
    car_x = 0.30 * LW
    ego = 0.81 * LW
    ego_half = 0.27 * LW
    margin = 0.05
    hw = 1.18
    clear = ego_half + hw + margin
    rep = select(_road(), [],
                 [Obj("car", car_x, None, near=True, half_w_m=hw)],
                 [], ego, _p())
    assert rep.layer == 2 and rep.why == "pass"
    assert rep.target_x_m > ego                    # 自车在右：不横穿目标车
    assert rep.target_x_m - car_x >= clear - 1e-9
    # 旧输出 0.84 道在车体包络内——净空判据必须判它不可行
    assert abs(0.84 * LW - car_x) < clear
    # 更宽的车（实测半宽 1.6m）目标随物理净空外移，不受 graze_gap 封顶
    wide = select(_road(), [], [Obj("car", car_x, None, near=True,
                                    half_w_m=1.6)], [], ego, _p())
    assert wide.why == "pass"
    assert wide.target_x_m - car_x >= ego_half + 1.6 + margin - 1e-9


def test_unknown_car_does_not_veto_held_target():
    """横位未知拍的车不能当「通过位被占」的证据：并排碰撞局 2504 拍自车
    已在右、原命中车读数消失，新车的坏 x 落在保持位 0.6m 内——占用否决
    会释放保持闩、落回中心（左打 2.2m）。保持位应续住等读数恢复。"""
    sel = LayeredSelector(_p(pass_latch_enabled=True,
                             pass_x_unknown_enabled=True,
                             pass_graze_adjacent_enabled=True))
    r1 = sel.update(_road(), [],
                    [Obj("car", 0.309 * LW, 2.0, oid=52)], [],
                    0.761 * LW, 0.05)
    assert r1.why == "pass"
    x0 = r1.target_x_m
    for _ in range(4):
        r2 = sel.update(_road(), [],
                        [Obj("car", 0.806 * LW, None, near=True, oid=51,
                             half_w_m=1.142, x_unknown=True)],
                        [], 0.790 * LW, 0.05)
        assert r2.why == "pass" and r2.layer == 2
        assert r2.target_x_m == pytest.approx(x0, abs=0.1)


def test_graze_latch_freezes_target_on_unknown_reading():
    """命中车读数未知时保持闩不刷新目标：坏 x 不能把保持线拖到车体上。

    20261114_094558 车51 的 2504/2506 两拍：z 穿零时 x 塌到 0，下一拍又
    跳到 −1.79m——按拍重算会把目标从 1.01 道翻到 −0.05 道。"""
    sel = LayeredSelector(_p(pass_latch_enabled=True,
                             pass_x_unknown_enabled=True))
    rep = sel.update(_road(), [], [Obj("car", 0.3, 2.0, oid=7)], [],
                     0.0, 0.05)
    assert rep.why == "pass"
    x0 = rep.target_x_m
    for _ in range(4):
        rep = sel.update(_road(), [],
                         [Obj("car", 0.9, None, near=True, oid=7,
                              x_unknown=True)], [], 0.0, 0.05)
        assert rep.why == "pass" and rep.layer == 2
        assert rep.target_x_m == pytest.approx(x0, abs=0.05)
