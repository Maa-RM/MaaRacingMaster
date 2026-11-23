# -*- coding: utf-8 -*-
"""启动步骤的启动器检查闸门：分流、代点更新与干净收停
（闸门事实来自 2026-11-22 实测，证据以 commit 46727db 为指针）。

判定表：
  闸门关（环境开关）→ 不查，沿用直启；
  无启动器 / 拉起失败 → unavailable，直启照旧；
  无需更新 → 关启动器、直启；
  需要更新 + 开关关 → 不拉游戏、留启动器页面，结局 needs_update；
  需要更新 + 开关开 → 出窗后代点、等装好、关启动器、直启；
  启动器自行拉起游戏（updated_game）→ 接管现有窗口，不起第二份。

conftest 的 autouse 夹具默认置 MAARM_APP_LAUNCHER_CHECK=0；本文件各用例显式
打开并打桩，不碰真启动器。
"""
from __future__ import annotations

import threading
import types

import pytest

from maaracing_master.core import builtin_steps as bs
from maaracing_master.core import game_process as gp


class _App:
    def __init__(self, intent: bool = False, mode: str = "real"):
        self.intent_mode = intent
        self.click_mode = mode
        self.hwnd = 0
        self.adopted: list[int] = []

    def adopt_window(self, hwnd):
        self.adopted.append(hwnd)
        self.hwnd = hwnd

    def forget_window(self):
        self.hwnd = 0

    def request_exit(self):
        pass


class _Session:
    def __init__(self):
        self.started = False
        self.graph_calls = 0
        self.seen: dict = {}

    def start(self):
        self.started = True
        return True

    def graph(self, pipeline, image_dir):
        self.graph_calls += 1
        return types.SimpleNamespace(
            run=lambda *a, **k: True, last_outcome="reached")


@pytest.fixture()
def gate_on(monkeypatch, tmp_path):
    """闸门打开 + 启动器交互全部打桩。"""
    monkeypatch.setenv("MAARM_APP_LAUNCHER_CHECK", "1")
    launcher = tmp_path / "Launcher.exe"
    game = tmp_path / "g112-Win64-Shipping.exe"
    launcher.write_bytes(b"L")
    game.write_bytes(b"G")
    monkeypatch.setattr(gp, "detect_launcher_exe", lambda: launcher)
    monkeypatch.setattr(gp, "detect_game_exe", lambda: game)
    monkeypatch.setattr(gp, "game_running", lambda name: False)
    state: dict = {"launcher_pids": [], "game_pids": [], "closed": 0,
                   "clicked": [], "check": {"update_needed": False,
                                            "size": 0, "files": 0},
                   "ready": "ready"}
    monkeypatch.setattr(gp, "spawn_launcher",
                        lambda exe: state["launcher_pids"].append(exe.name) or 4321)
    monkeypatch.setattr(gp, "close_launcher", lambda: state.__setitem__(
        "closed", state["closed"] + 1) or "closed")
    monkeypatch.setattr(gp, "wait_launcher_check",
                        lambda stop_event, timeout_s: dict(state["check"]))
    monkeypatch.setattr(gp, "click_launcher_update_button",
                        lambda hwnd: state["clicked"].append(hwnd) or True)
    monkeypatch.setattr(gp, "wait_launcher_ready",
                        lambda stop_event, timeout_s, log=None: state["ready"])
    monkeypatch.setattr(gp, "spawn",
                        lambda exe, args: state["game_pids"].append(exe.name)
                        or types.SimpleNamespace(pid=1))

    def _window_stub(name, timeout, stop):
        # 启动器窗口 999 / 游戏窗口 1234
        return 999 if name == gp.LAUNCHER_EXE_NAME else 1234

    monkeypatch.setattr(gp, "wait_for_window", _window_stub)
    return state


def _run(cfg=None, app=None, stop=None):
    logs: list[tuple[str, str]] = []
    app = app or _App()
    stop = stop or threading.Event()
    step = bs.LaunchGameStep(app)
    ok = step.run(dict(cfg or {}), stop, lambda m, lv="INFO": logs.append((lv, m)),
                  _Session())
    return ok, step, logs, app


def test_gate_env_off_skips_check_and_launches_directly(monkeypatch, gate_on):
    """环境开关关闭（conftest 默认态的显式复核）：不拉启动器，直启照旧。"""
    monkeypatch.setenv("MAARM_APP_LAUNCHER_CHECK", "0")
    ok, step, logs, _app = _run()
    assert ok is True and step.last_outcome == "completed"
    assert gate_on["launcher_pids"] == []
    assert gate_on["game_pids"] == ["g112-Win64-Shipping.exe"]


def test_no_update_closes_launcher_and_launches(gate_on):
    ok, _step, logs, _app = _run()
    assert ok is True
    assert gate_on["launcher_pids"] == ["Launcher.exe"]
    assert gate_on["closed"] == 1
    assert gate_on["game_pids"] == ["g112-Win64-Shipping.exe"]
    assert any("无需更新" in m for _lv, m in logs)


def test_update_without_toggle_stops_cleanly_leaving_launcher(gate_on):
    gate_on["check"] = {"update_needed": True, "size": 200_000_000, "files": 2}
    ok, step, logs, _app = _run()
    assert ok is False and step.last_outcome == "needs_update"
    assert gate_on["game_pids"] == []          # 不直启过期的游戏
    assert gate_on["closed"] == 0              # 启动器页面留着
    assert gate_on["clicked"] == []            # 不代点
    assert any(lv == "WARNING" and "游戏需要更新" in m for lv, m in logs)


def test_update_with_toggle_clicks_updates_then_launches(gate_on):
    gate_on["check"] = {"update_needed": True, "size": 200_000_000, "files": 2}
    ok, step, logs, _app = _run({"auto_update": True})
    assert ok is True and step.last_outcome == "completed"
    assert gate_on["clicked"] == [999]
    assert gate_on["closed"] == 1
    assert gate_on["game_pids"] == ["g112-Win64-Shipping.exe"]
    assert any("补丁安装完成" in m for _lv, m in logs)


def test_update_toggle_window_missing_goes_manual(gate_on, monkeypatch):
    gate_on["check"] = {"update_needed": True, "size": 200_000_000, "files": 2}
    monkeypatch.setattr(gp, "wait_for_window",
                        lambda name, timeout, stop: 0)  # 启动器窗口一直没出
    ok, step, _logs, _app = _run({"auto_update": True})
    assert ok is False and step.last_outcome == "needs_update"
    assert gate_on["clicked"] == []


def test_launcher_starts_game_is_adopted_not_relaunched(gate_on):
    """updated_game：启动器自行拉起了游戏就接管，不起第二份。"""
    gate_on["check"] = {"update_needed": True, "size": 200_000_000, "files": 2}
    gate_on["ready"] = "game_started"
    ok, _step, logs, app = _run({"auto_update": True})
    assert ok is True
    assert gate_on["game_pids"] == []          # 没有第二个游戏进程
    assert app.hwnd == 1234
    assert any("接管" in m or "自行拉起" in m for _lv, m in logs)


def test_stop_during_check_maps_to_stopped(gate_on):
    stop = threading.Event()
    stop.set()
    ok, step, _logs, _app = _run({}, stop=stop)
    assert ok is False and step.last_outcome == "stopped"


def test_no_launcher_found_goes_direct(gate_on, monkeypatch):
    monkeypatch.setattr(gp, "detect_launcher_exe", lambda: None)
    ok, _step, logs, _app = _run()
    assert ok is True
    assert gate_on["launcher_pids"] == []
    assert any(lv == "WARNING" and "跳过版本检查" in m for lv, m in logs)
