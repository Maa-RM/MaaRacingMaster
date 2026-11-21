# -*- coding: utf-8 -*-
"""插件资产安装的宿主接缝：白名单动作、准入互斥、声明式通用能力回归锁。

本文件同时是「未来模块也能自己下载资产」的证据：宿主只认 manifest 声明，
不认模块实现；下面用两个互不相关的假模块走通 start/status/cancel。
"""

from __future__ import annotations

import threading
from pathlib import Path
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
OTHER_MODULE_ID = "install_test_module_b"
REPO_ROOT = Path(__file__).resolve().parents[1]

_DECL = {
    "ASSET_INSTALLER": "fake.Installer",
    "ASSET_INSTALLER_LABEL": "测试资产",
    "ASSET_INSTALLER_MISSING_HINT": "测试资产未安装",
}


class FakeInstaller:
    def __init__(self, phase="downloading", tag=""):
        self._phase = phase
        self._tag = tag
        self.cancelled = False
        self.joined = False

    def status(self):
        return SimpleNamespace(
            task_id="task-1", phase=self._phase, revision=1, downloaded=5,
            total=10, source="local", error=None,
            manifest_digest="module-private-field",
        )

    def start(self):
        return True, self.status()

    def cancel(self):
        self.cancelled = True
        return self.status()

    def join(self, timeout):
        self.joined = True
        return True

    def needs_install(self):
        return True


def _svc() -> SidecarService:
    with mock.patch.object(ctl, "debug_dir", lambda: Path(
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
    monkeypatch.setattr(sc, "plugin_declared_ref", lambda mid, field: _DECL.get(field))
    created = []

    def factory():
        inst = FakeInstaller()
        created.append(inst)
        return inst

    monkeypatch.setattr(sc, "load_plugin_ref", lambda mid, ref: factory)
    return created


def test_status_is_idle_before_any_install():
    svc = _svc()
    ok, data, err = svc.install_asset_status({"module_id": MODULE_ID})
    assert ok and not err and data["phase"] == "idle"
    assert data["supported"] is False


def test_start_returns_monotonic_status_and_records_installer(with_installer):
    svc = _svc()
    ok, data, err = svc.install_asset_start({"module_id": MODULE_ID})
    assert ok and not err
    assert data == {
        "task_id": "task-1", "phase": "downloading", "revision": 1,
        "downloaded": 5, "total": 10, "source": "local", "error": None,
        "label": "测试资产", "missing_hint": "测试资产未安装",
    }
    assert len(with_installer) == 1
    ok, data, err = svc.install_asset_status({"module_id": MODULE_ID})
    assert data["task_id"] == "task-1"


def test_host_payload_carries_no_module_private_fields(with_installer):
    """宿主契约只收公共字段：插件私有的摘要/版本键不得泄进 RPC。"""
    svc = _svc()
    _ok, data, _err = svc.install_asset_start({"module_id": MODULE_ID})
    assert "manifest_digest" not in data


def test_start_refused_while_module_worker_runs(with_installer):
    svc = _svc()
    svc._worker = threading.Thread(target=lambda: None)
    ok, _data, err = svc.install_asset_start({"module_id": MODULE_ID})
    assert not ok and "任务运行中" in err
    assert not with_installer


def test_start_refused_during_update_freeze(with_installer):
    svc = _svc()
    svc._update_freeze = True
    ok, _data, err = svc.install_asset_start({"module_id": MODULE_ID})
    assert not ok and "升级准备" in err


def test_start_refused_while_install_is_busy(with_installer):
    svc = _svc()
    assert svc.install_asset_start({"module_id": MODULE_ID})[0]
    ok, _data, err = svc.install_asset_start({"module_id": MODULE_ID})
    assert not ok and "资产安装进行中" in err
    assert len(with_installer) == 1


def test_game_start_refused_while_install_is_busy(with_installer):
    """反向准入：安装进行中不得启动任务，不能只靠前端置灰。"""
    svc = _svc()
    assert svc.install_asset_start({"module_id": MODULE_ID})[0]
    ok, _data, err = svc.start({})
    assert not ok and "资产安装进行中" in err


def test_prepare_update_refused_while_install_is_busy(with_installer):
    svc = _svc()
    svc.install_asset_start({"module_id": MODULE_ID})
    ok, _data, err = svc.prepare_update({})
    assert not ok and "资产安装中" in err
    assert not svc._update_freeze


def test_close_cancels_and_bounds_installer(with_installer):
    svc = _svc()
    svc.install_asset_start({"module_id": MODULE_ID})
    svc.close({})
    assert with_installer[0].cancelled and with_installer[0].joined


def test_second_module_routes_to_its_own_installer(monkeypatch):
    """通用能力证据：第二个模块声明安装器即可被宿主路由，core 无需改动。"""
    monkeypatch.setitem(sc.MODULE_REGISTRY, MODULE_ID, object)
    monkeypatch.setitem(sc.MODULE_REGISTRY, OTHER_MODULE_ID, object)
    declared = {MODULE_ID, OTHER_MODULE_ID}
    monkeypatch.setattr(
        sc, "plugin_declared_ref",
        lambda mid, field: _DECL.get(field) if mid in declared else None)
    built: dict[str, FakeInstaller] = {}

    def loader(mid, ref):
        def factory():
            built.setdefault(mid, FakeInstaller())
            return built[mid]
        return factory

    monkeypatch.setattr(sc, "load_plugin_ref", loader)
    svc = _svc()
    assert svc.install_asset_start({"module_id": OTHER_MODULE_ID})[0]
    _ok, other, _err = svc.install_asset_status({"module_id": OTHER_MODULE_ID})
    assert other["task_id"] == "task-1"
    assert set(built) == {OTHER_MODULE_ID}
    # 未声明的模块保持隐藏，不因别的模块在安装而被连带唤起
    ok, data, _err = svc.install_asset_status({"module_id": "no_installer_module"})
    assert ok and data["supported"] is False


def test_actions_tolerate_malformed_params():
    """参数不是 dict 时按「未知模块」拒绝，不抛异常打断 RPC 派发。"""
    svc = _svc()
    ok, data, err = svc.install_asset_status(None)
    assert ok and not err and data["supported"] is False and data["label"] == "资产"
    for method in (svc.install_asset_start, svc.install_asset_cancel):
        ok, _data, err = method([])
        assert not ok and "未知模块" in err


def test_bad_declaration_does_not_break_rpc(monkeypatch):
    """第三方插件写错能力引用，只让该模块不可用，不把宿主 RPC 打断。"""
    monkeypatch.setitem(sc.MODULE_REGISTRY, MODULE_ID, object)
    monkeypatch.setattr(sc, "plugin_declared_ref", lambda mid, field: _DECL.get(field))

    def broken(mid, ref):
        raise ImportError("no such installer")

    monkeypatch.setattr(sc, "load_plugin_ref", broken)
    svc = _svc()
    ok, _data, err = svc.install_asset_start({"module_id": MODULE_ID})
    assert not ok and "安装器不可用" in err


def test_host_install_surface_has_no_module_specific_semantics():
    """宿主安装面不得出现模块实现或模块语义：RPC 名、默认文案、状态字段都要通用。"""
    sidecar_src = (REPO_ROOT / "maaracing_master" / "core" / "sidecar.py").read_text(
        encoding="utf-8")
    assert "infinite_drive" not in sidecar_src
    assert "from maaracing_master.plugins" not in sidecar_src
    panel_src = (REPO_ROOT / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js"
                 / "config-panel.js").read_text(encoding="utf-8")
    assert "一键安装模型" not in panel_src
    assert "模型已就绪" not in panel_src
    assert "install_model_" not in panel_src
