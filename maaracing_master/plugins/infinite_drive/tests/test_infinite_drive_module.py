# -*- coding: utf-8 -*-
"""无限驾驶流程模块的配置面与录制态回归锁（离线，ctx=None）。

覆盖：录制开关的配置读写往返、任务卡摘要、录制态放宽启动约束（虚拟手柄能力 /
手柄独占）、人工接管期声明只落驾驶阶段、配置表单字段键都在默认配置里、
录制状态行跟随录制器实况。
"""

from __future__ import annotations

from maaracing_master.plugins.infinite_drive.module import InfiniteDriveModule


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
