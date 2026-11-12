# -*- coding: utf-8 -*-
"""step 4 决策层的回归锁：配置 fail-loud、校验合取、评分偏序、FSM 全转移矩阵。

矩阵按设计稿 v2 §二逐格上锁（含取消/超时/改判/恢复/升级五类收尾路径）；
评分只锁**偏序**不锁绝对值（验收 3）；数值场景以 decision.json 起值反推
（helper 里注释了各场景过/不过哪道门的算式），改参数会红——这是刻意的：
参数与测试场景是一对契约面。
"""

from __future__ import annotations

import json
import math

import pytest

from maaracing_master.plugins.speedrush.config import _read_decision
from maaracing_master.plugins.speedrush.decision import (
    DecisionEngine, Scorer, ValidationWatch)
from maaracing_master.plugins.speedrush.traffic import CarView
from maaracing_master.plugins.speedrush.tracking import (
    CoinGroup, DecisionState, PerceptionHealth, TrackedTarget, WorldObservation)
from maaracing_master.plugins.speedrush.world_model import (
    IMAGE_H, _read_gate0, load_calib, t_meet_car_s)
from maaracing_master.plugins.speedrush import DECISION_FILE as CFG_PATH, GATE0_FILE as GATE0_PATH

DT = 0.05          # 20Hz 名义 tick
CFG = _read_decision(
    __import__("maaracing_master.plugins.speedrush", fromlist=["DECISION_FILE"])
    .DECISION_FILE)


# ---------- 观测构造 helper（场景算式见各测试注释） ----------

def _obs(*, fid=1, groups=(), targets=(), presence=None, alive=True, fresh=True,
         geometry=True, transition=False, boundary=None, fid_hz=None, raw=None,
         raw_coins=0):
    if presence is None:
        presence = bool(groups or targets)
    if raw is None:
        raw = presence
    return WorldObservation(
        schema_version=1, frame_id=fid, ts_ns=fid * 50_000_000, frame_age_ms=10.0,
        stage=1,
        health=PerceptionHealth(alive, fresh, geometry, presence, transition,
                                raw_cars=int(bool(raw)),
                                raw_coins=raw_coins),
        boundary=boundary, targets=targets, far_targets=(), coin_groups=groups,
        fid_rate_hz=fid_hz)


def _members(fid, gid, n, x, cy, rel, conf=0.85):
    return [TrackedTarget(
        id=gid * 100 + i, kind="coin", x_lane=x - 0.15 * i, x_sigma=0.1,
        cy=cy + i * 3, w=20, h=20, conf=conf, rel_approach=rel,
        first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
        for i in range(n)]


def _group(gid, fid, n, x, cy, rel, conf=0.85):
    ms = _members(fid, gid, n, x, cy, rel, conf)
    g = CoinGroup(group_id=gid, member_ids=tuple(m.id for m in ms),
                  observed_count=n, estimated_count=n, x_center=x,
                  x_span=0.15 * (n - 1), cy_min=cy, cy_max=cy + 3 * (n - 1),
                  conf_min=conf, partial_observation=False,
                  first_seen_fid=fid, last_seen_fid=fid, validity_until_fid=fid + 8)
    return g, tuple(ms)


# 场景 A（默认"良候选"）：n=3 conf=0.85 x=0.5 cy=500 rel=5
#   流口径（全场 k 中位 = rel·hz/(cy−y_h)²）：k=100/178²≈3.16e-3；
#   t_miss=∫=(1/k)·(1/181.8−1/391.8)≈0.93s；life=e^−0.93/5≈0.83
#   value=30·3·0.6=54 → 44.8 −shift 5 ≈ 39.8 ≥ min 20 ✓；
#   need=0.3+0.4·0.5+0.13+0.15=0.78 < 0.93 ✓可行
GOOD = dict(gid=1, n=3, x=0.5, cy=500, rel=5)


def _good_obs(fid=1, **kw):
    p = {**GOOD, **kw}
    gid = p.pop("gid")
    g, ms = _group(gid, fid, **p)
    return _obs(fid=fid, groups=(g,), targets=ms)


# ---------- 配置 fail-loud ----------

def _decision_dict():
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("schema_version"), "schema_version"),
    (lambda d: d["validate"].update(t_recover_s=99.0), "t_conserve_max"),
    (lambda d: d["mode"].update(allow_car_graze=True), "allow_car_graze"),
    (lambda d: d["scoring"].update(conf_floor=1.5), "越界"),
    (lambda d: d["scoring"].update(conf_floor=0.9), "折扣区倒置"),
    (lambda d: d["control"].update(frame_rate_hz=0), "越界"),
    (lambda d: d.pop("planner"), "planner"),
    (lambda d: d["planner"].update(lookahead_tau_s=0.05), "lookahead"),
    (lambda d: d["planner"].update(rate_limit_raw=99999.0), "限幅"),
    (lambda d: d["control"].pop("abort_settle_v_lane_s"), "abort_settle"),
    (lambda d: d["planner"].update(maneuver_stick_max=0.0), "maneuver_stick_max"),
    (lambda d: d["planner"].update(k_lat_lane_s=-1.0), "k_lat_lane_s"),
    (lambda d: d["traffic"].pop("v_lat_ema_alpha"), "v_lat_ema"),
    (lambda d: d["motion"].update(preview_s=3.0), "preview_s"),
    (lambda d: d["motion"].update(mode="live", calibrated=False),
     "calibrated"),
])
def test_decision_fail_loud(tmp_path, mutate, frag):
    d = _decision_dict()
    mutate(d)
    p = tmp_path / "decision.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        _read_decision(p)
    assert frag in str(e.value)


@pytest.mark.parametrize("mutate,frag", [
    (lambda d: d.pop("a_x"), "a_x"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d.update(y_h="地平线"), "非数值"),
])
def test_gate0_fail_loud(tmp_path, mutate, frag):
    d = json.loads(GATE0_PATH.read_text(encoding="utf-8"))
    mutate(d)
    p = tmp_path / "gate0.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        _read_gate0(p)
    assert frag in str(e.value)


def test_real_sources_load():
    assert CFG.validate.t_recover_s < CFG.validate.t_conserve_max_s
    cal = load_calib()
    assert cal.a_x > 0 and cal.min_denom > 0 and cal.lane_w_m > 0
    # 线角相机焦距：标定真源，落在全幅相机的合理域内
    assert 600.0 <= cal.focal_px <= 1200.0


# ---------- 校验合取（§四：五信号合取，任何单项不作数） ----------

def test_watch_frame_stale_fatal():
    w = ValidationWatch(CFG)
    r = w.check(_obs(fresh=False), DT)
    assert r.fatal == "frame_stale"


def test_watch_stale_during_transition_not_fatal():
    w = ValidationWatch(CFG)
    assert w.check(_obs(fresh=False, transition=True), DT).fatal is None


def test_watch_empty_needs_conjunction():
    """枯竭计时要求 alive∧fresh∧geometry∧¬transition 全立——空旷/过渡/半死不计时。"""
    w = ValidationWatch(CFG)
    for _ in range(int(CFG.validate.t_empty_s / DT)):
        assert w.check(_obs(presence=False, geometry=False), DT).fatal is None
    # 合取恢复后从头计时（旧账不追溯）
    for i in range(int(CFG.validate.t_empty_s / DT) - 1):
        assert w.check(_obs(presence=False, raw=True), DT).fatal is None
    assert w.check(_obs(presence=False, raw=True), DT).fatal == "target_empty"


def test_watch_target_presence_resets_timer():
    w = ValidationWatch(CFG)
    for _ in range(10):
        w.check(_obs(presence=False, raw=True), DT)
    assert w.check(_good_obs(), DT).fatal is None      # 有目标：清零
    for _ in range(int(CFG.validate.t_empty_s / DT) - 1):
        assert w.check(_obs(presence=False, raw=True), DT).fatal is None


def test_watch_cy_jump_reversal():
    w = ValidationWatch(CFG)
    g, ms = _group(1, 1, 3, 0.5, 500, 10)
    w.check(_obs(fid=1, groups=(g,), targets=ms), DT)
    g2, ms2 = _group(1, 2, 3, 0.5, 700, 10)            # 同 id 全组 +200px ≫ 90
    assert w.check(_obs(fid=2, groups=(g2,), targets=ms2), DT).fatal == "cy_jump"


# ---------- 评分偏序（验收 3：锁排序语义不锁绝对值） ----------

def _score(**kw):
    p = {**GOOD, **kw}
    gid = p.pop("gid")
    g, ms = _group(gid, 1, **p)
    s = Scorer(CFG, load_calib())
    return s.score(g, _obs(groups=(g,), targets=ms))


def test_score_monotone_in_count():
    assert _score(n=4).score > _score(n=3).score


def test_score_group_floor_partial_view():
    """组下限（RULES 2026-10-14 定案）：观测 1~2 枚是残缺视图（距离/遮挡/置信
    门削剩的头部），价值按整组 ≥3 枚计，不按观测枚数折算——n=1 组按 30 分
    基值再被车道惩罚一压连 min_score 都过不了（20261014 实机 score 拒
    106~119 次/局、金币候选 ok 每局 1 拍）。锁：同几何下 n=1 与 n=3 同值
    （残缺视图按整组计）且过 min_score；n=4 仍严格更高（test_monotone 已锁）。"""
    def _g_with(n):
        ms = _members(1, 1, n, 0.5, 500, 5)
        g = CoinGroup(group_id=1, member_ids=tuple(m.id for m in ms),
                      observed_count=n, estimated_count=n, x_center=0.5,
                      x_span=0.0, cy_min=500, cy_max=500, conf_min=0.85,
                      partial_observation=False,
                      first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
        return g, ms

    s = Scorer(CFG, load_calib())
    g1, ms1 = _g_with(1)
    g3, ms3 = _g_with(3)
    s1 = s.score(g1, _obs(groups=(g1,), targets=ms1))
    s3 = s.score(g3, _obs(groups=(g3,), targets=ms3))
    assert s1 is not None and s3 is not None
    # 组值项逐值同（max(1,3)=3）；t_miss 的全场 k 随组员集中位略差，放宽到 2%
    assert s1.score == pytest.approx(s3.score, rel=0.02)
    assert s1.score > 0                          # 完整整组值，未被折扣归零


def test_score_monotone_in_distance():
    # 流口径下速率随行号二次增长，远行目标必须配按场一致的 rel（同一 k）——
    # rel(cy) ∝ (cy−y_h)²，锚定 GOOD(cy=500, rel=5)；锁「越远时间越多」的序
    def _rel(cy):
        return 5.0 * (cy - load_calib().y_h) ** 2 / (500 - load_calib().y_h) ** 2
    assert _score(cy=620, rel=_rel(620)).score \
        > _score(cy=500).score > _score(cy=420, rel=_rel(420)).score


def test_score_penalizes_lateral_shift():
    assert _score(x=0.3).score > _score(x=1.5).score


def test_score_conf_ramp_and_floor():
    assert _score(conf=0.99).score > _score(conf=0.7).score
    # 20261017 教训：地板降到检测门槛 0.35（日落逆光场景 conf 帧间 0.42~0.83
    # 剧烈跳，0.5 切在区间正中把肉眼可见的币整组拒掉）——0.4 进低权坡度不拒
    assert _score(conf=0.4) is not None      # 低权计入（折扣代替拒收）
    assert _score(conf=0.4).score < _score(conf=0.7).score
    assert _score(conf=0.34) is None         # 地板（=检测门槛）下不低信决策
    held = CoinGroup(group_id=9, member_ids=(1,), observed_count=1, estimated_count=1,
                     x_center=0.5, x_span=0, cy_min=500, cy_max=500, conf_min=0.0,
                     partial_observation=True, first_seen_fid=0, last_seen_fid=0,
                     validity_until_fid=9)
    assert Scorer(CFG, load_calib()).score(held, _obs(groups=(held,))) is None


def test_score_rejects_receding_target():
    """rel≤0（远离）：机会视为无限远 → 分数被折扣压到不可选（−cost）。"""
    s = _score(rel=-5)
    assert s is None or s.score < 1.0            # 折扣归零（−位移代价）


def test_score_survives_member_id_churn_via_field_flow():
    """换基主锁（2026-10-02）：组员 id 全断（不在 targets 里，逐轨账本旧法
    必然饿死成 life=0）但场内另有健康 track——地面流是全场属性，k 从横截面
    拟合出来，金币照常可选。旧逐轨实现下此锁必红。"""
    g, _ = _group(7, 1, 3, x=0.5, cy=500, rel=0.0)     # 组员 rel=0 且不在 targets
    # 场内两个健康 track（行号不同、速率按同一 k=100/178² 流场自洽）
    flow = [TrackedTarget(
        id=90 + i, kind="coin", x_lane=-0.8 + 1.6 * i, x_sigma=0.1,
        cy=cy, w=20, h=20, conf=0.9,
        rel_approach=100.0 * (cy - load_calib().y_h) ** 2 / (500 - load_calib().y_h) ** 2
        / CFG.control.frame_rate_hz,
        first_seen_fid=1, last_seen_fid=1, validity_until_fid=9)
        for i, cy in enumerate((600, 450))]
    s = Scorer(CFG, load_calib()).score(g, _obs(targets=flow))
    assert s is not None and s.score > 0


def test_score_blackout_keeps_failsafe_discount():
    """全场无任何速率测量（断供黑视）：无限远折扣 → 不可选——安全默认与
    逐轨断供时代同形，不许在无证据时臆造可达性。"""
    g, _ = _group(7, 1, 3, x=0.5, cy=500, rel=0.0)
    s = Scorer(CFG, load_calib()).score(g, _obs(targets=()))
    assert s is not None and s.score < 1.0


# ---------- FSM 全转移矩阵（§二优先级逐格） ----------

def _eng(**over):
    return DecisionEngine(CFG)


def test_conserve_output_holds_and_forbids_moves():
    e = _eng()
    e._enter_conserve()                            # 直接进态（矩阵其余路径另有锁）
    out = e.update(_obs(fid=5, presence=True), DT)
    assert out.state is DecisionState.CONSERVE
    assert out.x_target is None                    # 保持≠回中（§二）


def test_intent_precise_marks_graze_and_pass_only():
    """交付型意图位：贴边/让位置位（计划改窄死区），回中不置位。
    接线断掉会让贴边重新吃回中的 0.3 道死区，掠过横距系统性偏大半档。"""
    from maaracing_master.plugins.speedrush.selection import (
        SelectionReport, Waypoint)
    e = _eng()
    rep = SelectionReport(2, (Waypoint(1.0, 0.3, "pass"),), 0.3, "graze",
                          True, True)
    e.selector.update = lambda *a, **k: rep
    out = e.update(_obs(fid=1, presence=True), DT, executed_lane=0.0,
                   corridor=_corridor(-5.0, 5.0))
    assert out.reason == "sel:graze" and out.intent_precise is True
    rep = SelectionReport(4, (Waypoint(1.0, 0.0, "center"),), 0.0, "center",
                          True, False)
    e.selector.update = lambda *a, **k: rep
    out = e.update(_obs(fid=2, presence=True), DT, executed_lane=0.0,
                   corridor=_corridor(-5.0, 5.0))
    assert out.reason == "sel:center" and out.intent_precise is False


def test_conserve_recovers_with_full_reset():
    e = _eng()
    e._enter_conserve()                            # fatal 触发保守态（直接进入）
    e.update(_obs(fid=2, fresh=False), DT)         # 过旧拍：保守态续留
    e.update(_obs(fid=3, presence=True), DT)
    f = 4
    out = e.update(_obs(fid=f, presence=True, geometry=True), DT)
    while out.reason != "conserve_recovered" and f < 200:
        f += 1
        out = e.update(_obs(fid=f, presence=True, geometry=True), DT)
    assert out.state is DecisionState.CRUISE
    # 首个非致命拍=fid 3：恢复计数从这拍起，不许秒回
    assert f - 2 >= int(CFG.validate.t_recover_s / DT)
    e.reset()
    assert e._target_prev is None                       # 重置面


def test_conserve_escalates_to_fault_and_back():
    """升级要"降级但不致命"的观测（过旧+过渡=非致命但 degraded），否则满 T_recover
    就恢复、到不了 T_conserve_max 的 FAULT 升级；恢复再切回健康帧走双 T_recover。"""
    e = _eng()
    e._enter_conserve()
    f = 10
    out = e.update(_obs(fid=f, presence=True, fresh=False, transition=True), DT)
    while out.reason != "fault:conserve_max" and f < 400:
        f += 1
        out = e.update(_obs(fid=f, presence=True, fresh=False, transition=True), DT)
    assert out.state is DecisionState.FAULT
    while out.reason != "fault_recovered" and f < 600:
        f += 1
        out = e.update(_obs(fid=f, presence=True), DT)   # 健康帧：双 T_recover 降回
    assert out.state is DecisionState.CRUISE
    e.reset()
    assert e._target_prev is None                       # 重置面（恢复后冷启动）


def test_output_schema_and_validity_window():
    e = _eng()
    out = e.update(_good_obs(), DT)
    assert out.schema_version == 5                 # v5：删除 move_allowed
    assert out.valid_until_fid > out.emitted_fid
    assert isinstance(out.reason, str) and out.reason


def test_unverified_position_holds_target_and_skips_selection():
    """执行层位置不可信时选择层不重建目标：场景里的车/币绝对位置全部由
    那个假位置平移而来，假位置下的选择只会给出假目标（20261031 两局：
    盲窗内目标跟着漂移的状态走，重获证据时整道跳）。"""
    e = _eng()
    e.update(_obs(fid=1, presence=True), DT, executed_lane=0.2,
             corridor=_corridor(-5.0, 5.0))
    for fid in range(2, 6):                          # 目标 slew 收敛到冻结位
        out = e.update(_obs(fid=fid, presence=True), DT, executed_lane=0.2,
                       corridor=_corridor(-5.0, 5.0), state_unverified=True)
        assert out.reason == "sel:hold:state_unverified"
        assert e.scene is None                       # 不建场景 → 不出计划
    assert out.x_target == pytest.approx(0.2, abs=1e-6)


def test_engine_reset_clears_everything():
    e = _eng()
    e.update(_good_obs(), DT)
    e.reset()
    assert e._state is DecisionState.CRUISE and e._target_prev is None

def _cv(tid=5, x=1.1, cy=500, rel=10.0):
    return CarView(id=tid, x_lane=x, cy=cy, rel_approach=rel,
                   d_min=abs(x), age_ticks=10)


def _traffic(views=(), events=()):
    return (tuple(views), tuple(events))


# ---------- 对称性原则（维护者裁定 2026-09-22：决策默认左右对称，禁用方向需显式证据） ----------

def _corridor(left_m=None, right_m=None):
    """单带走廊（_side_has_space 消费面）：米制入参，obs.boundary 通道
    v4 恒 None 已退役（第 6 步侧别检查改吃走廊带）。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, Corridor, CorridorBand, UNKNOWN)
    band = CorridorBand(
        z0=3.0, z1=6.0,
        left=BandBoundary(left_m, "measured") if left_m is not None
        else BandBoundary(None, UNKNOWN),
        right=BandBoundary(right_m, "measured") if right_m is not None
        else BandBoundary(None, UNKNOWN))
    return Corridor((band,), None, None, "pair", "ok")




def test_abort_settles_on_velocity_zero():
    """有反馈 ABORT：executed 停住（速率≤界）即落稳，不再一律冻满 2s。"""
    e2 = _eng()
    e2._enter_abort("test")                                # fatal 路径外直接进入
    out = e2.update(_obs(fid=10, presence=True), DT, executed_lane=0.2)  # 首拍立参考
    assert out.state is DecisionState.ABORT
    out = e2.update(_obs(fid=11, presence=True), DT, executed_lane=0.2)  # v=0 → 落稳
    assert out.state is DecisionState.CRUISE and "abort:settled" in out.reason
    # 仍在横移：不落稳
    e3 = _eng()
    e3._enter_abort("test")
    out = e3.update(_obs(fid=10, presence=True), DT, executed_lane=0.2)  # 立参考
    assert out.state is DecisionState.ABORT
    out = e3.update(_obs(fid=11, presence=True), DT, executed_lane=0.35)   # v=3>0.3
    assert out.state is DecisionState.ABORT





# ---------- 前置三件套①：车流 t_meet 地面流化（2026-10-11 V0 归因） ----------

def test_car_t_meet_ground_flow_and_fallback():
    """fid 率有供=地面流积分式（对 1/r 沿行积分的闭式对账）；缺供=退回旧
    行率式（已知 TTC 高估方向的旧消费口径，行为不静默换式）。"""
    cal = load_calib()
    d, D, rel, f = 200.0, IMAGE_H - cal.y_h, 4.0, 30.0
    assert t_meet_car_s(rel, cal.y_h + d, cal, f) == \
        pytest.approx(d * (D - d) / (D * rel) / f)
    assert math.isinf(t_meet_car_s(-1.0, 500.0, cal, f))   # 不接近→永不到站
    assert t_meet_car_s(4.0, IMAGE_H + 5, cal, f) == 0.0  # 已到下缘
    with pytest.raises(ValueError):
        t_meet_car_s(4.0, 500.0, cal, 0.0)
    e = _eng()
    v = _cv(tid=1, cy=cal.y_h + d, rel=rel)
    e.update(_obs(fid=1, presence=True), DT, traffic=_traffic([v]))
    # 缺供退路=旧线性式（rel 按 px/tick 消费），与三件套①前的行为逐位同
    assert e.scorer.car_t_meet_s(rel, cal.y_h + d) == \
        pytest.approx((IMAGE_H - cal.y_h - d) / (rel * 20.0))
    # 引擎逐拍喂 fid 率后换积分式
    e.update(_obs(fid=2, presence=True, fid_hz=f), DT, traffic=_traffic([v]))
    assert e.scorer.fid_hz == f
    assert e.scorer.car_t_meet_s(rel, cal.y_h + d) == \
        pytest.approx(d * (D - d) / (D * rel) / f)




# ---------- 目标流枯竭：空街不是故障 ----------

def test_empty_road_does_not_trip_target_empty():
    w = ValidationWatch(CFG)
    for fid in range(1, 12):
        rep = w.check(_obs(fid=fid, presence=False, raw=False), 0.5)
        assert rep.fatal is None


def test_raw_detections_without_tracks_trip_target_empty():
    w = ValidationWatch(CFG)
    fatal = None
    for fid in range(1, 12):
        fatal = w.check(_obs(fid=fid, presence=False, raw=True), 0.5).fatal
    assert fatal == "target_empty"


def test_raw_coins_without_tracks_do_not_trip_target_empty():
    """画面只有币/奖励、跟踪为空：机会面闪断不是感知故障，不冻结决策。"""
    w = ValidationWatch(CFG)
    for fid in range(1, 12):
        rep = w.check(_obs(fid=fid, presence=False, raw=False, raw_coins=3),
                      0.5)
        assert rep.fatal is None


def _car_t(tid, fid, cy, box):
    return TrackedTarget(
        id=tid, kind="car", x_lane=0.0, x_sigma=0.1, cy=cy, w=100, h=100,
        conf=0.9, rel_approach=0.0, first_seen_fid=1, last_seen_fid=fid,
        validity_until_fid=fid + 8, box=box)


def test_fast_approach_cy_jump_with_continuous_box_is_not_fatal():
    """大车逼近 1 拍跨 150px 是物理的；2D 框仍重叠就不是身份互换。"""
    w = ValidationWatch(CFG)
    a = _car_t(1, 1, 500, (100, 400, 220, 560))
    b = _car_t(1, 2, 650, (100, 460, 220, 700))
    assert w.check(_obs(fid=1, targets=(a,)), 0.05).fatal is None
    assert w.check(_obs(fid=2, targets=(b,)), 0.05).fatal is None


def test_cy_jump_without_box_overlap_is_fatal():
    w = ValidationWatch(CFG)
    a = _car_t(1, 1, 500, (100, 400, 200, 500))
    b = _car_t(1, 2, 650, (700, 600, 800, 700))
    assert w.check(_obs(fid=1, targets=(a,)), 0.05).fatal is None
    assert w.check(_obs(fid=2, targets=(b,)), 0.05).fatal == "cy_jump"


# ---------- 远场粗带的坐标口径（方向级消费，2026-10-21） ----------

def test_far_hint_is_shared_and_not_recomputed_in_selection_frame():
    """方向结果由走廊层共享：选择帧换算（executed/原点）不得重算或平移它。
    实拍证据：p2 2796→2799 远带与中心线未动，executed 翻 1.47 道；共享结果
    在原始坐标算好，消费端只读。"""
    from maaracing_master.plugins.speedrush.corridor import (
        BandBoundary, Corridor, CorridorBand, far_hint_lane)

    e = _eng()
    lw = e.cal.lane_w_m
    bands = tuple(
        CorridorBand(float(z), float(z + 3.0),
                     BandBoundary(-8.0, "measured", 1.0),
                     BandBoundary(8.0, "measured", 1.0))
        for z in range(-3, 30, 3))
    coarse = (CorridorBand(
        11.0, 26.0,
        BandBoundary(-9.0, "inferred"),
        BandBoundary(5.0, "inferred")),)
    e._corridor = Corridor(bands, 0.0, 16.0, "pair", "ok", coarse,
                           far_hint_lane=0.42)
    assert far_hint_lane(bands, coarse, lw) == pytest.approx(-2.0 / lw)
    for executed in (-1.0, 0.0, 1.0):
        road, _, _ = e._road_frame(executed, 1)
        assert road.far_hint_lane == pytest.approx(0.42)


def test_selection_reachability_uses_plant_kinematics():
    """横移可达性 = 执行层同一条闭环律：0.7 道约 1.4 秒（台架实测同量级）。

    旧口径「恒加速到横速上限」把 0.7 道估成 0.5 秒，比实车快一倍多——
    决策敢在车到眼前才起意，执行层按真实车判到不了。"""
    from maaracing_master.plugins.speedrush.selection import (
        SelectionParams, _reachable, _t_move)

    p = SelectionParams(lane_w_m=3.4)
    assert _t_move(0.7 * 3.4, p) == pytest.approx(1.42, abs=0.06)
    assert _t_move(1.0 * 3.4, p) == pytest.approx(1.58, abs=0.06)
    assert _t_move(0.2 * 3.4, p) < _t_move(0.7 * 3.4, p)
    # 反应时 + 余量之外还要整段横移时间：1.0 秒不够 0.7 道
    assert not _reachable(0.7 * 3.4, 0.0, 1.0, p)
    assert _reachable(0.7 * 3.4, 0.0, 1.8, p)
    assert not _reachable(3.0 * 3.4, 0.0, 1.8, p)
    # 已对齐只留反应时间
    assert _reachable(0.05 * 3.4, 0.0, 0.2, p)
    assert not _reachable(0.05 * 3.4, 0.0, 0.1, p)


def test_selection_kinematics_share_planner_source():
    """选择层的横移能力直接读 planner 的闭环律参数，不另存一份换道时间。"""
    e = _eng()
    assert e.selector.p.k_lat_lane_s == pytest.approx(
        e.cfg.planner.k_lat_lane_s)
    assert e.selector.p.tau_heading_s == pytest.approx(
        e.cfg.planner.tau_heading_s)
    assert e.selector.p.heading_max_rad == pytest.approx(
        e.cfg.planner.heading_max_rad)


def test_box_size_metric_car_uses_pixel_lateral_and_no_ttc():
    """无锚框高粗距离（2026-10-23）：横向走像素尺子、到站时间弃权、
    z 不喂精确米制口径；近/远判断吃粗距离。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)
    e = _eng()
    e._views = (_cv(tid=1, x=-1.2, cy=620, rel=8.0),)
    e._metric_cars = (MetricCarState(
        target_id=1, source=DistanceSource.BOX_SIZE, z_m=4.5, x_m=None,
        v_close_mps=None, t_meet_s=None, anchor_age_ms=None),)
    src = e._reach_car_sources(0.0)
    assert len(src) == 1
    s = src[0]
    assert s.src == "metric_coarse"
    assert s.x_m == pytest.approx(-1.2 * e.cal.lane_w_m)   # 横向=像素尺子
    assert s.t_meet_s is None and s.z_m is None             # 粗距离不进精确口径
    assert s.near is True                                   # 4.5m ≤ near_z_m=6m


def test_box_size_metric_car_without_lateral_near_enters_as_block():
    """近距无横位 ≠ 无威胁（2026-11-03 帧 0213 帧证）：补给/事件车 12m 内
    横位缺位（框心贴地平线过不了尺子门），旧口径整段跳过=避让层视而不见。
    近距按占本道处理；远轨仍诚实弃权。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)
    e = _eng()
    # 近距（z 4.5m ≤ near_z_m）无横位：按占本道入账
    e._views = (CarView(id=1, x_lane=None, cy=620, rel_approach=8.0,
                        d_min=0.0, age_ticks=10),)
    e._metric_cars = (MetricCarState(
        target_id=1, source=DistanceSource.BOX_SIZE, z_m=4.5, x_m=None,
        v_close_mps=None, t_meet_s=None, anchor_age_ms=None),)
    src = e._reach_car_sources(0.0)
    assert len(src) == 1
    s = src[0]
    assert s.src == "metric_coarse"
    assert s.x_m == pytest.approx(0.0, abs=0.3)   # 占本道（自车系正前）
    assert s.near is True
    assert s.t_meet_s is None and s.z_m is None   # 仍不进精确口径
    # 远轨（z 25m、cy 远）无横位：不入账
    e._views = (CarView(id=2, x_lane=None, cy=360, rel_approach=8.0,
                        d_min=0.0, age_ticks=10),)
    e._metric_cars = (MetricCarState(
        target_id=2, source=DistanceSource.BOX_SIZE, z_m=25.0, x_m=None,
        v_close_mps=None, t_meet_s=None, anchor_age_ms=None),)
    assert e._reach_car_sources(0.0) == []


def test_lane_ttc_map_keeps_earliest_per_lane():
    """车道到站时间表：同一道多车取最早；无到站时间/视界外的车不进表；
    道号按 ±0.5 道就近归属（路线层的时间面真源）。"""
    from types import SimpleNamespace as NS
    from maaracing_master.plugins.speedrush.decision import _lane_ttc_map
    lw = 3.5
    scene = NS(lane_w_m=lw, objs=(
        NS(kind="car", timed=True, t_arrive_s=2.0, x_m=0.2),      # 道 0
        NS(kind="car", timed=True, t_arrive_s=0.8, x_m=-0.3),     # 道 0 更近
        NS(kind="car", timed=True, t_arrive_s=1.5, x_m=3.4),      # 道 1
        NS(kind="car", timed=True, t_arrive_s=9.0, x_m=-6.9),     # 视界外
        NS(kind="car", timed=False, t_arrive_s=None, x_m=7.0),    # 无时间
        NS(kind="coin", timed=True, t_arrive_s=0.2, x_m=0.0),     # 非车
    ))
    assert _lane_ttc_map(scene, 0.0, 3.0) == {0: 0.8, 1: 1.5}


def test_lane_ttc_map_skips_unsafe_cars():
    """路线时间门只读安全车：超龄/退役车不得制造紧急或阻挡逃生。"""
    from types import SimpleNamespace as NS
    from maaracing_master.plugins.speedrush.decision import _lane_ttc_map
    scene = NS(lane_w_m=3.5, objs=(
        NS(kind="car", timed=True, t_arrive_s=0.5, x_m=0.0,
           safety_ok=False),
        NS(kind="car", timed=True, t_arrive_s=1.2, x_m=0.0,
           safety_ok=True),
    ))
    assert _lane_ttc_map(scene, 0.0, 3.0) == {0: 1.2}


def test_coin_group_far_time_is_later_than_near():
    """长币线：远端（cy_min）到站时间晚于最近一枚，选择层用它提前起意；
    计分仍用近枚时间。"""
    ms = _members(1, 1, 3, 0.5, 500, 5)
    g = CoinGroup(group_id=1, member_ids=tuple(m.id for m in ms),
                  observed_count=3, estimated_count=3, x_center=0.5,
                  x_span=0.3, cy_min=500, cy_max=506, conf_min=0.85,
                  partial_observation=False, first_seen_fid=1,
                  last_seen_fid=1, validity_until_fid=9)
    sc = Scorer(CFG, load_calib()).score(g, _obs(groups=(g,), targets=ms))
    assert sc is not None
    assert sc.t_far_s > sc.t_miss_s


def test_metric_x_ruler_disagreement_takes_urgent_reading():
    """米制 x ↔ 尺子互证（20261103 雨 p2 车5 帧证）：0.6~1 道分歧 3 拍接管、
    贴回 0.3 道 2 拍恢复；>1 道按重锚单拍接管（2026-11-09 车道归属，不走
    EMA 慢拖）。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)
    e = _eng()
    # 0.8 道分歧（0.6~1 带）：前 2 拍不接管，第 3 拍按尺子入账
    e._views = (_cv(tid=1, x=-1.0, cy=620, rel=8.0),)
    for _ in range(2):
        e._metric_cars = (MetricCarState(
            target_id=1, source=DistanceSource.ANCHOR, z_m=15.0, x_m=-1.8 * 3.5,
            v_close_mps=20.0, t_meet_s=0.75, anchor_age_ms=50.0),)
        src = e._reach_car_sources(0.0)
        assert len(src) == 1
        assert src[0].x_m == pytest.approx(-1.8 * 3.5, abs=0.4), "未满持续拍数不接管"
    e._metric_cars = (MetricCarState(
        target_id=1, source=DistanceSource.ANCHOR, z_m=15.0, x_m=-1.8 * 3.5,
        v_close_mps=20.0, t_meet_s=0.75, anchor_age_ms=50.0),)
    src = e._reach_car_sources(0.0)
    assert src[0].x_m == pytest.approx(-1.0 * 3.5, abs=0.4), "持续分歧后按尺子读数入账"
    # 贴回 1 拍不恢复，2 拍恢复
    e._metric_cars = (MetricCarState(
        target_id=1, source=DistanceSource.ANCHOR, z_m=15.0, x_m=-1.05 * 3.5,
        v_close_mps=20.0, t_meet_s=0.75, anchor_age_ms=50.0),)
    assert e._reach_car_sources(0.0)[0].x_m == pytest.approx(-1.0 * 3.5, abs=0.4)
    assert e._reach_car_sources(0.0)[0].x_m == pytest.approx(-1.05 * 3.5, abs=0.4), "贴回 2 拍恢复米制"


def test_metric_x_large_disagreement_reanchors_immediately():
    """>1 道分歧单拍重锚（2026-11-09）：尺子在场且与米制差 >1 道时立即以
    尺子入账，不等 3 拍、不被 EMA 拖住（oid67 远场错 1.7 道案）。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)
    e = _eng()
    e._views = (_cv(tid=1, x=-1.0, cy=620, rel=8.0),)
    e._metric_cars = (MetricCarState(
        target_id=1, source=DistanceSource.ANCHOR, z_m=15.0, x_m=-3.0 * 3.5,
        v_close_mps=20.0, t_meet_s=0.75, anchor_age_ms=50.0),)
    src = e._reach_car_sources(0.0)
    assert src[0].x_m == pytest.approx(-1.0 * 3.5, abs=0.1), "首拍即重锚到尺子"


def test_far_field_lane_line_median_veto():
    """远场尺子缺席（x_lane=None）时车道线顶替互证（2026-11-09）：最近线
    随框抖振荡，取中位去抖后与米制互证，>1 道两拍重锚。米制错 3 道、车道线
    真值 ~1.2 夹一个 2.5 离群，中位收敛后按车道线入账。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)
    import dataclasses
    e = DecisionEngine(dataclasses.replace(
        CFG, traffic=dataclasses.replace(
            CFG.traffic, metric_ll_veto_enabled=True)))
    ll_seq = [1.2, 2.5, 1.2, 1.2, 1.2]
    last = None
    for ll in ll_seq:
        v = CarView(id=1, x_lane=None, cy=380, rel_approach=8.0,
                    d_min=1.2, age_ticks=10, x_lane_line=ll)
        e._views = (v,)
        e._metric_cars = (MetricCarState(
            target_id=1, source=DistanceSource.ANCHOR, z_m=60.0, x_m=3.0 * 3.5,
            v_close_mps=20.0, t_meet_s=3.0, anchor_age_ms=50.0),)
        last = e._reach_car_sources(0.0)[0].x_m
    assert last == pytest.approx(1.2 * 3.5, abs=0.3), "中位去抖后按车道线重锚"
    # 闸关（默认）：车道线不参与互证，远场无尺子时信米制
    e2 = _eng()
    e2._views = (CarView(id=1, x_lane=None, cy=380, rel_approach=8.0,
                         d_min=1.2, age_ticks=10, x_lane_line=1.2),)
    e2._metric_cars = (MetricCarState(
        target_id=1, source=DistanceSource.ANCHOR, z_m=60.0, x_m=3.0 * 3.5,
        v_close_mps=20.0, t_meet_s=3.0, anchor_age_ms=50.0),)
    assert e2._reach_car_sources(0.0)[0].x_m == pytest.approx(3.0 * 3.5, abs=0.3)


def test_metric_x_agreement_keeps_metric():
    """双仪器一致（<0.6 道）不接管，正常车不受闸影响。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)
    e = _eng()
    e._views = (_cv(tid=1, x=1.2, cy=620, rel=8.0),)
    e._metric_cars = (MetricCarState(
        target_id=1, source=DistanceSource.ANCHOR, z_m=20.0, x_m=1.4 * 3.5,
        v_close_mps=5.0, t_meet_s=4.0, anchor_age_ms=50.0),)
    for _ in range(6):
        src = e._reach_car_sources(0.0)
        assert len(src) == 1
        assert src[0].x_m == pytest.approx(1.4 * 3.5, abs=0.4)


def test_metric_x_ruler_wins_even_when_metric_urgent():
    """持续分歧按尺子入账，不按"更急者"（20261103_125755 车51 二次帧证：
    米制把 1.0 道的邻道车压成 0.14 道幻影——更急的恰是错的那个，邻道车
    变同车道幻影＝过早躲避的发生器）。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)
    e = _eng()
    e._views = (_cv(tid=1, x=-1.0, cy=620, rel=8.0),)
    for _ in range(3):
        e._metric_cars = (MetricCarState(
            target_id=1, source=DistanceSource.ANCHOR, z_m=15.0, x_m=-0.14 * 3.5,
            v_close_mps=20.0, t_meet_s=0.75, anchor_age_ms=50.0),)
        src = e._reach_car_sources(0.0)
        assert len(src) == 1
    assert src[0].x_m == pytest.approx(-1.0 * 3.5, abs=0.4), \
        "更急的米制幻影不接管，尺子赢"


def test_depth_grid_blocked_cluster_enters_as_pseudo_car():
    """深占据通道（20261103_141656 帧证）：YOLO 全盲拍里近距大车不出 car
    类、车表空＝正面撞；深度栅格路内窄块簇按占道障碍入账（负数千位段 id，
    与墙按簇宽区分，单行簇滤噪声）。"""
    import numpy as np
    from maaracing_master.plugins.speedrush.depth_geo import (
        GRID_BLOCKED, GRID_CELL, GRID_NX, GRID_NZ, DrivableGrid)
    e = _eng()
    state = np.zeros((GRID_NZ, GRID_NX), dtype=np.int8)
    # 3-6m 带、右邻道（x≈+3.5m → ix≈70）、簇宽 2 格、跨 3 行（≥2 行过噪声门）
    z_lo_i = int((3.0 - 3.0) / GRID_CELL)      # GRID_Z_LO=3.0 → 第 0 行
    ix = int((3.5 + 14.0) / GRID_CELL - 0.5)   # −GRID_X_MAX=−14
    state[z_lo_i:z_lo_i + 3, ix:ix + 2] = GRID_BLOCKED
    e._depth_grid = DrivableGrid(
        state=state, coef=None, counts=np.ones_like(state),
        dig_cells=0, latency_ms=1.0)
    e._ego_speed_mps = 40.0
    grid = e._grid_car_sources(0.0)
    assert len(grid) == 1
    assert grid[0].id <= -3000 and grid[0].safety_ok
    assert grid[0].near and grid[0].t_meet_s > 0
    assert abs(grid[0].z_m - 4.5) < 2.0        # 3-6m 带中心
    # 整行宽簇（墙）不入账
    state[:] = 0
    state[z_lo_i:z_lo_i + 3, 20:90] = GRID_BLOCKED
    e._depth_grid = DrivableGrid(state=state, coef=None,
                                 counts=np.ones_like(state), dig_cells=0,
                                 latency_ms=1.0)
    assert e._grid_car_sources(0.0) == []


def test_depth_grid_pseudo_car_outside_road_dropped():
    """路缘外的深占据簇不是社会车（20261109_134818 fid 7012 帧证）：栅格
    横向窗 ±14m 宽于路面，护栏/路肩在路缘外成的窄块簇按车源入账，选择层
    的中心预置读它「左侧有车」→ 目标被顶向邻道真车。给路面帧后只认簇自身
    纵深处那条带内的簇（与场景 in_corridor 同规则；弯道远近带不同心，用
    z=0 路缘判会把远近带判错）；无路面帧（离线/旧调用）逐位回今天行为。"""
    import numpy as np
    from maaracing_master.plugins.speedrush.depth_geo import (
        GRID_BLOCKED, GRID_CELL, GRID_NX, GRID_NZ, DrivableGrid)
    from maaracing_master.plugins.speedrush.selection import (
        RoadBand, RoadFrame)
    # 近带 [3,16) 路缘 ±7m；远带 [16,30) 整体右移（左缘 −2m、右缘 +12m）
    road = RoadFrame(
        (RoadBand(3.0, 16.0, -7.0, 7.0, src="measured", conf=1.0),
         RoadBand(16.0, 30.0, -2.0, 12.0, src="measured", conf=1.0)),
        14.0, -7.0, 7.0, True)
    e = _eng()
    state = np.zeros((GRID_NZ, GRID_NX), dtype=np.int8)

    def _put(z, x):
        iz = int((z - 3.0) / GRID_CELL)
        ix = int((x + 14.0) / GRID_CELL - 0.5)
        state[iz:iz + 3, ix:ix + 2] = GRID_BLOCKED

    _put(4.5, 3.0)      # 近带内
    _put(4.5, -9.0)     # 近带外
    _put(23.0, 9.0)     # 近带外(+7) 但远带内(+12)：弯道右移，必须保留
    _put(23.0, -5.0)    # 近带内(−7) 但远带外(−2)：必须丢弃
    e._depth_grid = DrivableGrid(state=state, coef=None,
                                 counts=np.ones_like(state), dig_cells=0,
                                 latency_ms=1.0)
    e._ego_speed_mps = 40.0
    assert len(e._grid_car_sources(0.0)) == 4       # 无路面帧：逐位回今天
    kept = e._grid_car_sources(0.0, road, 0.0)
    xs = sorted(round(c.x_m, 1) for c in kept)
    assert xs == pytest.approx([3.0, 9.0], abs=0.6)


def test_z_regression_arrival_time_catches_bogus_v_close():
    """纵深回归到站时间（2026-11-09）：v_close 估计窗未开时 t 会虚高到
    十几秒（实测 seq 393~414：真值 0.6s 时还报 7~70s）。纵深斜率是同一件
    事的第二份估算，只用来把到站时间**提前**，不推迟。"""
    e = _eng()
    e._fps = 20.0
    # 8 拍、每拍 -3m：斜率 -60m/s，最后一拍 z=19m → t≈0.317s
    e._car_z_hist[1] = [(100 + k, 40.0 - 3.0 * k) for k in range(8)]
    assert e._z_regress_t(1) == pytest.approx(19.0 / 60.0, rel=0.05)
    # 远离/静止：不给提前量
    e._car_z_hist[2] = [(100 + k, 20.0 + 0.5 * k) for k in range(8)]
    assert e._z_regress_t(2) is None
    # 历史太短/跨度不足：不给
    e._car_z_hist[3] = [(100, 40.0), (101, 37.0), (102, 34.0)]
    assert e._z_regress_t(3) is None
    # 无历史的 id：不给
    assert e._z_regress_t(99) is None


# 20261109_184127 p2 车22 的发布 z 序列（fid, 米）；框高比桥接下的真实
# 阶梯：0.4s 内 49.85→57.93 回跳、50.2 平台三拍，单对差分与最小二乘都会被
# 带偏。这段锚点 v_close 报 1.46 m/s（t≈36s），实测闭速 ~20 m/s。
_BOX_BRIDGE_Z_SERIES = (
    (7668, 58.15), (7670, 53.68), (7672, 49.85), (7674, 57.93),
    (7676, 53.79), (7678, 50.20), (7679, 50.20), (7681, 50.20),
    (7683, 52.80), (7686, 52.77), (7687, 50.78), (7690, 51.63),
    (7692, 49.73), (7693, 49.72), (7696, 42.85), (7698, 42.83),
    (7699, 42.82), (7701, 40.31), (7703, 39.19), (7705, 35.65),
)


def test_z_regression_survives_box_bridge_steps():
    """桥接阶梯不得让到站时间退回几十秒（fid 7633~7816 帧证）：50m 处
    稳健斜率必须给出与真值同量级（1.8~2.6s）的到站时间，而不是 36s；
    平台段的估值上限也远低于旧的 20~36s 量级。"""
    e = _eng()
    e._fps = 38.0
    for fid in (7681, 7683, 7690, 7696, 7701, 7703, 7705):
        e._car_z_hist[22] = [p for p in _BOX_BRIDGE_Z_SERIES if p[0] <= fid]
        t = e._z_regress_t(22)
        assert t is not None
        assert t < 10.0, (fid, t)      # 旧口径同段报 26~36s
    e._car_z_hist[22] = [p for p in _BOX_BRIDGE_Z_SERIES if p[0] <= 7681]
    assert e._z_regress_t(22) == pytest.approx(2.16, abs=0.25)   # z=50.2m
    e._car_z_hist[22] = list(_BOX_BRIDGE_Z_SERIES)
    assert e._z_regress_t(22) == pytest.approx(2.13, abs=0.25)   # z=35.65m
    # 6 拍（< 最少样本 7）：不给提前量
    e._car_z_hist[23] = list(_BOX_BRIDGE_Z_SERIES[:6])
    assert e._z_regress_t(23) is None


def test_box_bridged_z_does_not_reset_anchor_baseline():
    """桥接 z 只平滑，不进锚点跳变门（2026-11-09）：8m 阶梯不得把 x 平滑
    重新起跑，也不得清空纵深历史；锚点口径的大跳变仍按换锚重置。"""
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)

    def tick(z, x_m, box, fid):
        e._fid = fid
        e._views = (_cv(tid=1, x=x_m / e.cal.lane_w_m, cy=620, rel=8.0),)
        e._metric_cars = (MetricCarState(
            target_id=1, source=DistanceSource.ANCHOR, z_m=z, x_m=x_m,
            v_close_mps=20.0, t_meet_s=z / 20.0, anchor_age_ms=50.0,
            z_box_bridged=box),)
        e._reach_car_sources(0.0)

    e = _eng()
    e._fps = 20.0
    tick(50.0, 0.0, True, 100)
    tick(42.0, 1.0, True, 101)         # 8m 桥接跳变 + 1m 横移
    assert e._car_xf[1] == pytest.approx(0.35, abs=0.03)   # EMA，不重锚
    assert len(e._car_z_hist[1]) == 2                      # 历史不清空
    assert 1 not in e._car_za            # 桥接值不进锚点基线
    tick(39.0, 1.0, False, 102)        # 锚点口径、3m 正常移动
    assert e._car_za[1] == pytest.approx(39.0)
    assert len(e._car_z_hist[1]) == 3
    tick(30.0, 2.0, False, 103)        # 锚点口径 9m 跳变=换锚 → x 重锚
    assert e._car_xf[1] == pytest.approx(2.0)
    assert e._car_za[1] == pytest.approx(30.0)


def test_route_ttc_horizon_covers_overtake_threshold(monkeypatch):
    """路线时间面视界必须盖过超车门（执行层横移时间换算，≈3.4s）：否则
    窗外的车永远进不了表，提前超车看不到（旧 2.0s 视界把它截成 1~2s）。"""
    from types import SimpleNamespace as NS
    import maaracing_master.plugins.speedrush.decision as dmod
    from maaracing_master.plugins.speedrush.route import overtake_need_s

    seen = {}

    def fake_map(scene, c0, horizon_s):
        seen["h"] = horizon_s
        return {}

    monkeypatch.setattr(dmod, "_lane_ttc_map", fake_map)
    e = _eng()
    e._scene = NS(lane_w_m=3.5, objs=())
    e._tick_route(c0=0.0, executed=0.0)
    assert seen["h"] == pytest.approx(
        max(e.cfg.motion.horizon_s, overtake_need_s(e._route_params)))


def test_pass_x_unknown_marks_jump_zreset_and_gate():
    """横位未知判据（2026-11-10）：发布横位相邻跳 >pass_x_unknown_jump_m
    或锚点口径换锚（z_reset）的拍标 x_unknown；正常 EMA 横移不误标；
    开关关恒 False（逐位回今天）。"""
    import dataclasses
    from maaracing_master.plugins.speedrush.tracking import (
        DistanceSource, MetricCarState)

    def mc(z, x):
        return (MetricCarState(target_id=1, source=DistanceSource.ANCHOR,
                               z_m=z, x_m=x, v_close_mps=-10.0,
                               t_meet_s=None, anchor_age_ms=0.0),)

    def eng(on, ema=1.0, jmax=0.0):
        return DecisionEngine(dataclasses.replace(
            CFG,
            traffic=dataclasses.replace(
                CFG.traffic, metric_x_ema_alpha=ema,
                metric_x_jump_max_m=jmax),
            selection=dataclasses.replace(
                CFG.selection, pass_x_unknown_enabled=on)))

    def _no_ruler_view():
        # x_lane=None：横向互证无像素证人，x 直接吃 MetricCarState.x_m
        return (CarView(id=1, x_lane=None, cy=500, rel_approach=20.0,
                        d_min=0.0, age_ticks=10),)

    # 新轨首拍即换锚（无纵深历史）→ 标：邻道贴边要到站时间，首拍本就
    # 进不了候选，语义是「横位无历史不可信」
    e = eng(True)
    e._views = _no_ruler_view()
    e._metric_cars = mc(20.0, 0.0)
    assert e._reach_car_sources(0.0)[0].x_unknown is True
    # 拍 2：z 连续、发布横位跳 3.5m >2.8m 门 → 标
    e._metric_cars = mc(20.0, 3.5)
    assert e._reach_car_sources(0.0)[0].x_unknown is True
    # 拍 3：读数回到连续 → 重新武装不标
    e._metric_cars = mc(20.0, 3.5)
    assert e._reach_car_sources(0.0)[0].x_unknown is False

    # 锚点口径换锚（z 跳 8m >6m）即使横位只跳 0.3m 也标（弯道修正项会跳）
    e2 = eng(True)
    e2._views = _no_ruler_view()
    e2._metric_cars = mc(20.0, 0.0)
    e2._reach_car_sources(0.0)
    e2._metric_cars = mc(28.0, 0.3)
    assert e2._reach_car_sources(0.0)[0].x_unknown is True

    # 生产 EMA（0.35）下的正常横移 1m：EMA 后 0.35m ≤门，不误标
    e3 = eng(True, ema=0.35, jmax=1.5)
    e3._views = _no_ruler_view()
    e3._metric_cars = mc(20.0, 0.0)
    e3._reach_car_sources(0.0)
    e3._metric_cars = mc(20.0, 1.0)
    assert e3._reach_car_sources(0.0)[0].x_unknown is False

    # 开关关：跳超限也不标（逐位回今天）
    e4 = eng(False)
    e4._views = _no_ruler_view()
    e4._metric_cars = mc(20.0, 0.0)
    e4._reach_car_sources(0.0)
    e4._metric_cars = mc(20.0, 3.5)
    assert e4._reach_car_sources(0.0)[0].x_unknown is False


def test_lattice_phase_puts_candidates_on_lane_centers():
    """候选格相位：线网格里分隔线在整数位，道中心在半整数位。

    路心读数（road_offset）与线网格横位（lane_x）之差就是路心在道 0 线上的
    位置。路心落在分隔线（相位 0）时偏移半道，落在道中心（相位 ±0.5）时
    偏移 0——后者逐位回旧整数格。
    """
    from maaracing_master.plugins.speedrush.decision import lattice_phase_step

    ph, shift = lattice_phase_step(None, 0.0, 0.0)      # 路心=分隔线
    assert ph == pytest.approx(0.0) and shift == pytest.approx(0.5)
    ph, shift = lattice_phase_step(None, 0.5, 0.0)      # 路心=道中心
    assert ph == pytest.approx(-0.5) and shift == pytest.approx(0.0)
    # 无证据拍：相位保留、偏移归零（不猜）
    assert lattice_phase_step(0.12, None, 0.3) == (0.12, 0.0)
    assert lattice_phase_step(0.12, 0.4, None) == (0.12, 0.0)


def test_lattice_phase_filters_tick_noise():
    """单拍相位抖动被 EMA 吃掉：偏移不跟着逐拍翻。"""
    from maaracing_master.plugins.speedrush.decision import lattice_phase_step

    prev = None
    shifts = []
    for lane_x in (0.0, 0.30, 0.05, 0.25, 0.0, 0.20):
        prev, shift = lattice_phase_step(prev, lane_x, 0.0)
        shifts.append(shift)
    assert max(abs(b - a) for a, b in zip(shifts, shifts[1:])) < 0.15
