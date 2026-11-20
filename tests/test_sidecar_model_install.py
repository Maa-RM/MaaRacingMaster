# -*- coding: utf-8 -*-
"""插件资产安装的宿主接缝：白名单动作、准入互斥与冻结/关闭收尾回归锁。"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest import mock

import pytest

try:
    from maaracing_master.core import controller as ctl
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")

MODULE_ID = "install_test_module"


class FakeInstaller:
    def __init__(self, phase="downloading"):
        self._phase = phase
        self.cancelled = False
        self.joined = False

    def status(self):
        return SimpleNamespace(
            task_id="task-1", phase=self._phase, revision=1, downloaded=5,
            total=10, source="local", error=None, manifest_digest="digest",
        )

    def start(self):
        return True, self.status()

    def cancel(self):
        self.cancelled = True
        return self.status()

    def join(self, timeout):
        self.joined = True
        return True


def _svc() -> SidecarService:
    with mock.patch.object(ctl, "debug_dir", lambda: __import__("pathlib").Path(
            __import__("tempfile").gettempdir()) / "maar-test-debug"):
        controller = ctl.MaaRacingMasterController()
    controller.debug = None
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._closed = False
    svc._update_freeze = False
    svc._controller = controller
    svc._worker = None
    svc._asset_installers = {}
    svc._queue_order = ["a"]
    svc._queue_enabled = {"a": True}
    svc._start_from = {}
    svc._module_config_cache = {}
    svc._queue_run = None
    svc._round_result = None
    return svc


@pytest.fixture
def with_installer(monkeypatch):
    monkeypatch.setitem(sc.MODULE_REGISTRY, MODULE_ID, object)
    monkeypatch.setattr(sc, "plugin_declared_ref", lambda mid, field: "fake.Installer")
    created = []

    def factory():
        inst = FakeInstaller()
        created.append(inst)
        return inst

    monkeypatch.setattr(sc, "load_plugin_ref", lambda mid, ref: factory)
    return created


def test_status_is_idle_before_any_install():
    svc = _svc()
    ok, data, err = svc.install_model_status({"module_id": MODULE_ID})
    assert ok and not err and data["phase"] == "idle"


def test_start_returns_monotonic_status_and_records_installer(with_installer):
    svc = _svc()
    ok, data, err = svc.install_model_start({"module_id": MODULE_ID})
    assert ok and not err
    assert data == {
        "task_id": "task-1", "phase": "downloading", "revision": 1,
        "downloaded": 5, "total": 10, "source": "local",
        "error": None, "manifest_digest": "digest",
    }
    assert len(with_installer) == 1
    ok, data, err = svc.install_model_status({"module_id": MODULE_ID})
    assert data["task_id"] == "task-1"


def test_start_refused_while_module_worker_runs(with_installer):
    svc = _svc()
    svc._worker = threading.Thread(target=lambda: None)
    ok, _data, err = svc.install_model_start({"module_id": MODULE_ID})
    assert not ok and "任务运行中" in err
    assert not with_installer


def test_start_refused_during_update_freeze(with_installer):
    svc = _svc()
    svc._update_freeze = True
    ok, _data, err = svc.install_model_start({"module_id": MODULE_ID})
    assert not ok and "升级准备" in err


def test_prepare_update_refused_while_install_is_busy(with_installer):
    svc = _svc()
    svc.install_model_start({"module_id": MODULE_ID})
    ok, _data, err = svc.prepare_update({})
    assert not ok and "模型安装中" in err
    assert not svc._update_freeze


def test_close_cancels_and_bounds_installer(with_installer):
    svc = _svc()
    svc.install_model_start({"module_id": MODULE_ID})
    svc.close({})
    assert with_installer[0].cancelled and with_installer[0].joined
