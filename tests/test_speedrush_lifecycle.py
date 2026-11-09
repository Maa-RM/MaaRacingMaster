# -*- coding: utf-8 -*-
"""极速狂飙模块生命周期契约：start 报告真实终态，cleanup 传播收尾失败。

离线形态（ctx=None）+ 纯 Python 桩，不碰窗口、模型与原生句柄。
"""
from __future__ import annotations

import types

import pytest

from maaracing_master.plugins.speedrush import module as sr
from maaracing_master.plugins.speedrush.macro import MacroError
from maaracing_master.plugins.speedrush.module import SpeedRushModule


def test_cleanup_propagates_shutdown_failure_and_keeps_graph():
    mod = SpeedRushModule(None)
    graph = types.SimpleNamespace(shutdown=lambda: False)
    mod._graph = graph

    assert mod.cleanup() is False
    assert mod._graph is graph, "未收摊时保留图引用（销毁权在图自己，模块不得丢给 GC）"


def test_cleanup_success_releases_graph():
    mod = SpeedRushModule(None)
    graph = types.SimpleNamespace(shutdown=lambda: True)
    mod._graph = graph

    assert mod.cleanup() is True
    assert mod._graph is None


def test_cleanup_without_graph_succeeds():
    mod = SpeedRushModule(None)

    assert mod.cleanup() is True


def test_cleanup_returns_false_when_player_stuck():
    """回放线程超时未退仍持有手柄租约：cleanup 必须上报失败，控制器据此不销毁设备。"""
    mod = SpeedRushModule(None)
    player = types.SimpleNamespace(stop=lambda: False)
    mod._macro_player = player
    mod._graph = types.SimpleNamespace(shutdown=lambda: True)

    assert mod.cleanup() is False
    assert mod._macro_player is player, "未退出的回放器引用保留（租约仍被持有）"


def test_cleanup_success_releases_player():
    mod = SpeedRushModule(None)
    mod._macro_player = types.SimpleNamespace(stop=lambda: True)

    assert mod.cleanup() is True
    assert mod._macro_player is None


# ---------------------------------------------------------------------------
#  start() 终态：失败出口返回 False；停止/自然结束返回 True
# ---------------------------------------------------------------------------

@pytest.fixture()
def mod(monkeypatch):
    """离线模块 + 日志桩：只测返回值决策，不写真实日志组。"""
    for name in ("_tlog", "_open_grp", "_end_grp", "_sync_log_group"):
        monkeypatch.setattr(sr, name, lambda *a, **k: None)
    ctx = types.SimpleNamespace(
        connect=lambda: True,
        check_window_aspect=lambda: True,
        click_mode="real",
        lifecycle=types.SimpleNamespace(running=True),
    )
    m = SpeedRushModule(ctx)
    m._drive_mode = "straight"   # 跳过模型包校验，各用例按需改写
    m._start_preload = lambda: None
    return m


class _FakeGraph:
    def __init__(self, load_ok=True):
        self._load_ok = load_ok

    def add_plugin(self, *a):
        pass

    def load(self):
        return self._load_ok


def test_start_returns_false_when_model_pack_missing(mod, monkeypatch):
    mod._drive_mode = "depth"
    monkeypatch.setattr(sr.model_assets, "check_pack", lambda: "模型包缺失")

    assert mod.start() is False


def test_start_returns_false_when_connect_fails(mod):
    mod.ctx.connect = lambda: False

    assert mod.start() is False


def test_start_returns_false_when_aspect_wrong(mod):
    mod.ctx.check_window_aspect = lambda: False

    assert mod.start() is False


def test_start_returns_false_when_graph_load_fails(mod, monkeypatch):
    monkeypatch.setattr(sr, "NavGraph", lambda ctx: _FakeGraph(load_ok=False))

    assert mod.start() is False


def test_start_returns_false_when_macro_group_unavailable(mod, monkeypatch):
    mod._drive_mode = "macro"
    monkeypatch.setattr(sr, "NavGraph", lambda ctx: _FakeGraph())

    def boom(group):
        raise MacroError("组不存在")

    monkeypatch.setattr(sr, "resolve_group", boom)

    assert mod.start() is False


def test_start_returns_false_when_flow_fails(mod, monkeypatch):
    monkeypatch.setattr(sr, "NavGraph", lambda ctx: _FakeGraph())
    mod._run_flow = lambda index: False

    assert mod.start() is False


def test_start_returns_true_when_flow_stops(mod, monkeypatch):
    monkeypatch.setattr(sr, "NavGraph", lambda ctx: _FakeGraph())
    mod._run_flow = lambda index: True

    assert mod.start() is True


# ---------------------------------------------------------------------------
#  _run_flow() 出口映射：停止（任一运行标志落下）→ True；失败 → False
# ---------------------------------------------------------------------------

def test_flow_returns_true_when_stopped_before_loop(mod):
    mod._graph = object()
    mod._running = False

    assert mod._run_flow(0) is True


def test_flow_returns_false_when_drive_aborts(mod, monkeypatch):
    monkeypatch.setattr(sr, "FLOW", [("驾驶", None, 1)])
    mod._graph = object()
    mod._running = True
    mod._drive = lambda *a, **k: False

    assert mod._run_flow(0) is False


def test_flow_returns_true_when_drive_stopped_by_signal(mod, monkeypatch):
    monkeypatch.setattr(sr, "FLOW", [("驾驶", None, 1)])
    mod._graph = object()
    mod._running = True
    mod.ctx.lifecycle.running = False   # 到限自动停止只置 stop_event，不动 _running
    mod._drive = lambda *a, **k: False

    assert mod._run_flow(0) is True


def test_flow_returns_false_when_step_retries_exhausted(mod, monkeypatch):
    monkeypatch.setattr(sr, "FLOW", [("步骤", "node", 0)])
    mod._graph = object()
    mod._running = True
    mod._run_step = lambda node, stage: False

    assert mod._run_flow(0) is False


def test_flow_returns_true_when_step_stopped_by_signal(mod, monkeypatch):
    monkeypatch.setattr(sr, "FLOW", [("步骤", "node", 0)])
    mod._graph = object()
    mod._running = True
    mod.ctx.lifecycle.running = False
    mod._run_step = lambda node, stage: False

    assert mod._run_flow(0) is True