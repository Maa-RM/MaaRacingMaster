# -*- coding: utf-8 -*-
"""升级准备冻结准入的竞态锁。

「空闲检查＋冻结新任务」必须原子：prepare_update 在受理锁内一次完成
空闲判定与冻结置位，start 的拒绝分支在同一个锁内读到冻结位——冻结之后
到达的 start 不可能漏判。重复准备幂等；解冻幂等；关闭优先于冻结。

模型安装占用是同层竞争者，其状态落地后在本文件补对应用例；冻结判定集中
在一处，接入即被覆盖。
"""

from __future__ import annotations

import threading
from unittest import mock

import pytest

try:
    from maaracing_master.core import controller as ctl
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _OK, reason=f"需要完整运行时依赖：{_ERR}"
)


def _svc() -> tuple[SidecarService, ctl.MaaRacingMasterController]:
    with mock.patch.object(
            ctl, "debug_dir",
            lambda: __import__("pathlib").Path(__import__("tempfile").gettempdir()) / "maar-test-debug"):
        c = ctl.MaaRacingMasterController()
    c.debug = None
    svc = object.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._closed = False
    svc._update_freeze = False
    svc._controller = c
    svc._worker = None
    svc._queue_order = ["a"]
    svc._queue_enabled = {"a": True}
    svc._start_from = {}
    svc._module_config_cache = {}
    svc._queue_run = None
    svc._round_result = None
    sent: list = []
    svc.send = sent.append
    return svc, c


def test_prepare_freeze_blocks_start():
    """空闲时冻结成功；冻结后 start 被拒，原因点名升级准备。"""
    svc, _ = _svc()
    ok, data, err = svc.prepare_update({})
    assert ok, err
    assert ok and svc._update_freeze
    ok2, _, err2 = svc.start({})
    assert not ok2 and "升级准备" in (err2 or ""), (ok2, err2)


def test_prepare_busy_while_worker_alive():
    """任务运行中冻结被拒（原因点名运行）；拒绝不置冻结位。"""
    svc, _ = _svc()
    svc._worker = threading.Thread(target=lambda: None, daemon=True)
    ok, _, err = svc.prepare_update({})
    assert not ok and "运行" in (err or ""), (ok, err)
    assert not svc._update_freeze, "busy 时不得置冻结位"


def test_prepare_while_closed():
    """已关闭：冻结被拒（原因点名关闭），不置冻结位。"""
    svc, _ = _svc()
    svc._closed = True
    ok, _, err = svc.prepare_update({})
    assert not ok and "关闭" in (err or ""), (ok, err)
    assert not svc._update_freeze


def test_prepare_idempotent():
    """重复准备幂等：两次都成功，不产生第二种状态。"""
    svc, _ = _svc()
    ok1, _, err1 = svc.prepare_update({})
    ok2, _, err2 = svc.prepare_update({})
    assert ok1 and ok2, (err1, err2)
    assert svc._update_freeze
    ok3, _, err3 = svc.start({})
    assert not ok3 and "升级准备" in (err3 or "")


def test_cancel_unfreezes():
    """解冻后 start 恢复可用；解冻幂等。"""
    svc, _ = _svc()
    svc.prepare_update({})
    assert svc.cancel_update_freeze({})[0]
    assert not svc._update_freeze
    assert svc.cancel_update_freeze({})[0]  # 幂等
    ok, _, err = svc.start({})
    # start 依赖注册表与模块状态，这里只断言不再被「升级准备」拒绝
    assert "升级准备" not in (err or ""), (ok, err)


def test_freeze_does_not_leak_across_close():
    """关闭后冻结位失去意义：close 置 _closed，重启会话是全新状态（无持久化）。"""
    svc, _ = _svc()
    svc.prepare_update({})
    ok, _, err = svc.close({})
    assert ok, err
    ok2, _, err2 = svc.prepare_update({})
    assert not ok2 and "关闭" in (err2 or "")
    # start 的冻结拒绝不得抢在关闭拒绝前面说出错误原因
    ok3, _, err3 = svc.start({})
    assert "关闭" in (err3 or ""), (ok3, err3)
