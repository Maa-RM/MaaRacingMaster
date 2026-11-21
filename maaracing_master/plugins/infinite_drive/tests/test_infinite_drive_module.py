# -*- coding: utf-8 -*-
"""无限驾驶流程模块的配置面与录制态回归锁（离线，ctx=None）。

覆盖：录制开关的配置读写往返、任务卡摘要、录制态放宽启动约束（虚拟手柄能力 /
手柄独占）、人工接管期声明只落驾驶阶段、配置表单字段键都在默认配置里、
录制状态行跟随录制器实况。
"""

from __future__ import annotations

from maaracing_master.plugins.infinite_drive.module import (
    REC_DRIVE_FPS,
    REC_NAV_FPS,
    InfiniteDriveModule,
    _DRIVE_STAGES,
    _record_fps,
)


def _module() -> InfiniteDriveModule:
    return InfiniteDriveModule(None)


def test_record_mode_config_roundtrip() -> None:
    m = _module()
    assert m.get_module_config()["record_mode"] is False
    out = m.set_module_config({"record_mode": True})
    assert out["record_mode"] is True
    assert m.get_module_config()["record_mode"] is True


def test_set_module_config_ignores_non_dict_and_unknown_keys() -> None:
    m = _module()
    m.set_module_config("not-a-dict")  # type: ignore[arg-type]
    m.set_module_config({"unrelated": 1})
    assert m.get_module_config()["record_mode"] is False


def test_config_summary_announces_recording_only_when_on() -> None:
    assert InfiniteDriveModule.config_summary({"record_mode": True}) == ["录制: 开"]
    assert InfiniteDriveModule.config_summary({"record_mode": False}) == []


def test_drive_mode_roundtrip_and_legacy_default() -> None:
    m = _module()
    assert m.get_module_config()["drive_mode"] == "straight"
    assert m.set_module_config({"drive_mode": "depth"})["drive_mode"] == "depth"
    assert m.set_module_config({"record_mode": True})["drive_mode"] == "depth"
    assert m.set_module_config({"drive_mode": "macro"})["drive_mode"] == "straight"


def test_record_mode_relaxes_startup_constraints() -> None:
    """录制态不要求虚拟手柄能力、不独占手柄——验证轮驾驶阶段由用户物理手柄驾驶。"""
    assert InfiniteDriveModule.required_capabilities({"record_mode": True}) == \
        frozenset({"capture"})
    assert InfiniteDriveModule.required_capabilities({"record_mode": False}) == \
        InfiniteDriveModule.REQUIRES
    assert InfiniteDriveModule.required_capabilities(None) == InfiniteDriveModule.REQUIRES
    assert InfiniteDriveModule.requires_exclusive_gamepad({"record_mode": True}) is False
    assert InfiniteDriveModule.requires_exclusive_gamepad(None) is \
        InfiniteDriveModule.REQUIRES_GAMEPAD_EXCLUSIVE


def test_config_ui_fields_are_declared_keys() -> None:
    """表单字段键必须在默认配置里——宿主按默认值渲染，漂移键渲染不出。"""
    fields = [f for group in InfiniteDriveModule.CONFIG_UI["groups"]
              for f in group["fields"]]
    assert fields, "配置表单不应为空"
    for field in fields:
        assert field["key"] in InfiniteDriveModule.DEFAULT_MODULE_CONFIG


def test_manual_input_active_scoped_to_drive_stage() -> None:
    """人工接管期只落驾驶阶段且只在录制态：UI 步进仍是程序操作期。"""
    m = _module()
    m.set_module_config({"record_mode": True})
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

    def run(self, entry: str, _reached: str | None) -> bool:
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
        def run(self, entry: str, _reached: str | None) -> bool:
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

    def run(self, entry: str, _reached: str | None = None) -> bool:
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
        def run(self, entry: str, _reached: str | None = None) -> bool:
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

    def run(self, entry: str, _reached: str | None = None) -> bool:
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


# ---------- 偶发弹窗 OCR 兜底（限频） ----------

class _SweepTrackingGraph(_SwallowedClickGraph):
    """记录兜底节点被查询的次数；end 信号可脚本化。"""

    def __init__(self, end_hits: bool = False):
        super().__init__()
        self.sweeps = 0
        self.end_hits = end_hits

    def run(self, entry: str, _reached: str | None = None) -> bool:
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
    m.set_module_config({"record_mode": True})

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

    def run(entry, _reached):
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


def test_depth_mode_sends_feedback_steer_in_both_phases(monkeypatch) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "_DRIVE_HUD_GAP_S", 0.0)
    drive_frame = object()
    frames = iter([(drive_frame, 1, 1, 0), (object(), 2, 2, 0),
                   (drive_frame, 3, 3, 0), (object(), 4, 4, 0)])
    directions = []
    armed = []
    detected = []
    pad = SimpleNamespace(left_joystick=lambda x_value: directions.append(x_value),
                          right_trigger=lambda **_: None, update=lambda: None)
    monkeypatch.setattr(m, "drive_hud_visible", lambda frame: frame is drive_frame,
                        raising=False)
    mod = _step_module(SimpleNamespace(run=lambda *args: True))
    mod.set_module_config({"drive_mode": "depth"})
    mod.ctx.capture = SimpleNamespace(frame_with_age=lambda: next(frames))
    mod.ctx.gamepad = SimpleNamespace(acquire=lambda: nullcontext(pad))

    def steer(frame, fid, timestamp):
        detected.append((frame, fid, timestamp))
        return 0.2, False

    mod._depth_drive = SimpleNamespace(begin=lambda **options: armed.append(options), steer=steer,
                                       release=lambda: None)
    assert mod._drive("p1") and mod._drive("p2")
    assert armed == [{"allow_view_switch": True}, {"allow_view_switch": False}]
    assert detected == [(drive_frame, 1, 1), (drive_frame, 3, 3)]
    assert directions[0] == directions[1] > 0


def test_depth_view_switch_releases_camera_button(monkeypatch) -> None:
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

