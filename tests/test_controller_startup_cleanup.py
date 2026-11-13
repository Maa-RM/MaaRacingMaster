# -*- coding: utf-8 -*-
"""启动段异常的统一清理边界：状态获取之后任何一步抛异常，都必须走收尾清理。

修复前：创建/连接/采集在正式执行的 try/finally 之外，connect 构造抛异常只结束
日志组就重抛——active_module / _running 残留，下一轮永远报「已有模块在运行」。
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

    def log(self, msg, level="INFO"):
        pass

    def end(self, outcome=None):
        pass


class _FakeModule:
    STAGE_ORDER = ["待机"]
    ID = "fake"

    def __init__(self):
        self.started = 0
        self.cleanup_calls = 0

    def required_capabilities(self, config):
        return set()

    def start(self, start_from):
        self.started += 1
        return True

    def cleanup(self):
        self.cleanup_calls += 1
        return True


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(ctl, "_run_group", lambda title, kind="session": _Grp())
    with mock.patch.object(
            ctl, "debug_dir",
            lambda: Path(tempfile.gettempdir()) / "maar-test-debug"):
        c = ctl.MaaRacingMasterController()
    c.debug = None
    c.adopt_window(12345)
    c.controller = object()
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


def test_connect_exception_releases_state_and_allows_restart(env, monkeypatch):
    c, mod, released = env

    def boom(self, grp=None):
        raise RuntimeError("连接构造炸了")

    monkeypatch.setattr(type(c), "connect", boom)
    with pytest.raises(RuntimeError, match="连接构造炸了"):
        c.start_module("fake")

    assert c.active_module is None, "启动异常不得残留活动模块"
    assert c._running is False
    assert mod.cleanup_calls == 1
    assert released == ["ctx", "gpad", "wgc"]
    assert c._teardown_failed is False

    # 下一轮正常启动（修复前这里永远报「已有模块在运行」）
    monkeypatch.setattr(type(c), "connect", lambda self, grp=None: True)
    c.start_module("fake")
    assert mod.started == 1


def test_validation_exception_before_activation_keeps_state_clean(env):
    c, mod, _ = env

    with pytest.raises(ValueError, match="断点"):
        c.start_module("fake", "不存在的阶段")

    assert c.active_module is None
    assert c._running is False
    c.start_module("fake")
    assert mod.started == 1