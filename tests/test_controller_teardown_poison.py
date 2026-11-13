# -*- coding: utf-8 -*-
"""收尾失败向上传播（ADR-0018 决策 6 的模块路径）。

框架 worker 未收摊时：模块 cleanup 返回 False，控制器保留宿主资源、
把失败对象隔离持有（防垃圾回收析构仍被原生线程使用的句柄）、标污染态
并拒绝下一轮。正常收尾路径不受影响的用例一并锁在这里。

手段：真实控制器 + 纯 Python 桩（模块/图/外设），不构造真实 C 句柄。
"""
from __future__ import annotations

import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest

from maaracing_master.core import controller as ctl


class _Grp:
    def __init__(self):
        self.id = "g"
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO"):
        self.lines.append((level, msg))

    def end(self, outcome=None):
        pass


def _ctl() -> ctl.MaaRacingMasterController:
    with mock.patch.object(
            ctl, "debug_dir",
            lambda: Path(tempfile.gettempdir()) / "maar-test-debug"):
        c = ctl.MaaRacingMasterController()
    c.debug = None
    return c


class _FakeModule:
    STAGE_ORDER = ["待机"]
    ID = "fake"

    def __init__(self):
        self.start_result = True
        self.cleanup_result = True
        self.cleanup_calls = 0

    def required_capabilities(self, config):
        return set()

    def start(self, start_from):
        return self.start_result

    def cleanup(self):
        self.cleanup_calls += 1
        if isinstance(self.cleanup_result, Exception):
            raise self.cleanup_result
        return self.cleanup_result


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(ctl, "_run_group", lambda title, kind="session": _Grp())
    c = _ctl()
    c.adopt_window(12345)
    c.controller = object()  # 已连接哨兵：connect() 幂等短路
    monkeypatch.setattr(ctl, "is_window_valid", lambda hwnd: True)
    released: list[str] = []
    c._destroy_gpad = lambda grp=None: released.append("gpad")
    c._stop_wgc_capture = lambda: released.append("wgc")
    c._start_wgc_capture = lambda grp=None: None
    mod = _FakeModule()
    monkeypatch.setattr(ctl, "create_module", lambda mid, ctx: mod)
    fake_ctx = types.SimpleNamespace(
        capabilities=set(), close=lambda: released.append("ctx"))
    monkeypatch.setattr(type(c), "ctx", property(lambda self: fake_ctx))
    c._ctx = fake_ctx
    return c, mod, released


def test_teardown_failure_parks_module_and_keeps_host_resources(env):
    c, mod, released = env
    mod.cleanup_result = False

    c.start_module("fake")

    assert c._teardown_failed is True
    assert released == [], "未收摊时不得释放宿主资源（Context/手柄/采集）"
    assert mod in c._teardown_quarantine, "失败模块须隔离持有，GC 不能析构其原生句柄"
    assert c.active_module is None
    assert c._running is False
    with pytest.raises(RuntimeError, match="收尾未完成"):
        c.start_module("fake")


def test_teardown_failure_revokes_natural_result(env):
    """执行成功但收尾失败：自然完成必须撤销（侧车据此判 fail，与内置步骤同口径）。"""
    c, mod, _ = env
    mod.start_result = True
    mod.cleanup_result = False

    c.start_module("fake")

    assert c._teardown_failed is True
    assert c.last_run_natural is False, "收尾失败不得仍报自然完成"


def test_teardown_exception_treated_as_failure(env):
    c, mod, released = env
    mod.cleanup_result = RuntimeError("收尾炸了")

    c.start_module("fake")

    assert c._teardown_failed is True
    assert released == []
    assert mod in c._teardown_quarantine


def test_teardown_success_releases_and_allows_next_round(env):
    c, mod, released = env

    c.start_module("fake")

    assert c._teardown_failed is False
    assert released == ["ctx", "gpad", "wgc"]
    assert c._teardown_quarantine == []
    c.start_module("fake")
    assert mod.cleanup_calls == 2


def test_step_session_parks_graph_when_shutdown_fails():
    c = _ctl()
    session = ctl.StepSession(c, frozenset())
    graph = types.SimpleNamespace(shutdown=lambda: False)
    session._graph = graph

    assert session.close() is False
    assert graph in c._teardown_quarantine, "会话路径同样不许把未收摊的图留给 GC"


def test_step_session_releases_normally_when_shutdown_succeeds():
    c = _ctl()
    released: list[str] = []
    c._destroy_gpad = lambda grp=None: released.append("gpad")
    c._stop_wgc_capture = lambda: released.append("wgc")
    session = ctl.StepSession(c, frozenset())
    graph = types.SimpleNamespace(shutdown=lambda: True)
    session._graph = graph
    session._touched = True
    c._gpad_owner = "step"

    assert session.close() is True
    assert c._teardown_quarantine == []
    assert released == ["gpad", "wgc"]