# -*- coding: utf-8 -*-
"""续跑收口的停止判据：停止在途不得续跑、不计恢复次数。

回归背景（真机会话 20261127_230258）：手动停止被续跑漏斗当成页面异常接回，
在停止请求受理完成后向框架投全新任务（调试帧证据：停止后仍有约 10 秒识别与
点击），收尾两道 10s 有界等待全部超时，句柄被隔离、提示重启程序。判据位置在
恢复决策点（``_resume_from`` 入口）；core 的 ``run()`` / ``probe()`` 投递前门
是兜底，由 test_nav_graph_runner.py 另行看护。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

try:
    from maaracing_master.plugins.infinite_drive import module as idmod

    _RUNTIME_OK, _RUNTIME_ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _RUNTIME_OK, _RUNTIME_ERR = False, str(exc)

pytestmark = pytest.mark.skipif(
    not _RUNTIME_OK, reason=f"需要完整运行时依赖（maa/…）：{_RUNTIME_ERR}"
)


def _bare_module() -> idmod.InfiniteDriveModule:
    """只装配 ``_resume_from`` 用到的状态（绕过 __init__ 的重依赖）。"""
    m = idmod.InfiniteDriveModule.__new__(idmod.InfiniteDriveModule)
    m._running = True
    m.ctx = MagicMock()
    m.ctx.lifecycle.running = True
    m._graph = MagicMock()
    m._log_grp = MagicMock()  # 阶段日志归桩组，不落全局 logger
    m._stage_index = 0        # 入口步：偶发弹窗清扫不探大厅（见 _poll_hall）
    m._resume_attempts = 1
    m._hall_reset = False
    m._next_hall_at = 0.0
    return m


def test_resume_refuses_when_module_stopped():
    """模块已停：不 probe、不续跑、不计恢复次数，交回调用方中止。"""
    m = _bare_module()
    m._running = False
    assert m._resume_from("关卡挑战") is None
    m._graph.probe.assert_not_called()
    m._graph.run.assert_not_called()
    assert m._resume_attempts == 1


def test_resume_refuses_when_stop_event_set_before_notice():
    """停止事件已置位、模块 stop() 通知未到的窗口：同样不得续跑。

    controller.stop 先置停止事件再调模块 stop()，事件先于通知到达。
    """
    m = _bare_module()
    m.ctx.lifecycle.running = False
    assert m._resume_from("关卡挑战") is None
    m._graph.probe.assert_not_called()
    m._graph.run.assert_not_called()
    assert m._resume_attempts == 1


def test_resume_still_routes_when_running():
    """原本正常的场景也要在锁里：未停止时按页面路由续跑、计次照旧。"""
    m = _bare_module()
    m._graph.probe.return_value = idmod.ProbeOutcome.HIT
    idx = m._resume_from("关卡挑战")
    assert idx == idmod._RESUME_ROUTES[0][1]
    assert m._resume_attempts == 2
