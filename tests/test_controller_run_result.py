# -*- coding: utf-8 -*-
"""运行终态归约：模块 start() 的返回值决定「自然完成」，不再凭「没抛异常」。

判定表（侧车消费 last_run_natural）：
  - start 返回 False（启动校验/流程失败）→ 非自然，该项 fail、中止整轮；
  - 返回 True 且非手动停止 → 自然，该项 ok；
  - 手动停止优先于自然（用户停止不算完成也不算失败）；
  - 抛异常 → 非自然（既有行为，回归锁）。
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
        self.start_result = True
        self.on_start = None

    def required_capabilities(self, config):
        return set()

    def start(self, start_from):
        if self.on_start:
            self.on_start()
        if isinstance(self.start_result, Exception):
            raise self.start_result
        return self.start_result

    def cleanup(self):
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
    c._destroy_gpad = lambda grp=None: None
    c._stop_wgc_capture = lambda: None
    c._start_wgc_capture = lambda grp=None: None
    mod = _FakeModule()
    monkeypatch.setattr(ctl, "create_module", lambda mid, ctx: mod)
    fake_ctx = types.SimpleNamespace(capabilities=set())
    monkeypatch.setattr(type(c), "ctx", property(lambda self: fake_ctx))
    return c, mod


def test_module_failure_result_marks_run_not_natural(env):
    c, mod = env
    mod.start_result = False

    c.start_module("fake")

    assert c.last_run_natural is False, "模块报失败不得记成自然完成（侧车据此判 fail）"


def test_module_success_result_marks_run_natural(env):
    c, mod = env
    mod.start_result = True

    c.start_module("fake")

    assert c.last_run_natural is True


def test_manual_stop_overrides_natural(env):
    c, mod = env
    mod.on_start = lambda: setattr(c, "_manual_stop", True)

    c.start_module("fake")

    assert c.last_run_natural is False


def test_module_exception_marks_run_not_natural(env):
    c, mod = env
    mod.start_result = RuntimeError("boom")

    c.start_module("fake")

    assert c.last_run_natural is False