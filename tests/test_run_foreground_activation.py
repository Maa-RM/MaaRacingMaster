# -*- coding: utf-8 -*-
"""前台(鼠标)模式「按下开始就切前台」按轮生效；后台(手柄)模式不碰窗口。

真机反馈（2026-11-09）：打开程序后第一次启动任务会自动把游戏切到前台，
第二次及以后不会。根因是切前台挂在幂等的 connect() 里——第二次起短路返回，
整段窗口准备被跳过。修复把「用户按下开始」这个一次性动作放到运行边界
begin_run()，connect() 只补「本轮才首次建连」的路径。

锁四条：
1. 窗口已连着时，连续两轮 begin_run() 各切一次（失败例的回归锁）；
2. 后台(手柄)模式任何路径都不切窗口（防新 bug）；
3. 轮内后续项只走 connect() 短路，用户中途切走不被拉回（防呆）；
4. 原本正常的首次建连仍切一次（回归锁的正常场景）。
"""
from __future__ import annotations

import tempfile
import types
from pathlib import Path
from unittest import mock

from maaracing_master.core import controller as ctl


class _Log:
    """记录桩：静音/连接等散行只用到 log()。"""

    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="INFO", **kwargs):
        self.lines.append((level, msg))


def _ctl(monkeypatch):
    """真实控制器 + 窗口平台原语记录桩，其余生命周期照常跑。"""
    with mock.patch.object(
            ctl, "debug_dir",
            lambda: Path(tempfile.gettempdir()) / "maar-test-debug"):
        c = ctl.MaaRacingMasterController()
    rec = _Log()
    calls: list[int] = []
    monkeypatch.setattr(ctl, "logger", rec)
    monkeypatch.setattr(ctl, "is_window_valid", lambda hwnd: bool(hwnd))
    monkeypatch.setattr(ctl, "activate_window",
                        lambda hwnd: calls.append(hwnd) or True)
    return c, calls, rec


def test_real_mode_activates_on_every_run_start(monkeypatch):
    """窗口已连着时每轮按下开始都切一次前台——第二次起不再漏。"""
    c, calls, _ = _ctl(monkeypatch)
    c.set_click_mode("real")
    c.adopt_window(123)
    c.controller = object()  # 已连接：connect() 会幂等短路

    c.begin_run()
    c.begin_run()

    assert calls == [123, 123]


def test_gamepad_mode_never_pulls_window_to_front(monkeypatch):
    """后台(手柄)模式：运行边界和连接短路都不碰窗口。"""
    c, calls, _ = _ctl(monkeypatch)
    c.set_click_mode("gamepad")
    c.adopt_window(123)
    c.controller = object()

    c.begin_run()
    assert c.connect() is True

    assert calls == []


def test_no_extra_activation_during_a_run(monkeypatch):
    """整轮只在开始切一次：轮内后续项走短路，用户中途切走不被拉回来。"""
    c, calls, _ = _ctl(monkeypatch)
    c.set_click_mode("real")
    c.adopt_window(123)
    c.controller = object()

    c.begin_run()  # 用户按下开始
    assert c.connect() is True  # 第一项
    assert c.connect() is True  # 第二项（此时用户可能已切走）

    assert calls == [123]


def test_first_connection_still_activates_in_real_mode(monkeypatch):
    """本轮才首次建连（含游戏刚被拉起）：由 connect() 兑现一次，不重复。"""
    c, calls, _ = _ctl(monkeypatch)
    c.set_click_mode("real")

    class _FakeMaaController:
        def post_connection(self):
            return types.SimpleNamespace(wait=lambda: True)

    monkeypatch.setattr(ctl, "Win32Controller", lambda **kw: _FakeMaaController())
    monkeypatch.setattr(c, "locate_game_hwnd", lambda **kw: 456)
    monkeypatch.setattr(ctl, "resize_game_window_720p", lambda *a, **kw: True)
    monkeypatch.setattr(ctl, "is_window_on_screen", lambda hwnd: True)

    c.begin_run()  # 还没有窗口句柄，不该切
    assert calls == []

    assert c.connect() is True
    assert calls == [456]


def test_run_start_skips_invalid_window_handle(monkeypatch):
    """句柄失效时不注入按键，交给 connect() 重新定位窗口。"""
    c, calls, _ = _ctl(monkeypatch)
    c.set_click_mode("real")
    c.adopt_window(123)
    monkeypatch.setattr(ctl, "is_window_valid", lambda hwnd: False)

    c.begin_run()

    assert calls == []


def test_activation_failure_only_warns(monkeypatch):
    """Windows 前台锁定拒绝切换时只告警，不阻断这一轮。"""
    c, _, rec = _ctl(monkeypatch)
    c.set_click_mode("real")
    c.adopt_window(123)
    monkeypatch.setattr(ctl, "activate_window", lambda hwnd: False)

    c.begin_run()

    assert any(level == "WARNING" and "前台" in msg for level, msg in rec.lines)
