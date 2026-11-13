# -*- coding: utf-8 -*-
"""启动受理与停止的竞态锁：停止信号在受理时清，worker 启动后不再清。

竞态窗口（修复前）：start 受理创建 worker → 用户按停止 → worker 才执行
begin_run 无条件清信号 → 停止丢失、整轮照跑。修复后清信号发生在受理锁内，
worker 创建之前；受理后到达的停止必然被循环判定看到。

手段：门控线程桩让 worker「已创建、未调度」，把竞态窗口变成确定时序。
"""
from __future__ import annotations

import tempfile
import threading
import types
from pathlib import Path
from unittest import mock

import pytest

try:
    from maaracing_master.core import controller as ctl
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"需要完整运行时依赖：{_ERR}"
)


class _GatedThread:
    """受理但不调度：start() 空转，测试手动决定何时执行 worker 体。"""

    def __init__(self, target, args=(), daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        pass

    def is_alive(self):
        return True

    def run_target(self):
        self.target(*self.args)


def _ctl() -> ctl.MaaRacingMasterController:
    with mock.patch.object(
            ctl, "debug_dir",
            lambda: Path(tempfile.gettempdir()) / "maar-test-debug"):
        c = ctl.MaaRacingMasterController()
    c.debug = None
    return c


def _svc(controller):
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._closed = False
    svc._controller = controller
    svc._worker = None
    svc._queue_order = ["a"]
    svc._queue_enabled = {"a": True}
    svc._start_from = {}
    svc._module_config_cache = {}
    svc._queue_run = None
    svc._round_result = None
    sent = []
    svc.send = sent.append
    return svc, sent


def _patch_registry(monkeypatch):
    monkeypatch.setattr(sc, "has_physical_controller", lambda: False)
    monkeypatch.setattr(sc, "check_required_assets", lambda mid: [])
    monkeypatch.setattr(
        sc, "get_module_info",
        lambda mid: {"id": mid, "name": mid, "stages": [], "expired": False})
    cls = type("A", (), {
        "ID": "a",
        "required_capabilities": classmethod(lambda cls, cfg=None: frozenset()),
        "requires_exclusive_gamepad": classmethod(lambda cls, cfg=None: False),
    })
    monkeypatch.setattr(sc, "MODULE_REGISTRY", {"a": cls})


def test_stop_between_acceptance_and_worker_start_survives(monkeypatch):
    c = _ctl()
    svc, sent = _svc(c)
    _patch_registry(monkeypatch)
    monkeypatch.setattr(sc, "threading", types.SimpleNamespace(
        Thread=_GatedThread,
        current_thread=threading.current_thread,
        get_ident=threading.get_ident,
        Lock=threading.Lock, RLock=threading.RLock,
        Semaphore=threading.Semaphore))
    # 上一轮遗留的停止：受理必须清掉（否则本轮第一项被误判 stopped）
    c._manual_stop = True
    c.stop_event.set()

    ok, _, err = svc.start({})
    assert ok, err
    assert c._manual_stop is False, "清停止信号必须发生在受理时（worker 创建前）"
    assert not c.stop_event.is_set()

    # worker 尚未调度时用户按停止
    svc.stop({})
    assert c._manual_stop is True

    # 现在才执行 worker：停止必须仍然可见，模块不得启动
    svc._worker.run_target()
    assert svc._round_result == {"a": "stopped"}
    assert c.active_module is None