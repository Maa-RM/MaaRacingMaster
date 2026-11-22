# -*- coding: utf-8 -*-
"""极速狂飙 v1 驾驶探针、就绪判据与调试存盘接线。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.retired

MODULE_SRC = Path(__file__).resolve().parents[1] / "module.py"


def test_only_two_drive_loop_call_sites_use_probe():
    """只有驾驶主循环那两个已过 A0-2 的调用点改调 probe。

    注意 `_wait_drive_ready` 里仍保留 `run(DRIVE_STAGE_NODE)`——那是**有意**的，
    不在本次替换范围（A0-2 没验过就绪判据用的节点），所以断言限定在 `_drive` 体内。
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    probe_calls = re.findall(r"self\._graph\.probe\(", src)
    assert len(probe_calls) == 2, f"probe 调用点应为 2 个，实际 {len(probe_calls)}"
    assert "self._graph.probe(DRIVE_STAGE_NODE)" in src
    assert "STORE_BANNER_NODE" in src

    start = src.index("def _drive(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert not re.search(
        r"self\._graph\.run\(\s*(DRIVE_STAGE_NODE|STORE_BANNER_NODE)", body), \
        "驾驶主循环里这两个锚点不得再走导航动作路径"


def test_wait_drive_ready_still_uses_run():
    """就绪判据不在替换范围：A0-2 没验过它用的节点。"""
    src = MODULE_SRC.read_text(encoding="utf-8")
    start = src.index("def _wait_drive_ready")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "self._graph.run(" in body
    assert "self._graph.probe(" not in body


def test_ui_nav_segments_declare_save_drive_does_not():
    """UI 导航段显式声明落图；驾驶控制链不声明。

    `DEBUG_SAVE` 是**运行项级**的（整局一个值，控制器开 scope 时读一次），
    做不到「一半落一半不落」。真正的分界靠接口：`run` 开段且可声明 save，
    `probe` 不开段——所以驾驶期间识别再频繁也进不了调试流。
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    for name in ("def _run_step", "def _wait_drive_ready"):
        start = src.index(name)
        end = src.index("\n    def ", start + 10)
        body = src[start:end]
        calls = re.findall(r"self\._graph\.run\([^)]*\)", body)
        assert calls, f"{name} 里应有 run 调用"
        for call in calls:
            assert "save=NAV_SAVE" in call, f"{name} 的 run 未声明落图：{call}"

    start = src.index("def _drive_loop")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "save=" not in body, "驾驶控制链不应声明存盘策略（probe 本就不开段）"


def test_drive_loop_introduces_no_thread_or_async():
    """两个调用点仍在原有单线程驾驶流程内，未引入线程/异步调度。

    只验本次代码形态；同实例串行仍是调用方契约，不由本测试证明。
    """
    src = MODULE_SRC.read_text(encoding="utf-8")
    start = src.index("def _drive(")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "threading.Thread" not in body
    assert "asyncio" not in body
    assert "concurrent.futures" not in body
