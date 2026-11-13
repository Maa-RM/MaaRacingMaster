# -*- coding: utf-8 -*-
"""插件模板契约锁：templates/plugin/module.py 必须与 ActivityModule 的 bool 契约一致。

模板是新插件的起点；start/cleanup 返回 None 会被编排层判成「非自然完成 / 收尾失败」，
示例插件一跑就被污染态拒绝。这里把契约钉在模板上。
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path


def _template():
    path = Path(__file__).resolve().parents[1] / "templates" / "plugin" / "module.py"
    spec = importlib.util.spec_from_file_location("plugin_template_module", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_template_cleanup_returns_true():
    m = _template().SampleModule(None)
    assert m.cleanup() is True


def test_template_start_returns_false_on_connect_failure():
    ctx = types.SimpleNamespace(connect=lambda: False)
    m = _template().SampleModule(ctx)
    assert m.start() is False


def test_template_start_returns_true_on_stop():
    ctx = types.SimpleNamespace(
        connect=lambda: True,
        lifecycle=types.SimpleNamespace(running=False))
    m = _template().SampleModule(ctx)
    assert m.start() is True