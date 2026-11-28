# -*- coding: utf-8 -*-
"""后台消息点击模式（bg_winpos / bg_cursor）与防呆护栏单元测试。"""
from __future__ import annotations

import ctypes
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from maaracing_master.core import clicker as clk
from maaracing_master.core import controller as ctl
from maaracing_master.core import window_utils as wu


class _FakeUD:
    """Win32 user32 桩：记录 SetWindowPos / SetCursorPos / SendMessageW 调用序列。"""

    def __init__(self, *, iconic=False, zoomed=False,
                 cursor=(500, 500), win_rect=(100, 100, 1380, 820),
                 client_origin=(100, 100), hmon_win=1, hmon_cur=1, hmon_dest=1):
        self.iconic = iconic
        self.zoomed = zoomed
        self.cursor = cursor
        self.win_rect = win_rect
        self.client_origin = client_origin
        self.hmon_win = hmon_win
        self.hmon_cur = hmon_cur
        self.hmon_dest = hmon_dest
        self.set_win_pos_calls: list[tuple[int, int, int, int, int, int, int]] = []
        self.set_cursor_calls: list[tuple[int, int]] = []
        self.send_msg_calls: list[tuple[int, int, int, int]] = []
        self.raise_on_send = False

    def IsWindow(self, hwnd):
        return bool(hwnd)

    def IsIconic(self, hwnd):
        return self.iconic

    def IsZoomed(self, hwnd):
        return self.zoomed

    def GetCursorPos(self, pt_ptr):
        pt = ctypes.cast(pt_ptr, ctypes.POINTER(wu.wintypes.POINT)).contents
        pt.x, pt.y = self.cursor
        return 1

    def GetWindowRect(self, hwnd, rect_ptr):
        r = ctypes.cast(rect_ptr, ctypes.POINTER(wu.wintypes.RECT)).contents
        r.left, r.top, r.right, r.bottom = self.win_rect
        return 1

    def ClientToScreen(self, hwnd, pt_ptr):
        pt = ctypes.cast(pt_ptr, ctypes.POINTER(wu.wintypes.POINT)).contents
        pt.x += self.client_origin[0]
        pt.y += self.client_origin[1]
        return 1

    def MonitorFromWindow(self, hwnd, flags):
        return self.hmon_win

    def MonitorFromPoint(self, pt, flags):
        if (pt.x, pt.y) == self.cursor:
            return self.hmon_cur
        return self.hmon_dest

    def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
        self.set_win_pos_calls.append((hwnd, int(insert_after), x, y, cx, cy, flags))
        return 1

    def SetCursorPos(self, x, y):
        self.set_cursor_calls.append((x, y))
        return 1

    def SendMessageW(self, hwnd, msg, wparam, lparam):
        self.send_msg_calls.append((hwnd, msg, wparam, lparam))
        if self.raise_on_send:
            raise RuntimeError("boom")
        return 0


def test_click_modes_and_need_foreground():
    assert set(clk.CLICK_MODES) == {"real", "bg_winpos", "bg_cursor"}
    assert clk.Clicker(123, "real").need_foreground is True
    assert clk.Clicker(123, "bg_winpos").need_foreground is False
    assert clk.Clicker(123, "bg_cursor").need_foreground is False
    with pytest.raises(ValueError):
        clk.Clicker(123, "gamepad")


def test_cursor_detector_decoupled_from_gamepad():
    """纯视觉光标识别器不依赖手柄驱动即可识别圆盘光标。"""
    import numpy as np
    from maaracing_master.core import cursor_detector as cd

    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    cy, cx = 100, 100
    yy, xx = np.ogrid[:200, :200]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    frame[dist <= 13] = (133, 133, 133)
    frame[dist <= 9] = (255, 255, 255)

    targets, sel = cd.detect_cursor(frame)
    assert targets, "未识别到合成圆盘光标"
    assert sel is not None
    assert abs(sel.pos[0] - cx) <= 2 and abs(sel.pos[1] - cy) <= 2


def test_legacy_gamepad_profile_migrates_to_bg_winpos(tmp_path, monkeypatch):
    """历史 profile 中的 click_mode='gamepad' 恢复时自动迁移为 bg_winpos。"""
    from maaracing_master.core import sidecar as sc

    with mock.patch.object(ctl, "debug_dir", lambda: tmp_path):
        c = ctl.MaaRacingMasterController()
    srv = sc.SidecarService.__new__(sc.SidecarService)
    srv._controller = c
    monkeypatch.setattr(sc, "_load_profile", lambda: {"debug": {"click_mode": "gamepad"}})
    srv._restore_profile()
    assert c.click_mode == "bg_winpos"


def test_bg_winpos_moves_and_immediately_restores(monkeypatch):
    ud = _FakeUD(cursor=(800, 600), win_rect=(100, 100, 1380, 820), client_origin=(100, 100))
    monkeypatch.setattr(wu, "_UD", ud)
    monkeypatch.setattr(wu.time, "sleep", lambda _: None)

    ok = wu.bg_click_window_pos(123, 200, 100, down_up_gap_ms=0)
    assert ok is True
    # 目标点屏幕原为 (300, 200)，对齐到鼠标 (800, 600) 需偏移 (+500, +400) → (600, 500)
    assert len(ud.set_win_pos_calls) == 2
    assert ud.set_win_pos_calls[0][2:4] == (600, 500)
    assert ud.set_win_pos_calls[1][2:4] == (100, 100)
    assert [m[1] for m in ud.send_msg_calls] == [
        wu._WM_MOUSEMOVE, wu._WM_LBUTTONDOWN, wu._WM_LBUTTONUP
    ]


def test_bg_winpos_restores_even_on_exception(monkeypatch):
    ud = _FakeUD()
    ud.raise_on_send = True
    monkeypatch.setattr(wu, "_UD", ud)
    monkeypatch.setattr(wu.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError):
        wu.bg_click_window_pos(123, 200, 100, down_up_gap_ms=0)
    assert len(ud.set_win_pos_calls) == 2
    assert ud.set_win_pos_calls[1][2:4] == (100, 100)


def test_bg_winpos_blocks_cross_monitor_and_zoomed(monkeypatch):
    ud_cross = _FakeUD(hmon_win=1, hmon_cur=2)
    monkeypatch.setattr(wu, "_UD", ud_cross)
    monkeypatch.setattr(wu.time, "sleep", lambda _: None)
    assert wu.bg_click_window_pos(123, 200, 100, down_up_gap_ms=0) is False
    assert ud_cross.set_win_pos_calls == []

    ud_zoomed = _FakeUD(zoomed=True)
    monkeypatch.setattr(wu, "_UD", ud_zoomed)
    assert wu.bg_click_window_pos(123, 200, 100, down_up_gap_ms=0) is False
    assert ud_zoomed.set_win_pos_calls == []


def test_bg_cursor_moves_cursor_and_restores(monkeypatch):
    ud = _FakeUD(cursor=(900, 700), client_origin=(100, 100))
    monkeypatch.setattr(wu, "_UD", ud)
    monkeypatch.setattr(wu.time, "sleep", lambda _: None)

    ok = wu.bg_click_cursor_pos(123, 200, 100, down_up_gap_ms=0)
    assert ok is True
    assert ud.set_cursor_calls == [(300, 200), (900, 700)]
    assert ud.set_win_pos_calls == []


def test_clicker_dispatches_bg_modes(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(clk, "window_client_size", lambda hwnd: (1280, 720))
    monkeypatch.setattr(clk, "norm_to_screen", lambda hwnd, cx, cy, cw, ch: (640, 360))
    monkeypatch.setattr(clk, "bg_click_window_pos",
                        lambda hwnd, x, y, gap: calls.append(("winpos", x, y)) or True)
    monkeypatch.setattr(clk, "bg_click_cursor_pos",
                        lambda hwnd, x, y, gap: calls.append(("cursor", x, y)) or True)

    c1 = clk.Clicker(123, "bg_winpos")
    assert c1.submit_click(0.5, 0.5) is True
    assert c1.consume_result()["ok"] is True

    c2 = clk.Clicker(123, "bg_cursor")
    assert c2.submit_click(0.25, 0.5) is True
    assert c2.consume_result()["ok"] is True

    assert calls == [("winpos", 640, 360), ("cursor", 320, 360)]


def test_controller_begin_run_sends_to_bottom_in_bg_winpos(monkeypatch):
    with mock.patch.object(
            ctl, "debug_dir",
            lambda: Path(tempfile.gettempdir()) / "maar-test-debug"):
        c = ctl.MaaRacingMasterController()
    c.set_click_mode("bg_winpos")
    c.adopt_window(123)
    c.controller = object()

    activated: list[int] = []
    bottomed: list[int] = []
    monkeypatch.setattr(ctl, "is_window_valid", lambda hwnd: bool(hwnd))
    monkeypatch.setattr(ctl, "activate_window", lambda hwnd: activated.append(hwnd) or True)
    monkeypatch.setattr(wu, "send_window_to_bottom", lambda hwnd: bottomed.append(hwnd) or True)

    c.begin_run()
    assert activated == []
    assert bottomed == [123]
