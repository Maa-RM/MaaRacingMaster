# -*- coding: utf-8 -*-
"""无限驾驶流程模块的配置面与录制态回归锁（离线，ctx=None）。

覆盖：录制开关的配置读写往返、任务卡摘要、录制态放宽启动约束（虚拟手柄能力 /
手柄独占）、人工接管期声明只落驾驶阶段、配置表单字段键都在默认配置里、
录制状态行跟随录制器实况、录制开关仅在源码模式暴露、直线档不保留录制开启态。
"""

from __future__ import annotations

import time

from maaracing_master import SOURCE_MODE
from maaracing_master.core.config_ui_contract import (
    CONFIG_UI_CONTRACT_VERSION,
    UI_OK,
    normalize_config_ui,
)
from maaracing_master.plugins.infinite_drive import flow_data
from maaracing_master.plugins.infinite_drive.module import (
    REC_DRIVE_FPS,
    REC_NAV_FPS,
    InfiniteDriveModule,
    _DRIVE_STAGES,
    _best_card_slot,
    _build_config_ui,
    _record_fps,
)


def _module() -> InfiniteDriveModule:
    return InfiniteDriveModule(None)


def test_record_mode_config_roundtrip() -> None:
    m = _module()
    assert m.get_module_config()["record_mode"] is False
    m.set_module_config({"drive_mode": "depth"})
    out = m.set_module_config({"record_mode": True})
    assert out["record_mode"] is True
    assert m.get_module_config()["record_mode"] is True


def test_straight_drive_clears_record_mode() -> None:
    """直线档不许残留录制开启态：写入、读回、启动约束三处口径一致。"""
    m = _module()
    m.set_module_config({"drive_mode": "depth", "record_mode": True})
    assert m.get_module_config()["record_mode"] is True
    # 开着录制切回直线档：录制态被撤销。
    out = m.set_module_config({"drive_mode": "straight"})
    assert out["record_mode"] is False
    assert m.get_module_config()["record_mode"] is False
    # 直线档下直接写开录制也被归零（core 归一化会做同样的事）。
    assert m.set_module_config({"record_mode": True})["record_mode"] is False

    straight_on = {"drive_mode": "straight", "record_mode": True}
    assert InfiniteDriveModule.normalize_config(straight_on)["record_mode"] is False
    # 直线档归一化后录制按关处理，摘要回落到驾驶档位标签（带连刷轮数）。
    assert InfiniteDriveModule.config_summary(straight_on) == \
        ["驾驶: 全程直行", "连刷不限"]
    assert InfiniteDriveModule.required_capabilities(straight_on) == \
        InfiniteDriveModule.REQUIRES
    assert InfiniteDriveModule.requires_exclusive_gamepad(straight_on) is True

    # 自动驾驶档录制仍生效（回归锁：新判据不能误伤正常录制局）。
    depth_on = {"drive_mode": "depth", "record_mode": True}
    assert InfiniteDriveModule.config_summary(depth_on) == ["录制: 开", "连刷不限"]
    assert InfiniteDriveModule.required_capabilities(depth_on) == frozenset({"capture"})
    assert InfiniteDriveModule.requires_exclusive_gamepad(depth_on) is False


def test_set_module_config_ignores_non_dict_and_unknown_keys() -> None:
    m = _module()
    m.set_module_config("not-a-dict")  # type: ignore[arg-type]
    m.set_module_config({"unrelated": 1})
    assert m.get_module_config()["record_mode"] is False


def test_config_summary_announces_drive_mode_unless_recording() -> None:
    """录制开只报录制；录制关报当前驾驶档位（直行档同样有标签，不留空）。"""
    assert InfiniteDriveModule.config_summary(
        {"drive_mode": "depth", "record_mode": True}) == ["录制: 开", "连刷不限"]
    assert InfiniteDriveModule.config_summary(
        {"drive_mode": "depth", "record_mode": False}) == ["驾驶: 自动驾驶", "连刷不限"]
    assert InfiniteDriveModule.config_summary(
        {"drive_mode": "straight", "record_mode": False}) == ["驾驶: 全程直行", "连刷不限"]


def test_round_total_normalize_clamps_and_coerces() -> None:
    """轮数归一：0 = 不限，负数/垃圾回落默认，上限 99，字符串折算整数。"""
    normalize = InfiniteDriveModule.normalize_config
    assert normalize({"round_total": 0})["round_total"] == 0
    assert normalize({"round_total": 5})["round_total"] == 5
    assert normalize({"round_total": -3})["round_total"] == 0
    assert normalize({"round_total": 100})["round_total"] == 99
    assert normalize({"round_total": "7"})["round_total"] == 7
    assert normalize({"round_total": "垃圾"})["round_total"] == 0
    assert normalize({"round_total": None})["round_total"] == 0
    assert normalize({})["round_total"] == 0


def test_round_total_config_roundtrip() -> None:
    """轮数读写往返：实例读写与越界钳制，非法载荷不影响已有值。"""
    m = _module()
    assert m.get_module_config()["round_total"] == 0
    assert m.set_module_config({"round_total": 5})["round_total"] == 5
    assert m.get_module_config()["round_total"] == 5
    assert m.set_module_config({"round_total": 999})["round_total"] == 99
    m.set_module_config("not-a-dict")  # type: ignore[arg-type]
    assert m.get_module_config()["round_total"] == 99


def test_config_summary_includes_round_total() -> None:
    """标题栏摘要直读本次设置：连刷 N 轮 / 连刷不限。"""
    assert InfiniteDriveModule.config_summary(
        {"drive_mode": "depth", "round_total": 5}) == ["驾驶: 自动驾驶", "连刷 5 轮"]
    assert InfiniteDriveModule.config_summary(
        {"drive_mode": "depth", "record_mode": True, "round_total": 3}) == \
        ["录制: 开", "连刷 3 轮"]


def test_round_total_field_carries_estimate_only_with_history(monkeypatch) -> None:
    """预计用时只在有历史依据时下发：轮均间隔来自战绩库，None 不冒充有数。"""
    from maaracing_master.plugins.infinite_drive import dashboard_store

    monkeypatch.setattr(dashboard_store, "median_round_seconds", lambda: 152.4)
    field = _ui_fields(True)["round_total"]
    assert field["type"] == "stepper" and field["max"] == 99
    assert field["suffix"] == "轮"
    assert field["unit_seconds"] == 152
    monkeypatch.setattr(dashboard_store, "median_round_seconds", lambda: None)
    assert "unit_seconds" not in _ui_fields(True)["round_total"]


class _AllOkGraph:
    """所有锚点/动作都成功的图桩：_run_flow 的轮次推进纯看桩返回。

    只读探测一律 MISS（页面判定类分支不误命中），需要命中的测试自行覆盖 probe。
    """

    def run(self, *args, **kwargs) -> bool:
        return True

    def probe(self, *_args, **_kwargs):
        from maaracing_master.core.nav_graph import ProbeOutcome

        return ProbeOutcome.MISS


def _stub_flow_steps(monkeypatch, mod) -> None:
    monkeypatch.setattr(mod, "_pick_cards", lambda phase: True)
    monkeypatch.setattr(mod, "_drive", lambda phase: True)
    monkeypatch.setattr(mod, "_run_step", lambda node, stage: True)
    monkeypatch.setattr(mod, "_read_level", lambda: 1)


def test_run_flow_stops_after_round_total(monkeypatch) -> None:
    """刷满设定轮数即正常收尾；0 = 不限维持连刷（停止信号仍优先）。"""
    m = _module()
    m._running = True
    m._graph = _AllOkGraph()
    m._round_total = 2
    seen: list[str] = []
    monkeypatch.setattr(m, "_pick_cards", lambda phase: seen.append(phase) or True)
    monkeypatch.setattr(m, "_drive", lambda phase: seen.append(phase) or True)
    monkeypatch.setattr(m, "_run_step", lambda node, stage: True)
    monkeypatch.setattr(m, "_read_settlement", lambda: seen.append("settle"))
    monkeypatch.setattr(m, "_read_level", lambda: 1)
    assert m._run_flow(0) is True
    assert seen.count("settle") == 2 and m._round_no == 2

    # 不限档：不因轮数停（由停止信号收）；这里用桩在第 3 轮后给停止信号
    m2 = _module()
    m2._running = True
    m2._graph = _AllOkGraph()
    m2._round_total = 0
    _stub_flow_steps(monkeypatch, m2)
    counter = {"n": 0}

    def _settle() -> None:
        counter["n"] += 1
        if counter["n"] >= 3:
            m2._running = False

    monkeypatch.setattr(m2, "_read_settlement", _settle)
    assert m2._run_flow(0) is True
    assert counter["n"] == 3


def test_drive_mode_roundtrip_and_legacy_default() -> None:
    m = _module()
    assert m.get_module_config()["drive_mode"] == "straight"
    assert m.set_module_config({"drive_mode": "depth"})["drive_mode"] == "depth"
    assert m.set_module_config({"record_mode": True})["drive_mode"] == "depth"
    assert m.set_module_config({"drive_mode": "macro"})["drive_mode"] == "straight"


def test_record_mode_relaxes_startup_constraints() -> None:
    """录制态不要求虚拟手柄能力、不独占手柄——验证轮驾驶阶段由用户物理手柄驾驶。"""
    on = {"drive_mode": "depth", "record_mode": True}
    assert InfiniteDriveModule.required_capabilities(on) == frozenset({"capture"})
    assert InfiniteDriveModule.required_capabilities(
        {"drive_mode": "depth", "record_mode": False}) == InfiniteDriveModule.REQUIRES
    assert InfiniteDriveModule.required_capabilities(None) == InfiniteDriveModule.REQUIRES
    assert InfiniteDriveModule.requires_exclusive_gamepad(on) is False
    assert InfiniteDriveModule.requires_exclusive_gamepad(None) is \
        InfiniteDriveModule.REQUIRES_GAMEPAD_EXCLUSIVE


def test_config_ui_fields_are_declared_keys() -> None:
    """表单字段键必须在默认配置里——宿主按默认值渲染，漂移键渲染不出。"""
    fields = [f for group in InfiniteDriveModule.CONFIG_UI["groups"]
              for f in group["fields"]]
    assert fields, "配置表单不应为空"
    for field in fields:
        assert field["key"] in InfiniteDriveModule.DEFAULT_MODULE_CONFIG


def _ui_fields(source_mode: bool) -> dict[str, dict]:
    ui = _build_config_ui(source_mode)
    return {f["key"]: f for group in ui["groups"] for f in group["fields"]}


def test_record_switch_only_declared_in_source_mode() -> None:
    """录制开关只在源码模式声明；正式包连字段都不下发，前端无从渲染。"""
    source = _ui_fields(True)
    assert set(source) == {"drive_mode", "record_mode", "round_total"}
    # 显隐由驾驶模式决定：直线行驶档不显示录制，其余档显示；驾驶档自身常显。
    assert source["record_mode"]["visible_if"] == {
        "key": "drive_mode", "op": "not_equals", "value": "straight"}
    assert "visible_if" not in source["drive_mode"]

    packaged = _ui_fields(False)
    assert set(packaged) == {"drive_mode", "round_total"}
    # 裁掉录制字段后不留悬空条件，契约校验才不会降级。
    assert "visible_if" not in packaged["drive_mode"]

    # 类属性与构建器同一批字段键。不做全量相等：CONFIG_UI 会被结算落库重建
    # （unit_seconds 随战绩库变化），键集稳定即可。
    def _keys(groups: list) -> set:
        return {f["key"] for g in groups for f in g["fields"]}

    assert _keys(InfiniteDriveModule.CONFIG_UI["groups"]) == \
        _keys(_build_config_ui(SOURCE_MODE)["groups"])


def test_drive_labels_and_record_copy() -> None:
    """档位改名与录语文案是给用户看的，锁住实际下发的字符串。"""
    drive = _ui_fields(True)["drive_mode"]
    assert [o["label"] for o in drive["options"]] == ["全程直行", "自动驾驶"]
    record = _ui_fields(True)["record_mode"]
    assert record["label"] == "录制人工语料"
    assert "程序不会操作车辆" in record["description"]
    assert "自动驾驶仪的影子数据" in record["description"]


def test_both_ui_variants_normalize_cleanly() -> None:
    """两种模式都过宿主契约校验：正常态 UI_OK 且无告警（回归锁）。"""
    for source_mode in (True, False):
        class _Probe:
            CONFIG_UI_CONTRACT = CONFIG_UI_CONTRACT_VERSION
            CONFIG_UI = _build_config_ui(source_mode)

        result = normalize_config_ui(
            _Probe, set(InfiniteDriveModule.DEFAULT_MODULE_CONFIG))
        assert result.state == UI_OK, (source_mode, result.warnings)
        assert result.warnings == ()
        assert "drive_mode" in {f["key"] for g in result.schema["groups"]
                                for f in g["fields"]}


def test_manual_input_active_scoped_to_drive_stage() -> None:
    """人工接管期只落驾驶阶段且只在录制态：UI 步进仍是程序操作期。"""
    m = _module()
    m.set_module_config({"drive_mode": "depth", "record_mode": True})
    m._recorder = object()  # 只判在场，不读其状态
    m._stage_name = "驾驶·阶段1"
    assert m.manual_input_active() is True
    m._stage_name = "驾驶·阶段2"
    assert m.manual_input_active() is True
    m._stage_name = "入口·比赛"
    assert m.manual_input_active() is False
    m._recorder = None
    assert m.manual_input_active() is False
    m.set_module_config({"record_mode": False})
    m._stage_name = "驾驶·阶段1"
    assert m.manual_input_active() is False


def test_record_fps_aligned_with_v1_nav_and_drive() -> None:
    """录制帧率对齐 v1：导航/UI 段 3fps、驾驶段 10fps（用户权威口径）。"""
    assert REC_NAV_FPS == 3.0
    assert REC_DRIVE_FPS == 10.0
    # 每个驾驶阶段都是 10fps
    for stage in _DRIVE_STAGES:
        assert _record_fps(stage) == REC_DRIVE_FPS
    # UI 导航段与未进入任何阶段（None）都是 3fps
    assert _record_fps(None) == REC_NAV_FPS
    for stage in ("入口·比赛", "关卡挑战", "卡牌·阶段1", "结算·下一关", "放弃·确认"):
        assert _record_fps(stage) == REC_NAV_FPS


def test_config_status_tracks_recorder_liveness() -> None:
    """状态行跟随录制器实况：未起/已停返回 None，不冒充在录。"""

    class _FakeRec:
        running = True
        stats = {"frames_written": 120, "frames_dropped": 3,
                 "hud_written": 0, "write_errors": 0, "pad_samples": 0}

    m = _module()
    assert m.config_status() is None
    m._recorder = _FakeRec()
    status = m.config_status()
    assert status is not None
    assert status["level"] == "warn"
    assert "120" in status["text"] and "丢帧 3" in status["text"]


# ---------- 步级重发策略（_run_step） ----------

class _StubLifecycle:
    running = True

    @staticmethod
    def sleep(_seconds: float) -> bool:
        return True


class _StubCtx:
    lifecycle = _StubLifecycle()


def _step_module(graph) -> InfiniteDriveModule:
    m = InfiniteDriveModule(None)
    m.ctx = _StubCtx()
    m._running = True
    m._graph = graph
    return m


class _SwallowedClickGraph:
    """点击「送达且 settle 报已生效」但落地确认永不出现（真机 2026-11-21 场景）。"""

    def __init__(self) -> None:
        self.clicks = 0
        self.last_click_landed = None
        self.last_click_sent = False

    def run(self, entry: str, _reached: str | None, **kwargs) -> bool:
        if entry == "infinite_drive.点比赛":
            self.clicks += 1
            self.last_click_sent = True
            self.last_click_landed = True  # settle 误报：抖动帧分数掉到阈值下
            return True
        return False  # 落地确认（比赛页锚点）永不命中


def test_run_step_resends_despite_settle_landed(monkeypatch) -> None:
    """settle 报「已生效」而落地页一直不来时必须重发点击，不得只等不点。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_WINDOW_S", 0.05)
    monkeypatch.setattr(m, "STEP_POLL_S", 0.001)
    graph = _SwallowedClickGraph()
    ok = _step_module(graph)._run_step("infinite_drive.点比赛", "入口·比赛")

    assert ok is False                              # 预算耗尽显式失败
    assert graph.clicks == 1 + m.STEP_MAX_RETRIES   # 首击 + 全部重发（旧策略停在 1）


def test_run_step_resend_recovers_when_page_arrives(monkeypatch) -> None:
    """重发后落地页出现 → 步骤成功；settle 误报不阻塞真正的落地确认。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_WINDOW_S", 0.05)
    monkeypatch.setattr(m, "STEP_POLL_S", 0.001)

    class _LateLandingGraph(_SwallowedClickGraph):
        def run(self, entry: str, _reached: str | None, **kwargs) -> bool:
            if entry == "infinite_drive.点比赛":
                super().run(entry, _reached)
                return True
            return self.clicks >= 2  # 第 2 次点击后落地页才到

    graph = _LateLandingGraph()
    ok = _step_module(graph)._run_step("infinite_drive.点比赛", "入口·比赛")

    assert ok is True
    assert graph.clicks == 2


# ---------- 选卡等待循环的事件介绍页驱赶 ----------

class _EventPageGraph:
    """事件介绍页先盖住选卡页，点一次空白后让位（真机每回合阶段2场景）。"""

    def __init__(self) -> None:
        self.event_checks = 0
        self.dismiss_clicks = 0
        self.card_checks = 0

    def run(self, entry: str, _reached: str | None = None, **kwargs) -> bool:
        from maaracing_master.plugins.infinite_drive import module as m

        if entry == m._EVENT_ANCHOR:
            self.event_checks += 1
            return self.dismiss_clicks == 0  # 未关时事件页一直盖着
        if entry == m._EVENT_DISMISS:
            self.dismiss_clicks += 1
            return True
        if entry == m._CARD_ANCHOR:
            self.card_checks += 1
            return self.dismiss_clicks > 0   # 关掉后选卡页才可见
        return False


def test_wait_card_anchor_dismisses_event_page() -> None:
    """事件页在位 → 点空白 → 选卡页出现；点击只在页面在位时发生。"""
    graph = _EventPageGraph()
    ok = _step_module(graph)._wait_card_anchor(want_present=True)

    assert ok is True
    assert graph.dismiss_clicks == 1
    assert graph.event_checks >= 1 and graph.card_checks >= 1


def test_wait_card_anchor_skips_dismiss_when_page_absent() -> None:
    """事件页不在位（或已关）→ 不发任何空白点击，直接等选卡页。"""
    from maaracing_master.plugins.infinite_drive import module as m

    class _NoEventGraph(_EventPageGraph):
        def run(self, entry: str, _reached: str | None = None, **kwargs) -> bool:
            if entry == m._EVENT_ANCHOR:
                self.event_checks += 1
                return False
            if entry == m._CARD_ANCHOR:
                self.card_checks += 1
                return True
            return False

    graph = _NoEventGraph()
    ok = _step_module(graph)._wait_card_anchor(want_present=True)

    assert ok is True
    assert graph.dismiss_clicks == 0


# ---------- 选卡循环：点一槽等商店自关，没关就重选重发 ----------

class _ShopGraph:
    """选卡商店桩：close_after_clicks 次点击后商店自关（锚点消失）。

    blink_after_first：第一次点击后先闪失两拍再重现（候选刷新动画），验证
    「持续不在」判据不把闪失误判成关闭。
    """

    def __init__(self, close_after_clicks: int = 1, blink_after_first: bool = False):
        from maaracing_master.plugins.infinite_drive import module as m

        self._m = m
        self.clicks = 0
        self.anchor_checks = 0
        self.close_after = close_after_clicks
        self.blink = blink_after_first
        self._blink_seen = 0

    def run(self, entry: str, _reached: str | None = None, **kwargs) -> bool:
        if entry == self._m._EVENT_ANCHOR:
            return False
        if entry.startswith("infinite_drive.选卡.r"):
            self.clicks += 1
            return True
        if entry == self._m._CARD_ANCHOR:
            self.anchor_checks += 1
            if self.clicks >= self.close_after:
                return False  # 商店已自关
            if self.blink and self.clicks >= 1:
                self._blink_seen += 1
                return self._blink_seen > 2  # 前两拍闪失，其后重现
            return True
        return False


def _pick_module(graph, monkeypatch):
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_POLL_S", 0.01)
    monkeypatch.setattr(m, "PICK_CLOSE_WINDOW_S", 0.3)
    monkeypatch.setattr(m, "PICK_CLOSE_QUIET_S", 0.05)
    monkeypatch.setattr(InfiniteDriveModule, "_choose_card_slot", lambda self: 0)
    return _step_module(graph)


def test_pick_cards_resends_swallowed_click(monkeypatch) -> None:
    """第 1 次点击被吞（商店不关）→ 重发第 2 次 → 商店自关收步。"""
    graph = _ShopGraph(close_after_clicks=2)
    ok = _pick_module(graph, monkeypatch)._pick_cards("card2")

    assert ok is True
    assert graph.clicks == 2


def test_pick_cards_refresh_blink_not_mistaken_as_close(monkeypatch) -> None:
    """候选刷新让锚点闪失两拍：不算关闭，重发后由真实关闭收步。"""
    graph = _ShopGraph(close_after_clicks=2, blink_after_first=True)
    ok = _pick_module(graph, monkeypatch)._pick_cards("card2")

    assert ok is True
    assert graph.clicks == 2


def test_pick_cards_budget_exhausted_fails_explicitly(monkeypatch) -> None:
    """商店一直不关（点击全被吞）：预算耗尽显式失败，不多点。"""
    graph = _ShopGraph(close_after_clicks=99)
    ok = _pick_module(graph, monkeypatch)._pick_cards("card1")

    assert ok is False
    assert graph.clicks == 3  # card1 预算 = max_picks(1) + PICK_RESEND_BUDGET(2)


# ---------- 卡面名优先级表（离线仿真） ----------

def test_card_priority_covers_released_pool() -> None:
    """94 张正式卡全部有唯一秩；末档是脚本不主动走的极限变道。"""
    ranks = list(flow_data.CARD_PRIORITY.values())
    assert len(ranks) == 94
    assert len(set(ranks)) == 94
    assert min(ranks) == 0 and max(ranks) == 93
    assert set(flow_data.CARD_PRIORITY) == {
        name for tier in flow_data.CARD_PRIORITY_TIERS for name in tier
    }
    assert flow_data.CARD_PRIORITY["冲击波"] == 0
    assert flow_data.CARD_PRIORITY["距离掌控"] == 92
    assert flow_data.CARD_PRIORITY["平移底盘"] == 93


def test_card_priority_follows_approved_order() -> None:
    """退场/清障在前，之后依次是冲刺、幽灵/失重、速度、闪烁、金币、跳跃、飞行。"""
    p = flow_data.CARD_PRIORITY
    assert p["冲击波"] < p["无敌"] < p["弹射"] < p["冲刺"]
    assert p["变道冲击波"] < p["冲刺"] and p["变道弹射"] < p["冲刺"]
    assert p["加速回馈"] < p["冲刺"]
    assert p["冲刺"] < p["幽灵化"] < p["极速"] < p["闪烁"]
    assert p["闪烁"] < p["金币掉落"] < p["跳跃"] < p["飞行"] < p["距离掌控"]


def test_best_card_slot_from_real_shop_frames() -> None:
    """调试录像三帧的实际 OCR 结果：取秩最高的槽，读不出回退 0。"""
    assert _best_card_slot(["冲击波", "极速", "加速"]) == 0
    assert _best_card_slot(["加速", "冲刺", "变道冲击波"]) == 2
    assert _best_card_slot(["幽灵化", "闪烁", "弹射"]) == 2
    assert _best_card_slot(["飞行", "金币掉落", "跳跃"]) == 1
    assert _best_card_slot(["冲 击 波", "极速", "加速"]) == 0
    assert _best_card_slot(["", "", ""]) == 0
    assert _best_card_slot(["未知甲", "未知乙", "未知丙"]) == 0


# ---------- 偶发弹窗 OCR 兜底（限频） ----------

class _SweepTrackingGraph(_SwallowedClickGraph):
    """记录兜底节点被查询的次数；end 信号可脚本化。"""

    def __init__(self, end_hits: bool = False):
        super().__init__()
        self.sweeps = 0
        self.end_hits = end_hits

    def run(self, entry: str, _reached: str | None = None, **kwargs) -> bool:
        from maaracing_master.plugins.infinite_drive import module as m

        if entry == m._ADVANCE_DISMISS:
            self.sweeps += 1
            return True
        if entry == m._DRIVE_END["p2"]:
            return self.end_hits
        return super().run(entry, _reached)


def test_drive_wait_sweeps_transient_popup(monkeypatch) -> None:
    """驾驶等待期兜底按限频运行：结束信号不来也会扫弹窗，且循环照常到点收步。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_POLL_S", 0.02)
    monkeypatch.setattr(m, "ADVANCE_SWEEP_INTERVAL_S", 0.1)
    monkeypatch.setattr(m, "_DRIVE_DURATION", {"p1": 0.1, "p2": 0.1})
    monkeypatch.setattr(m.flow_data, "DRIVE_END_MARGIN_S", 0.1)
    graph = _SweepTrackingGraph()
    m = _step_module(graph)
    m.set_module_config({"drive_mode": "depth", "record_mode": True})

    ok = m._drive("p2")

    assert ok is False            # 结束信号始终没来，到点显式失败
    assert graph.sweeps >= 2      # 兜底确实在跑


def test_program_drive_only_commands_pad_while_driving(monkeypatch) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "_DRIVE_HUD_GAP_S", 0.0)
    drive_frame, menu_frame = object(), object()
    frames = iter([(menu_frame, 1, 1, 0), (drive_frame, 2, 2, 0),
                   (drive_frame, 3, 3, 300), (menu_frame, 4, 4, 0)])
    commands = []
    controls = []
    throttle = 0
    steer = 0

    def joystick(x_value=0, y_value=0):
        nonlocal steer
        steer = x_value

    def trigger(value=0):
        nonlocal throttle
        throttle = value

    @contextmanager
    def acquire():
        yield SimpleNamespace(left_joystick=joystick, right_trigger=trigger,
                              update=lambda: commands.append((steer, throttle)))
        commands.append((0, 0))

    def run(entry, _reached, **kwargs):
        # 导航识别与弹窗不得在持有驾驶输入时运行。
        controls.append(list(commands))
        return entry == m._DRIVE_END["p2"] and len(controls) >= 3

    monkeypatch.setattr(m, "drive_hud_visible", lambda frame: frame is drive_frame,
                        raising=False)
    mod = _step_module(SimpleNamespace(run=run))
    mod.ctx.capture = SimpleNamespace(frame_with_age=lambda: next(frames))
    mod.ctx.gamepad = SimpleNamespace(acquire=acquire)

    assert mod._drive("p2") is True
    assert controls[0] == []
    assert commands[0][0] == 0 and commands[0][1] > 0
    assert all(command == (0, 0) for command in commands[1:])
    assert controls[-1][-1] == (0, 0)


def test_lane_mode_switches_camera_once_at_first_hud_in_p1_and_drives_both_phases(monkeypatch) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "_DRIVE_HUD_GAP_S", 0.0)
    drive_frame = object()
    frames = iter([(object(), 0, 0, 0), (drive_frame, 1, 1, 0), (drive_frame, 2, 2, 0), (object(), 3, 3, 0),
                   (drive_frame, 4, 4, 0), (object(), 5, 5, 0)])
    directions = []
    throttles = []
    armed = []
    detected = []
    camera = []
    pad = SimpleNamespace(left_joystick=lambda x_value: directions.append(x_value),
                          right_trigger=lambda value: throttles.append(value), update=lambda: None)
    monkeypatch.setattr(m, "drive_hud_visible", lambda frame: frame is drive_frame,
                        raising=False)
    menu_ticks = iter([False, True, True])
    mod = _step_module(SimpleNamespace(run=lambda *args: next(menu_ticks)))
    mod.set_module_config({"drive_mode": "depth"})
    monkeypatch.setattr(mod, "_switch_drive_view", lambda pad: camera.append(len(detected)) or True)
    monkeypatch.setattr(mod, "_sweep_transient_popup", lambda: None)
    mod.ctx.capture = SimpleNamespace(frame_with_age=lambda: next(frames))
    mod.ctx.gamepad = SimpleNamespace(acquire=lambda: nullcontext(pad))

    def steer(frame, fid, timestamp):
        detected.append((frame, fid, timestamp))
        mod._lane_drive.throttle_scale = 0.0 if fid == 1 else 1.0
        return 0.2

    mod._lane_drive = SimpleNamespace(begin=lambda **options: armed.append(options), steer=steer,
                                       release=lambda: None)
    assert mod._drive("p1") and mod._drive("p2")
    assert camera == [0]
    assert len(armed) == 2
    assert detected == [(drive_frame, fid, fid) for fid in (1, 2, 4)]
    assert directions[0] == directions[1] == directions[2] > 0
    assert throttles == [0, m._DRIVE_THROTTLE, m._DRIVE_THROTTLE]


def test_view_switch_releases_camera_button(monkeypatch) -> None:
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    events = []
    pad = SimpleNamespace(press_button=lambda button: events.append(("press", button)),
                          release_button=lambda button: events.append(("release", button)),
                          update=lambda: events.append(("update",)))
    mod = _step_module(None)
    mod.ctx.lifecycle = SimpleNamespace(sleep=lambda _seconds: False)
    monkeypatch.setattr(m, "_camera_button", lambda: object(), raising=False)

    assert mod._switch_drive_view(pad) is False
    assert [event[0] for event in events] == ["press", "update", "release", "update"]
    assert events[0][1] is events[2][1]


def test_program_drive_does_not_run_ocr_on_brief_hud_loss(monkeypatch) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    clock = 10.0
    monkeypatch.setattr(m.time, "monotonic", lambda: clock)
    monkeypatch.setattr(m, "drive_hud_visible", bool)
    frames = iter([True, False, True])
    fid = 0
    acted = []

    def frame_with_age():
        nonlocal fid
        fid += 1
        return next(frames, False), fid, fid, 0

    def sleep(seconds):
        nonlocal clock
        clock += seconds
        return clock < 12

    graph = _SweepTrackingGraph()
    mod = _step_module(graph)
    mod.ctx.lifecycle = SimpleNamespace(running=True, sleep=sleep)
    mod.ctx.capture = SimpleNamespace(frame_with_age=frame_with_age)
    pad = SimpleNamespace(left_joystick=lambda **_: None, right_trigger=lambda **_: None,
                          update=lambda: acted.append(fid))
    mod.ctx.gamepad = SimpleNamespace(acquire=lambda: nullcontext(pad))

    assert not mod._drive("p2")
    assert acted == [1, 3]
    assert graph.sweeps == 1
    assert mod._next_sweep_at > 13.0


def test_sweep_is_rate_limited(monkeypatch) -> None:
    """限频：窗口内反复调用只真正扫一次。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "ADVANCE_SWEEP_INTERVAL_S", 10.0)
    graph = _SweepTrackingGraph()
    m = _step_module(graph)

    m._sweep_transient_popup()
    m._sweep_transient_popup()
    m._sweep_transient_popup()

    assert graph.sweeps == 1


def test_step_retry_sweeps_before_resend(monkeypatch) -> None:
    """步级重发前做兜底：弹窗盖住按钮时先赶再发。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_WINDOW_S", 0.05)
    monkeypatch.setattr(m, "STEP_POLL_S", 0.001)
    monkeypatch.setattr(m, "ADVANCE_SWEEP_INTERVAL_S", 0.01)
    graph = _SweepTrackingGraph()
    ok = _step_module(graph)._run_step("infinite_drive.点比赛", "入口·比赛")

    assert ok is False
    assert graph.sweeps >= 1      # 重发循环里兜底至少跑过一次


# ---------- 控制局 raw 调试图（debug 开关闸；v1 口径的延续） ----------

def _drive_frames_stub(mod, monkeypatch, frame):
    """给 _drive 三个新鲜帧与假手柄：前两帧驾驶 HUD 在位、第三帧走结束信号。"""
    from contextlib import nullcontext
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "_DRIVE_HUD_GAP_S", 0.0)
    monkeypatch.setattr(m, "drive_hud_visible", lambda _frame: _frame is frame,
                        raising=False)
    frames = iter([(frame, 1, 1, 0), (frame, 2, 2, 0), (object(), 3, 3, 0)])
    mod.ctx.capture = SimpleNamespace(frame_with_age=lambda: next(frames))
    pad = SimpleNamespace(left_joystick=lambda **_: None,
                          right_trigger=lambda **_: None, update=lambda: None)
    mod.ctx.gamepad = SimpleNamespace(acquire=lambda: nullcontext(pad))


def test_debug_drive_saves_raw_frames(monkeypatch, tmp_path) -> None:
    """debug 开 + 程序驾驶局：raw 与叠加图按阶段落盘并登记行为清单段。

    布局走 session_layout 控制面（debug/<会话>/infinite_drive/drive/<局前缀>_p<阶段>/），
    索引 hud 指向实际叠加图，行为级索引在收尾重建。"""
    import json

    import numpy as np
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "debug_dir", lambda: tmp_path)
    monkeypatch.setattr(m.logger, "_session_token", "sess_dbg")
    mod = _step_module(SimpleNamespace(run=lambda *_args: True))
    mod.ctx.debug = SimpleNamespace(enabled=True)
    _drive_frames_stub(mod, monkeypatch, np.zeros((48, 64, 3), dtype=np.uint8))
    mod._round_no = 1
    mod._round_stamp = "20260101_000000"

    assert mod._drive("p1") is True
    behavior = tmp_path / "sess_dbg" / "infinite_drive" / "drive"
    seg = behavior / "20260101_000000_p1"
    assert (behavior / "manifest.json").is_file()           # 行为清单登记了段
    assert list((seg / "raw").glob("*.jpg"))                # raw 原帧落盘（10fps 节流）
    rows = [json.loads(line) for line
            in (seg / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert rows and all(row["hud"] and (seg / row["hud"]).is_file()
                        for row in rows)                    # 叠加图落盘且索引可解析
    assert (behavior / "frames.jsonl").is_file()            # 行为级索引收尾重建


def test_debug_drive_off_saves_nothing(monkeypatch, tmp_path) -> None:
    """debug 关（ctx 无该面）：零产物；录制局同样不建 writer（录制器已写同一套）。"""
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "debug_dir", lambda: tmp_path)
    monkeypatch.setattr(m.logger, "_session_token", "sess_dbg")
    mod = _step_module(SimpleNamespace(run=lambda *_args: True))
    _drive_frames_stub(mod, monkeypatch, object())
    assert mod._drive("p1") is True

    mod2 = _step_module(SimpleNamespace(run=lambda *_args: True))
    mod2.ctx.debug = SimpleNamespace(enabled=True)
    mod2.set_module_config({"drive_mode": "depth", "record_mode": True})
    _drive_frames_stub(mod2, monkeypatch, object())
    assert mod2._drive("p1") is True

    assert not (tmp_path / "sess_dbg").exists()             # 两种局都不落 debug 面


# ---------- 提前结算（分数达标中途退出；机制见 RULES.md §0.5） ----------

class _FakeOcr:
    """单框 OCR 假引擎：recognize 返回带 text 的自身，供 parse_int 消费。"""

    def __init__(self, texts):
        self._texts = iter(texts) if isinstance(texts, list) else None
        self.text = texts if not isinstance(texts, list) else None

    def recognize(self, frame, roi):
        if self._texts is not None:
            self.text = next(self._texts)
        return self


def test_score_above_target_parses_and_compares() -> None:
    """触发判据：OCR 文本取整后与本关目标比较；读不出 / 解析失败一律 False。"""
    from types import SimpleNamespace

    mod = _step_module(SimpleNamespace(run=lambda *_a: False))
    mod._ocr = _FakeOcr("6,895")
    assert mod._score_above_target(object()) is True
    mod._ocr = _FakeOcr("5,999")
    assert mod._score_above_target(object()) is False
    mod._ocr = _FakeOcr("")            # 读不出：False，不抛
    assert mod._score_above_target(object()) is False
    mod._ocr = _FakeOcr("目标得分")     # 无数字：False
    assert mod._score_above_target(object()) is False


def test_score_ocr_failure_disables_trigger() -> None:
    """OCR 不可用：停用触发且只告警一次，后续读数不再碰引擎。"""
    from types import SimpleNamespace

    class _BoomOcr:
        calls = 0

        def recognize(self, frame, roi):
            self.calls += 1
            raise RuntimeError("boom")

    mod = _step_module(SimpleNamespace(run=lambda *_a: False))
    mod._ocr = _BoomOcr()
    assert mod._score_above_target(object()) is False
    assert mod._score_above_target(object()) is False
    assert mod._ocr.calls == 1          # 停用后不再碰 OCR


class _EarlyExitGraph:
    """提前结算序列的页面状态机：第 N 步点击后第 N 个落地页才在位。"""

    def __init__(self) -> None:
        self.done = 0
        self.clicks = []

    def run(self, entry, _reached=None, **kwargs):
        from maaracing_master.plugins.infinite_drive import module as m

        if entry in (m._EXIT_GEAR, m._EXIT_LEAVE, m._EXIT_CONFIRM):
            self.clicks.append(entry)
            self.last_click_sent = True
            self.last_click_landed = True
            self.done = min(self.done + 1, 3)
            return True
        if entry == m.STEP_CONFIRM["提前结算·退出"]:     # 退出落地 = 确认框或结算页
            return self.done >= 2
        if entry == m._SETTINGS_ANCHOR:
            return self.done >= 1
        if entry == m._EXIT_DIALOG_ANCHOR:
            return self.done >= 2
        if entry == m._SETTLE_ANCHOR:
            return self.done >= 3
        if entry in (m._EXIT_CANCEL, m._EXIT_RESUME):   # 回赶点击
            self.clicks.append(entry)
            return True
        return False


def _early_exit_module(graph, monkeypatch):
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_WINDOW_S", 0.05)
    monkeypatch.setattr(m, "STEP_POLL_S", 0.001)
    return _step_module(graph)


def test_early_exit_sequence_lands_settlement(monkeypatch) -> None:
    """三步序列全部落地：齿轮 → 退出 → 确认，结算页在位即成功。"""
    graph = _EarlyExitGraph()
    mod = _early_exit_module(graph, monkeypatch)

    assert mod._run_early_exit() is True
    assert graph.clicks == [mod.__class__ and "infinite_drive.提前退出.点齿轮",
                            "infinite_drive.提前退出.点退出",
                            "infinite_drive.提前退出.点确认退出"]


def test_early_exit_skips_confirm_when_settlement_direct(monkeypatch) -> None:
    """「今日不再提示」生效日：点完退出直达结算页，确认步免点。"""

    class _NoDialogGraph(_EarlyExitGraph):
        def run(self, entry, _reached=None, **kwargs):
            from maaracing_master.plugins.infinite_drive import module as m

            if entry == m._EXIT_LEAVE:
                self.clicks.append(entry)
                self.last_click_sent = True
                self.last_click_landed = True
                self.done = 3      # 确认框被跳过，结算页直接在位
                return True
            return super().run(entry, _reached)

    graph = _NoDialogGraph()
    mod = _early_exit_module(graph, monkeypatch)

    assert mod._run_early_exit() is True
    assert "infinite_drive.提前退出.点确认退出" not in graph.clicks


def test_early_exit_failure_dismisses_back_to_driving(monkeypatch) -> None:
    """退出点击被吞：重发耗尽后回赶（设置页在位点继续），序列报失败。"""

    class _SwallowLeaveGraph(_EarlyExitGraph):
        def run(self, entry, _reached=None, **kwargs):
            from maaracing_master.plugins.infinite_drive import module as m

            if entry == m._EXIT_LEAVE:
                self.clicks.append(entry)
                self.last_click_sent = True
                self.last_click_landed = False   # 点击发出但页面不切
                return True
            return super().run(entry, _reached)

    graph = _SwallowLeaveGraph()
    mod = _early_exit_module(graph, monkeypatch)

    assert mod._run_early_exit() is False
    assert "infinite_drive.提前退出.点确认退出" not in graph.clicks
    assert "infinite_drive.提前退出.点继续" in graph.clicks   # 回赶续驾


def _drive_frames_with_ocr(mod, monkeypatch, texts, count):
    """给 _drive 连续 count 个驾驶帧（HUD 在位）+ 1 个菜单帧收尾（结束信号命中）。"""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "_SCORE_CHECK_INTERVAL_S", 0.0)
    monkeypatch.setattr(m, "_DRIVE_HUD_GAP_S", 0.0)
    drive_frame = object()
    menu_frame = object()
    frames = iter([(drive_frame, i, i, 0) for i in range(1, count + 1)]
                  + [(menu_frame, count + 1, count + 1, 0)])
    monkeypatch.setattr(m, "drive_hud_visible",
                        lambda _f: _f is drive_frame, raising=False)
    mod.ctx.capture = SimpleNamespace(frame_with_age=lambda: next(frames))
    released = []

    @contextmanager
    def acquire():
        yield SimpleNamespace(left_joystick=lambda **_: None,
                              right_trigger=lambda **_: None, update=lambda: None)
        released.append(True)

    mod.ctx.gamepad = SimpleNamespace(acquire=acquire)
    mod._ocr = _FakeOcr(texts)
    return released


def test_drive_p1_early_exit_sets_jump_flag(monkeypatch) -> None:
    """连续 3 拍超目标：松杆走退出序列，阶段1落点标记置位，阶段就地结束。"""
    from types import SimpleNamespace

    mod = _step_module(SimpleNamespace(run=lambda *_a: False))
    exits = []
    monkeypatch.setattr(mod, "_run_early_exit", lambda: exits.append(True) or True)
    released = _drive_frames_with_ocr(mod, monkeypatch, "6,895", 5)

    assert mod._drive("p1") is True
    assert exits == [True]
    assert mod._p1_settled_early is True
    assert released == [True]           # 先松杆再走退出序列


def test_drive_p2_early_exit_no_jump_flag(monkeypatch) -> None:
    """阶段2提前结算：同样触发，但不置阶段1跳转标记。"""
    from types import SimpleNamespace

    mod = _step_module(SimpleNamespace(run=lambda *_a: False))
    exits = []
    monkeypatch.setattr(mod, "_run_early_exit", lambda: exits.append(True) or True)
    _drive_frames_with_ocr(mod, monkeypatch, "12,345", 5)

    assert mod._drive("p2") is True
    assert exits == [True]
    assert mod._p1_settled_early is False


def test_drive_streak_reset_blocks_premature_exit(monkeypatch) -> None:
    """达标读数被中间读数打断：计数归零，不触发提前结算。"""
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    mod = _step_module(SimpleNamespace(
        run=lambda entry, _reached=None: entry == m._DRIVE_END["p1"]))
    exits = []
    monkeypatch.setattr(mod, "_run_early_exit", lambda: exits.append(True) or True)
    _drive_frames_with_ocr(mod, monkeypatch,
                           ["6,100", "93", "6,200", "93", "6,300"], 5)

    assert mod._drive("p1") is True     # 菜单帧走结束信号收步
    assert exits == []



def test_p1_early_settle_skips_card2_and_p2_in_flow(monkeypatch) -> None:
    """阶段1提前结算：流程跳过选卡2与阶段2，直达结算步并读结算。"""
    from types import SimpleNamespace

    from maaracing_master.core.nav_graph import ProbeOutcome
    from maaracing_master.plugins.infinite_drive import module as m

    mod = _step_module(SimpleNamespace(run=lambda *_a: False,
                                       probe=lambda *_a, **_k: ProbeOutcome.MISS))
    visited = []
    monkeypatch.setattr(mod, "_drive", lambda phase: visited.append(phase) or True)
    monkeypatch.setattr(mod, "_pick_cards", lambda phase: visited.append(phase) or True)
    monkeypatch.setattr(mod, "_read_settlement", lambda: visited.append("结算读数"))

    def stop_at_settle(node, stage):
        visited.append(stage)
        if stage == "结算·下一关":
            mod._running = False        # 结算步跑完即收，避免整轮循环
        return True

    monkeypatch.setattr(mod, "_run_step", stop_at_settle)
    mod._p1_settled_early = True
    start = next(i for i, (s, _, _) in enumerate(m.FLOW) if s == "驾驶·阶段1")

    assert mod._run_flow(start) is True
    assert visited == ["p1", "结算读数", "结算·下一关"]   # card2 / p2 未被访问
    assert mod._p1_settled_early is False               # 标记即取即清


# ---------- 调试存图接线：nav + drive 两面（v1 口径） ----------

def _function_body(src: str, name: str) -> str:
    start = src.index(name)
    end = src.index("\n    def ", start + 10)
    return src[start:end]


def test_ui_nav_segments_save_images_and_drive_chain_does_not() -> None:
    """UI 导航段必须显式声明落图（nav），驾驶控制链不声明（drive 归 writer/录制器）。

    `DEBUG_SAVE` 是运行项级（本模块为 self），做不到「一半落一半不落」；真正的
    分界靠接口：`run(..., save=NAV_SAVE)` 开段落图，驾驶侧的 probe/end 判据不声明。
    """
    import re
    from pathlib import Path

    from maaracing_master.plugins.infinite_drive import module as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    for name in ("def _run_flow", "def _run_step", "def _pick_cards",
                 "def _wait_card_anchor", "def _sweep_transient_popup",
                 "def _resume_from", "def _read_settlement",
                 "def _dismiss_exit_pages"):
        body = _function_body(src, name)
        calls = re.findall(r"self\._graph\.run\([^)]*\)", body)
        assert calls, f"{name} 里应有 run 调用"
        for call in calls:
            assert "save=NAV_SAVE" in call, f"{name} 的 run 未声明落图：{call}"

    drive = _function_body(src, "def _drive(")
    assert "save=" not in drive, "驾驶控制链不应声明存盘策略（与 drive 取证双写）"


# ---------- 失联层：流程外页面（大厅）作废本轮并从入口重开 ----------

class _HallSeenGraph:
    """只读探测命中大厅；其余识别一律 miss（流程卡住、画面已回大厅）。"""

    def __init__(self) -> None:
        self.probes = 0

    def run(self, *_args, **_kwargs) -> bool:
        return False

    def probe(self, _node: str, **_kwargs):
        from maaracing_master.core.nav_graph import ProbeOutcome

        self.probes += 1
        return ProbeOutcome.HIT


def test_hall_probe_skipped_in_entry_chain(monkeypatch) -> None:
    """入口链第 0 步本来就在大厅：不探测，避免重开自成死循环。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "HALL_PROBE_INTERVAL_S", 0.0)
    graph = _HallSeenGraph()
    mod = _step_module(graph)

    assert mod._poll_hall() is False
    assert graph.probes == 0

    mod._stage_index = m._SETTLE_STAGE_INDEX
    assert mod._poll_hall() is True
    assert mod._hall_reset is True
    assert graph.probes == 1


def test_step_stops_immediately_when_hall_detected(monkeypatch) -> None:
    """重发窗口里发现已回大厅：立刻收步，不再耗重发预算。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_WINDOW_S", 0.05)
    monkeypatch.setattr(m, "STEP_POLL_S", 0.001)
    monkeypatch.setattr(m, "HALL_PROBE_INTERVAL_S", 0.0)
    graph = _HallSeenGraph()
    mod = _step_module(graph)
    mod._stage_index = m._SETTLE_STAGE_INDEX

    assert mod._run_step("infinite_drive.点下一关", "结算·下一关") is False
    assert mod._hall_reset is True
    assert graph.probes >= 1


def test_card_wait_aborts_on_hall(monkeypatch) -> None:
    """选卡等待期发现已回大厅：立即收步（不等 15s 上限）。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "HALL_PROBE_INTERVAL_S", 0.0)
    mod = _step_module(_HallSeenGraph())
    mod._stage_index = m.LOOP_START_INDEX

    assert mod._wait_card_anchor(want_present=True) is False
    assert mod._hall_reset is True


def test_drive_wait_aborts_when_hall_detected(monkeypatch) -> None:
    """驾驶等待期发现已回大厅：立刻收步，不空等到阶段时限。"""
    from maaracing_master.core.nav_graph import ProbeOutcome
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_POLL_S", 0.01)
    monkeypatch.setattr(m, "ADVANCE_SWEEP_INTERVAL_S", 0.0)
    monkeypatch.setattr(m, "HALL_PROBE_INTERVAL_S", 0.0)
    monkeypatch.setattr(m, "_DRIVE_DURATION", {"p1": 5.0, "p2": 5.0})
    monkeypatch.setattr(m.flow_data, "DRIVE_END_MARGIN_S", 5.0)
    graph = _SweepTrackingGraph()
    graph.probe = lambda _node, **_kw: ProbeOutcome.HIT
    mod = _step_module(graph)
    mod._stage_index = 9
    mod.set_module_config({"drive_mode": "depth", "record_mode": True})

    started = time.monotonic()
    assert mod._drive("p2") is False
    assert mod._hall_reset is True
    assert time.monotonic() - started < 4.0   # 未空等到 10s 阶段时限


class _HallProbeGraph(_AllOkGraph):
    """只读探测只在「点比赛」节点命中（= 大厅在位）。"""

    def probe(self, node: str, **_kwargs):
        from maaracing_master.core.nav_graph import ProbeOutcome

        return ProbeOutcome.HIT if node == "infinite_drive.点比赛" else ProbeOutcome.MISS


def test_flow_resumes_from_hall_within_same_round(monkeypatch) -> None:
    """回大厅 → 同一轮内从入口链续跑；不额外消耗连刷轮数。"""
    mod = _module()
    mod._running = True
    mod._graph = _HallProbeGraph()
    mod._round_total = 2
    calls: list[str] = []
    state = {"aborted": False}

    def _step(node, stage):
        calls.append(stage)
        if stage == "结算·下一关" and not state["aborted"]:
            state["aborted"] = True
            mod._hall_reset = True
            return False
        return True

    monkeypatch.setattr(mod, "_run_step", _step)
    monkeypatch.setattr(mod, "_pick_cards", lambda phase: True)
    monkeypatch.setattr(mod, "_drive", lambda phase: True)
    monkeypatch.setattr(mod, "_read_settlement", lambda: None)
    monkeypatch.setattr(mod, "_read_level", lambda: 1)

    assert mod._run_flow(0) is True
    # 首轮中断后同轮从入口链续跑；第二轮从关卡挑战开跑：入口链共 2 次
    assert calls.count("入口·比赛") == 2
    assert mod._round_no == 2                 # 中断轮不额外计数
    assert mod._resume_attempts == 0          # 第二轮正常走完清零


def test_flow_stops_when_resume_exhausted(monkeypatch) -> None:
    """连续恢复达上限即按原样收：不再路由、本轮中止、运行判失败。"""
    from maaracing_master.plugins.infinite_drive import module as m

    mod = _module()
    mod._running = True
    mod._graph = _AllOkGraph()
    mod._resume_attempts = m.RESUME_MAX

    def _step(node, stage):
        if stage == "结算·下一关":
            mod._hall_reset = True
            return False
        return True

    monkeypatch.setattr(mod, "_run_step", _step)
    monkeypatch.setattr(mod, "_pick_cards", lambda phase: True)
    monkeypatch.setattr(mod, "_drive", lambda phase: True)
    monkeypatch.setattr(mod, "_read_settlement", lambda: None)
    monkeypatch.setattr(mod, "_read_level", lambda: 1)

    assert mod._run_flow(0) is False
    assert mod._round_no == 1


def test_drive_wait_probes_hall_but_keeps_waiting_on_miss(monkeypatch) -> None:
    """大厅探测 miss 不改变驾驶等待：照常扫弹窗、到点收步（回归锁）。"""
    from maaracing_master.core.nav_graph import ProbeOutcome
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_POLL_S", 0.02)
    monkeypatch.setattr(m, "ADVANCE_SWEEP_INTERVAL_S", 0.05)
    monkeypatch.setattr(m, "HALL_PROBE_INTERVAL_S", 0.0)
    monkeypatch.setattr(m, "_DRIVE_DURATION", {"p1": 0.1, "p2": 0.1})
    monkeypatch.setattr(m.flow_data, "DRIVE_END_MARGIN_S", 0.1)
    graph = _SweepTrackingGraph()
    probes: list[str] = []
    graph.probe = lambda node, **_kw: probes.append(node) or ProbeOutcome.MISS
    mod = _step_module(graph)
    mod._stage_index = 9
    mod.set_module_config({"drive_mode": "depth", "record_mode": True})

    assert mod._drive("p2") is False
    assert graph.sweeps >= 1
    assert probes and probes[0] == m._HALL_ANCHOR
    assert mod._hall_reset is False


# ---------- 步级成功判据：落地页已在位免点 ----------

class _AlreadyLandedGraph:
    """落地页（下一关准备页）在点之前就在位；点「下一关」的动作节点本应不被触发。"""

    def __init__(self) -> None:
        self.clicks = 0

    def run(self, entry: str, _reached: str | None = None, **_kwargs) -> bool:
        if entry == "infinite_drive.点下一关":
            self.clicks += 1
            return True
        return entry == "infinite_drive.__confirm.结算落地"

    def probe(self, _node: str, **_kwargs):
        from maaracing_master.core.nav_graph import ProbeOutcome

        return ProbeOutcome.MISS


def test_step_skips_click_when_landing_page_already_present(monkeypatch) -> None:
    """中断后被服务端推进到落地页：识别到即在位，免点直接成功。"""
    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "STEP_WINDOW_S", 0.05)
    monkeypatch.setattr(m, "STEP_POLL_S", 0.001)
    graph = _AlreadyLandedGraph()

    assert _step_module(graph)._run_step("infinite_drive.点下一关", "结算·下一关") is True
    assert graph.clicks == 0


# ---------- 结算读数只在结算页在位时进行 ----------

def test_settlement_read_skipped_without_settlement_page(monkeypatch) -> None:
    """准备页/大厅等非结算画面：不读数、不落库（防把别页文本当结算写进看板）。"""
    from types import SimpleNamespace

    mod = _step_module(SimpleNamespace(run=lambda *_a, **_k: False))
    monkeypatch.setattr(mod, "_dismiss_settlement_overlays", lambda: None)
    called = []
    monkeypatch.setattr(mod, "_read_settlement_stable",
                        lambda: called.append(1) or ("", "", False))

    mod._read_settlement()

    assert called == []


# ---------- 准备页关卡规则（目标关数暂固定 1） ----------

def _run_level_flow(levels, monkeypatch, *, target=1, round_total=1):
    from maaracing_master.plugins.infinite_drive import module as m

    mod = _module()
    mod._running = True
    mod._graph = _AllOkGraph()
    mod._round_total = round_total
    monkeypatch.setattr(m, "_TARGET_LEVEL", target)
    seq = iter(levels)
    monkeypatch.setattr(mod, "_read_level", lambda: next(seq, None))
    seen: list[str] = []
    monkeypatch.setattr(mod, "_run_step", lambda node, stage: seen.append(stage) or True)
    monkeypatch.setattr(mod, "_pick_cards", lambda phase: True)
    monkeypatch.setattr(mod, "_drive", lambda phase: True)
    monkeypatch.setattr(mod, "_read_settlement", lambda: None)
    assert mod._run_flow(0) is True
    return mod, seen


def test_prep_over_target_abandons_and_resets(monkeypatch) -> None:
    """中断后游戏停在第2关：准备页读到 2 > 目标 1 → 放弃归零，下一轮按第1关开跑。"""
    mod, seen = _run_level_flow([2, 2, 1, 2], monkeypatch, round_total=2)

    assert seen.count("准备·放弃") == 2      # 进入时放弃一次，正常结算后放弃一次
    assert seen.count("准备·开始") == 1      # 只在第1关开打
    assert mod._current_level == 1


def test_prep_within_target_plays_next_level(monkeypatch) -> None:
    """目标 3 关：结算后下一关正好前进一级且未超目标 → 直接开打，不放弃。"""
    mod, seen = _run_level_flow([1, 2, 2, 3, 3, 4], monkeypatch, target=3)

    assert seen.count("准备·开始") == 3
    assert seen.count("准备·放弃") == 1      # 第4关超出目标才放弃
    assert mod._current_level == 3


def test_target_score_follows_current_level(monkeypatch) -> None:
    """提前结算的目标分按当前关卡查表（第2关 12,000，不是第1关的 6,000）。"""
    from maaracing_master.plugins.infinite_drive import module as m

    mod = _module()
    mod._current_level = 2
    assert mod._target_score() == m.flow_data.PVE_STAGE_TARGETS[2] == 12000
    mod._current_level = 1
    assert mod._target_score() == 6000


# ---------- 失败收口：按页面路由 ----------

class _RouteGraph:
    def __init__(self, hit_node: str) -> None:
        self.hit_node = hit_node
        self.runs: list[str] = []

    def run(self, entry: str, *_args, **_kwargs) -> bool:
        self.runs.append(entry)
        return True

    def probe(self, node: str, **_kwargs):
        from maaracing_master.core.nav_graph import ProbeOutcome

        return ProbeOutcome.HIT if node == self.hit_node else ProbeOutcome.MISS


def test_resume_routes_by_current_page(monkeypatch) -> None:
    """四类页面各回各的阶段；认不出页面返回 None。"""
    from maaracing_master.plugins.infinite_drive import module as m

    cases = {
        m._SETTLE_ANCHOR: m._SETTLE_STAGE_INDEX,
        "infinite_drive.__confirm.准备页": m._PREP_START_INDEX,
        "infinite_drive.__confirm.模式页": m.LOOP_START_INDEX,
        m._HALL_ANCHOR: 0,
    }
    for node, index in cases.items():
        mod = _step_module(_RouteGraph(node))
        mod._stage_index = m._SETTLE_STAGE_INDEX
        assert mod._resume_from("结算·下一关") == index
        assert mod._resume_attempts == 1

    mod = _step_module(_RouteGraph("nowhere"))
    assert mod._resume_from("结算·下一关") is None


def test_resume_cap_stops_without_probing(monkeypatch) -> None:
    """连续恢复达上限：直接停止，不再探测页面。"""
    from maaracing_master.plugins.infinite_drive import module as m

    graph = _RouteGraph(m._HALL_ANCHOR)
    mod = _step_module(graph)
    mod._resume_attempts = m.RESUME_MAX

    assert mod._resume_from("结算·下一关") is None
    assert graph.runs == []


# ---------- 结算终局：挑战失败 / 五关跑满（按钮同形不同文案，按落点路由） ----------

class _ModeLandedGraph(_AllOkGraph):
    """结算按钮落在活动页（模式页）= 本局终局。"""

    def probe(self, node: str, **_kwargs):
        from maaracing_master.core.nav_graph import ProbeOutcome

        return (ProbeOutcome.HIT if node == "infinite_drive.__confirm.模式页"
                else ProbeOutcome.MISS)


def test_settle_end_returns_to_activity_and_starts_new_round(monkeypatch) -> None:
    """终局结算（失败/本轮关卡跑满）不走放弃：回活动页后本轮结束，下一轮从关卡挑战重开。"""
    mod = _module()
    mod._running = True
    mod._graph = _ModeLandedGraph()
    mod._round_total = 2
    seen: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(mod, "_run_step", lambda node, stage: seen.append(stage) or True)
    monkeypatch.setattr(mod, "_pick_cards", lambda phase: True)
    monkeypatch.setattr(mod, "_drive", lambda phase: True)
    monkeypatch.setattr(mod, "_read_settlement", lambda: None)
    monkeypatch.setattr(mod, "_read_level", lambda: 1)
    monkeypatch.setattr(mod, "_tlog", lambda msg, level="INFO": logs.append(msg))

    assert mod._run_flow(0) is True
    assert mod._round_no == 2
    assert seen.count("结算·下一关") == 2
    assert seen.count("准备·放弃") == 0      # 终局游戏已自动归零，不走放弃
    assert seen.count("关卡挑战") == 2       # 每轮都从活动页的关卡挑战重开
    assert mod._current_level == 0           # 关卡缓存随终局清空
    assert any("终局" in line for line in logs)


def test_settle_end_logs_failure_and_full_clear(monkeypatch) -> None:
    """终局日志按战绩记录区分失败与本轮跑满；活动页不在位不判终局。"""
    mod = _step_module(_ModeLandedGraph())
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_tlog", lambda msg, level="INFO": logs.append((msg, level)))

    mod._current_level = 3
    mod.last_settlement = {"record": {"level": 3, "level_total": 5, "cleared": False}}
    assert mod._settle_ends_run() is True
    assert "挑战失败" in logs[-1][0] and logs[-1][1] == "WARNING"
    assert mod._current_level == 0

    mod._current_level = 5
    mod.last_settlement = {"record": {"level": 5, "level_total": 5, "cleared": True}}
    assert mod._settle_ends_run() is True
    assert "本轮关卡已跑满" in logs[-1][0] and logs[-1][1] == "INFO"

    mod._graph = _AllOkGraph()
    mod._current_level = 2
    mod.last_settlement = {"record": {"level": 2, "level_total": 5, "cleared": True}}
    assert mod._settle_ends_run() is False      # 晋级落准备页，不是终局
    assert mod._current_level == 2


def test_settle_land_confirm_covers_both_landings() -> None:
    """结算步落地确认同时接受晋级准备页与终局活动页（同一按钮两种落点）。"""
    import json

    from maaracing_master.plugins.infinite_drive import PIPELINE_DIR
    from maaracing_master.plugins.infinite_drive import module as m

    graph = json.loads((PIPELINE_DIR / "infinite_drive.json").read_text(encoding="utf-8"))
    node = graph[m.STEP_CONFIRM["结算·下一关"]]
    assert node["recognition"]["type"] == "Or"
    assert set(node["recognition"]["param"]["any_of"]) == {
        "infinite_drive.__confirm.准备页", m._MODE_ANCHOR}
