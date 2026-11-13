# -*- coding: utf-8 -*-
"""数据看板路由：新契约、旧模块兼容桥、错误分支与「不建实例」。

导入 core.sidecar 会拉 maa 等重依赖；缺失时整文件 SKIP（同其他 sidecar 行为锁）。
"""
from __future__ import annotations

import pytest

from maaracing_master.core.base import ActivityModule

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.dashboard_contract import DASHBOARD_CONTRACT_VERSION
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"看板路由测试需要完整运行时依赖：{_ERR}"
)


class _NoInstantiate(ActivityModule):
    """构造即失败：只要路由通过，就说明看板取数没有建实例。"""

    def __init__(self, ctx=None):  # pragma: no cover —— 被调用即测试失败
        raise AssertionError("看板路由不得构造模块实例")


class _NewDashboard(_NoInstantiate):
    ID = "new_dashboard"
    NAME = "新看板"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION

    @classmethod
    def read_dashboard(cls):
        return {
            "version": DASHBOARD_CONTRACT_VERSION,
            "title": "测试看板",
            "blocks": [{"type": "text", "text": "ok"}],
        }


class _NoContent(_NoInstantiate):
    ID = "no_content"
    NAME = "无内容"
    HAS_DASHBOARD = True


class _Reserved(_NoInstantiate):
    ID = "reserved"
    NAME = "未实现看板"
    DASHBOARD_CONTRACT = 0


class _BadVersion(_NoInstantiate):
    ID = "bad_version"
    NAME = "坏版本"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION + 1

    @classmethod
    def read_dashboard(cls):
        return {"version": DASHBOARD_CONTRACT_VERSION, "blocks": []}


class _MissingImpl(_NoInstantiate):
    ID = "missing_impl"
    NAME = "缺实现"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION


class _Raises(_NoInstantiate):
    ID = "raises"
    NAME = "抛错"
    DASHBOARD_CONTRACT = DASHBOARD_CONTRACT_VERSION

    @classmethod
    def read_dashboard(cls):
        raise RuntimeError("boom")


def _svc(monkeypatch, modules):
    monkeypatch.setattr(sc, "MODULE_REGISTRY", modules)
    return object.__new__(SidecarService)


def test_new_contract_dashboard_is_normalized(monkeypatch):
    svc = _svc(monkeypatch, {"new_dashboard": _NewDashboard})
    ok, data, err = svc.get_module_dashboard({"module_id": "new_dashboard"})
    assert ok and err is None
    assert data["title"] == "测试看板"
    assert data["blocks"] == [{"type": "text", "text": "ok"}]


def test_unknown_module_rejected(monkeypatch):
    svc = _svc(monkeypatch, {})
    ok, data, err = svc.get_module_dashboard({"module_id": "no_such_module"})
    assert not ok and data is None
    assert "模块不存在" in err
