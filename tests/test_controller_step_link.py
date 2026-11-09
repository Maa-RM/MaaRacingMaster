# -*- coding: utf-8 -*-
"""内置步骤的执行态与资源所有权（ADR-0018）。

锁四件事：
1. `execution_active` 覆盖模块与步骤两种执行体，而 `_running` / `module_active`
   保持"有模块在跑"的原义（GUI 运行显示与看板依赖它们）；
2. 步骤执行期间 `_step_active` 置真、结束复位（含异常路径）；
3. 收尾未完成（框架未收摊）时保留资源、标污染、拒绝下一轮；
4. 虚拟手柄按「谁创建谁负责」销毁：步骤不拆模块建的设备。
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from maaracing_master.core import builtin_steps as bs
from maaracing_master.core import controller as ctl


class _Grp:
    def __init__(self):
        self.id = "g-step"
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO"):
        self.lines.append((level, msg))

    def end(self, outcome=None):
        pass


def _ctl() -> ctl.MaaRacingMasterController:
    """构造真实控制器：初始化契约由构造函数自己负责，测试不再手工复刻字段。"""
    with mock.patch.object(
            ctl, "debug_dir",
            lambda: Path(tempfile.gettempdir()) / "maar-test-debug"):
        return ctl.MaaRacingMasterController()


class _Step:
    NAME = "假步骤"
    seen_active = None
    seen_stop = None
    def __init__(self, app, grp=None):
        self.app = app
        self.last_outcome = "failed"

    def required_resources(self, cfg):
        return frozenset()

    def run(self, cfg, stop_event, log, session):
        _Step.seen_active = self.app._step_active
        _Step.seen_stop = stop_event.is_set()
        self.last_outcome = cfg.get("outcome", "completed")
        return cfg.get("ok", True)


class _Session:
    def __init__(self, app, needs, grp=None):
        self.needs = needs

    def close(self):
        return _Session.close_ok

    close_ok = True


@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(ctl, "_run_group", lambda title, kind="session": _Grp())
    monkeypatch.setattr(bs, "BUILTIN_STEPS", {"fake": _Step})
    monkeypatch.setattr(ctl, "StepSession", _Session)
    _Step.seen_active = None
    _Step.seen_stop = None
    _Session.close_ok = True
    return _ctl()


def test_connect_short_circuit_requires_live_window(monkeypatch):
    """手动关游戏后旧句柄失效：幂等短路不能假成功，必须重新定位并如实失败。"""
    c = _ctl()
    c.controller = object()
    c.adopt_window(123)
    monkeypatch.setattr(ctl, "is_window_valid", lambda hwnd: False)
    monkeypatch.setattr(ctl.game_process, "find_window_by_process", lambda: 0)
    monkeypatch.setattr(ctl, "find_game_hwnd", lambda **kw: 0)

    assert c.connect() is False
    assert c.controller is None
    assert c._hwnd == 0


def test_locate_prefers_process_over_title(monkeypatch):
    """没有接管句柄时先按进程名认，认不出才退回按标题：启动器窗口与游戏窗口同名，
    标题是最后一级线索，不能排在前面。"""
    c = _ctl()
    seen: list[str] = []
    monkeypatch.setattr(ctl.game_process, "find_window_by_process",
                        lambda: seen.append("exe") or 0)
    monkeypatch.setattr(ctl, "find_game_hwnd",
                        lambda **kw: seen.append("title") or 0)
    assert c.locate_game_hwnd() == 0
    assert seen == ["exe", "title"]


def test_locate_stops_at_process_hit(monkeypatch):
    c = _ctl()
    monkeypatch.setattr(ctl.game_process, "find_window_by_process", lambda: 4321)
    monkeypatch.setattr(ctl, "find_game_hwnd",
                        lambda **kw: pytest.fail("进程名已认出不该再按标题搜"))
    assert c.locate_game_hwnd() == 4321


def test_locate_uses_adopted_handle(monkeypatch):
    """启动步骤交来的句柄优先：两种搜索都不该被走到。"""
    c = _ctl()
    c.adopt_window(1234)
    monkeypatch.setattr(ctl, "is_window_valid", lambda hwnd: True)
    monkeypatch.setattr(ctl.game_process, "find_window_by_process",
                        lambda: pytest.fail("已有接管句柄不该再找窗口"))
    assert c.locate_game_hwnd() == 1234


def test_connect_short_circuit_keeps_live_window(monkeypatch):
    c = _ctl()
    c.controller = object()
    c.adopt_window(123)
    monkeypatch.setattr(ctl, "is_window_valid", lambda hwnd: True)
    assert c.connect() is True


def test_execution_active_covers_module_and_step_without_touching_running():
    c = _ctl()
    module = object()

    # 空闲
    assert (c.execution_active, c._running, c.module_active) == (False, False, False)

    # 模块执行中
    c._running, c._active_module = True, module
    assert (c.execution_active, c._running, c.module_active) == (True, True, True)

    # 内置步骤执行中：execution_active 为真，但"有模块在跑"仍为假
    c._running, c._active_module = False, None
    c._step_active = True
    assert (c.execution_active, c._running, c.module_active) == (True, False, False)

    # 两者同时（理论上不该发生，但判据必须仍然为真）
    c._running, c._active_module = True, module
    assert c.execution_active is True


def test_step_active_is_true_during_run_and_reset_after(wired):
    ok = wired.run_builtin_step("fake", {"ok": True, "outcome": "completed"})
    assert ok is True
    assert _Step.seen_active is True, "步骤执行期间 _step_active 必须为真（sleep 依赖它）"
    assert wired._step_active is False
    assert wired.last_step_outcome == "completed"
    assert wired._running is False, "_running 的语义是「有模块在跑」，步骤不得置真"


def test_step_active_resets_even_when_step_raises(wired, monkeypatch):
    class _Boom(_Step):
        def run(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(bs, "BUILTIN_STEPS", {"fake": _Boom})
    with pytest.raises(RuntimeError):
        wired.run_builtin_step("fake", {})
    assert wired._step_active is False


def test_teardown_failure_keeps_resources_and_blocks_next_round(wired, monkeypatch):
    _Session.close_ok = False
    ok = wired.run_builtin_step("fake", {})
    assert ok is False, "收尾未完成必须判失败（覆盖步骤自己的成功）"
    assert wired._teardown_failed is True
    assert wired.last_step_outcome == "completed"  # 步骤本身的结果仍可查

    with pytest.raises(RuntimeError, match="收尾未完成"):
        wired.run_builtin_step("fake", {})
    with pytest.raises(RuntimeError, match="收尾未完成"):
        wired.start_module("whatever")


def test_gpad_is_destroyed_only_when_the_step_created_it():
    c = _ctl()
    destroyed = []
    c._destroy_gpad = lambda grp=None: destroyed.append("gpad")
    c._stop_wgc_capture = lambda: destroyed.append("wgc")
    c._gpad = object()

    c._gpad_owner = "module"
    c.release_step_session()
    assert destroyed == ["wgc"], "模块建的设备不能被子会话拆掉"

    destroyed.clear()
    c._gpad_owner = "step"
    c.release_step_session()
    assert destroyed == ["gpad", "wgc"]


def test_release_closes_context_before_gpad_and_capture():
    c = _ctl()
    order = []
    c._ctx = type("C", (), {"close": lambda self: order.append("ctx")})()
    c._destroy_gpad = lambda grp=None: order.append("gpad")
    c._stop_wgc_capture = lambda: order.append("wgc")
    c._gpad_owner = "step"

    c.release_step_session()
    assert order == ["ctx", "gpad", "wgc"]
    assert c._ctx is None, "Context 关闭后必须置空，否则下次复用已关闭的实例"


def test_clear_run_requests_resets_run_requests():
    """受理新一轮时清手动停止/停止信号/退出请求；项入口不再各自清。"""
    c = _ctl()
    c.stop()
    c.request_exit()
    c.clear_run_requests()
    assert c._manual_stop is False
    assert c.stop_event.is_set() is False
    assert c._exit_requested is False


def test_begin_run_preserves_stop_requests():
    """worker 启动后到达的停止不得被运行边界清掉（受理与 worker 执行间的竞态窗口）。"""
    c = _ctl()
    c.stop()
    c.request_exit()
    c.begin_run()
    assert c._manual_stop is True
    assert c.stop_event.is_set() is True
    assert c._exit_requested is True


def test_manual_stop_is_not_cleared_by_item_entry(wired):
    """两项之间按下的停止必须保留：下一项不能把手动停止清掉后继续跑。"""
    wired.stop()
    wired.run_builtin_step("fake", {})
    assert _Step.seen_stop is True
    assert wired.stop_event.is_set() is True


def test_item_auto_stop_is_cleared_for_next_item(wired):
    """上一项到限自动停止会置位 stop_event，但不得泄漏到下一项。"""
    wired.stop_event.set()
    wired.run_builtin_step("fake", {})
    assert _Step.seen_stop is False
    assert wired.stop_event.is_set() is False


def test_run_mute_retries_until_audio_appears(monkeypatch):
    """窗口先出现、音频会话后出现：运行级静音重试到会话可用为止。"""
    c = _ctl()
    c.set_mute_game(True)
    attempts = []
    monkeypatch.setattr(c, "_try_capture_volume", lambda: [0.35])
    monkeypatch.setattr(c, "_try_game_volume",
                        lambda level: (attempts.append(level), 2 if len(attempts) >= 3 else 0)[1])

    grp = _Grp()
    c._start_run_mute(grp=grp)
    for _ in range(400):
        if c._run_muted:
            break
        time.sleep(0.01)
    assert c._run_muted is True
    assert c._mute_original == [0.35]
    assert len(attempts) >= 3
    # 措辞即归属（ADR-0017）：静音行以裸行进「其他日志」，前缀标明整轮边界，
    # 不得与「运行收尾」等生命周期组卡头撞名
    assert any("本轮开始：静音游戏" in msg for _, msg in grp.lines)


def test_run_restore_uses_original_volume(monkeypatch):
    """恢复写回用户原音量，不写死 100%；没静过音则不动。"""
    c = _ctl()
    c._run_muted = True
    c._mute_original = [0.35, 0.35]
    calls = []
    monkeypatch.setattr(c, "_apply_game_volume",
                        lambda level, label, **kw: calls.append((level, label)))

    c._restore_run_mute()
    assert calls == [(0.35, "本轮结束：恢复游戏音量（原 35%）")]
    assert c._run_muted is False

    c._run_muted = True  # 重新武装：默认路径没静过音则不动，第二段验证自定时机措辞
    c._mute_original = [0.35, 0.35]
    c._restore_run_mute(context="运行中关闭")
    assert calls[-1][1].startswith("运行中关闭：")
    assert calls[-1][0] == 0.35


def test_mute_toggle_mid_run_restores_and_restarts(monkeypatch):
    c = _ctl()
    c._step_active = True
    started, restored = [], []
    monkeypatch.setattr(c, "_start_run_mute",
                        lambda grp=None, context=None: started.append(context))
    monkeypatch.setattr(c, "_restore_run_mute",
                        lambda grp=None, context=None: restored.append(context))

    c.set_mute_game(True)
    assert started == ["运行中开启"]
    c.set_mute_game(False)
    assert restored == ["运行中关闭"]


def test_late_mute_cannot_land_after_restore(monkeypatch):
    """静音线程读到原值后、设 0 前，运行结束发起恢复：恢复必须是最后一次音量写入。

    修复前读+设在锁外，恢复插进中间后静音才落 0，且线程发现代际不符直接退出
    不补偿——游戏被永远留在静音。修复后读+设+记账同一临界区，恢复只能排在后面。
    """
    c = _ctl()
    c.set_mute_game(True)
    captured = threading.Event()
    release = threading.Event()
    applied: list[float] = []

    def fake_capture():
        captured.set()
        assert release.wait(2.0)
        return [0.35]

    monkeypatch.setattr(c, "_try_capture_volume", fake_capture)
    monkeypatch.setattr(c, "_try_game_volume",
                        lambda level: (applied.append(level), 1)[1])
    monkeypatch.setattr(c, "_apply_game_volume",
                        lambda level, label, **kw: applied.append(level))

    c._start_run_mute()
    assert captured.wait(2.0)
    restorer = threading.Thread(target=c._restore_run_mute, daemon=True)
    restorer.start()
    # 给恢复线程进入的机会：修复前它能立刻走完（代际已 bump）；修复后被锁挡在门外
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline and c._mute_gen == 0:
        time.sleep(0.01)
    release.set()
    restorer.join(2.0)
    c._mute_thread.join(2.0)
    assert not restorer.is_alive() and not c._mute_thread.is_alive()
    assert applied[-1] == 0.35, "恢复必须是最后一次音量写入（游戏不能留在静音）"
    assert c._run_muted is False


def test_restore_write_and_new_mute_serialize(monkeypatch):
    """恢复写入期间新一轮静音不得读音量：否则会把静音中的 0 当成用户原值记走。"""
    c = _ctl()
    c.set_mute_game(True)
    c._run_muted = True
    c._mute_original = [0.35]
    order: list = []
    restoring = threading.Event()
    release = threading.Event()

    def fake_apply(level, label, **kw):
        order.append(("restore", level))
        restoring.set()
        assert release.wait(2.0)

    monkeypatch.setattr(c, "_apply_game_volume", fake_apply)
    monkeypatch.setattr(c, "_try_capture_volume",
                        lambda: (order.append("capture"), [0.35])[1])
    monkeypatch.setattr(c, "_try_game_volume",
                        lambda level: (order.append(("mute", level)), 1)[1])

    restorer = threading.Thread(target=c._restore_run_mute, daemon=True)
    restorer.start()
    assert restoring.wait(2.0)
    # 新一轮的静音获取也在锁上排队（与生产一致：worker 调用），另线程发起
    starter = threading.Thread(target=c._start_run_mute, daemon=True)
    starter.start()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline and "capture" not in order:
        time.sleep(0.01)
    capture_before_release = "capture" in order
    release.set()
    restorer.join(2.0)
    starter.join(2.0)
    c._mute_thread.join(2.0)
    assert not restorer.is_alive() and not c._mute_thread.is_alive()
    assert not capture_before_release, "恢复写入完成前，新静音线程不得读音量"
    assert order[0] == ("restore", 0.35)
