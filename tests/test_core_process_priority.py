# -*- coding: utf-8 -*-
"""进程级资源优先级（`core/process_priority.py`）。

看护的是提级的**编排语义**：先 CPU 后 IO、单通道失败不抛异常也不影响另一通道、
非 Windows 全无操作、生效清单转日志串。API 细节（nice 值、IO 优先级码）用伪对象
钉住，不碰真进程状态；`sys.platform` 也按用例固定，Windows 与 Linux CI 同样确定性。
"""
from __future__ import annotations

import ctypes
import sys
import types

from maaracing_master.core import process_priority as pp

_HIGH_PRIORITY_CLASS, _NORMAL_PRIORITY_CLASS = 128, 32
_ProcessIoPriority = 0x21
_STATUS_PRIVILEGE_NOT_HELD = 0xC0000061


class _FakeFunc:
    """可设 restype/argtypes、可调用并记录入参的伪 API。"""

    def __init__(self, result=0):
        self.restype = None
        self.argtypes = None
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _FakeDll:
    def __init__(self):
        self._funcs: dict = {}

    def __getattr__(self, name):
        if name not in self._funcs:
            self._funcs[name] = _FakeFunc()
        return self._funcs[name]


class _FakeProc:
    def __init__(self, nice_error=None):
        self.nice_values: list = []
        self._err = nice_error

    def nice(self, value):
        if self._err is not None:
            raise self._err
        self.nice_values.append(value)


def _install_fakes(monkeypatch, proc):
    """注入伪 psutil 与伪 WinDLL（按名分发），返回 {dll 名: 伪对象}。"""
    fake_psutil = types.SimpleNamespace(
        HIGH_PRIORITY_CLASS=_HIGH_PRIORITY_CLASS,
        NORMAL_PRIORITY_CLASS=_NORMAL_PRIORITY_CLASS,
        Process=lambda: proc)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    dlls: dict = {"kernel32": _FakeDll(), "ntdll": _FakeDll()}

    def _win_dll(name, use_last_error=False):
        dlls.setdefault(name, _FakeDll())
        return dlls[name]

    # raising=False：Linux 的 ctypes 没有 WinDLL 属性，补挂才能让伪对象在两边 CI
    # 都生效（文件口径：Windows 与 Linux CI 同样确定性，生产侧按 sys.platform 分流）。
    monkeypatch.setattr(ctypes, "WinDLL", _win_dll, raising=False)
    return dlls


def _io_value(call):
    """NtSetInformationProcess 入参里 byref 缓冲携带的 IO 优先级值。"""
    _, cls, buf, size = call
    assert cls == _ProcessIoPriority
    assert size == ctypes.sizeof(ctypes.c_ulong)
    return ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong)).contents.value


def test_apply_boost_sets_cpu_high_then_io_high(monkeypatch):
    proc = _FakeProc()
    dlls = _install_fakes(monkeypatch, proc)
    monkeypatch.setattr(sys, "platform", "win32")

    applied = pp.apply_boost()

    assert applied == {"cpu": True, "io": True}
    assert proc.nice_values == [_HIGH_PRIORITY_CLASS]
    (nt_call,) = dlls["ntdll"].NtSetInformationProcess.calls
    assert _io_value(nt_call) == 3


def test_revert_restores_normal_and_io_normal(monkeypatch):
    proc = _FakeProc()
    dlls = _install_fakes(monkeypatch, proc)
    monkeypatch.setattr(sys, "platform", "win32")

    applied = pp.revert()

    assert applied == {"cpu": True, "io": True}
    assert proc.nice_values == [_NORMAL_PRIORITY_CLASS]
    (nt_call,) = dlls["ntdll"].NtSetInformationProcess.calls
    assert _io_value(nt_call) == 2


def test_cpu_failure_does_not_block_io_channel(monkeypatch):
    proc = _FakeProc(nice_error=PermissionError("access denied"))
    dlls = _install_fakes(monkeypatch, proc)
    monkeypatch.setattr(sys, "platform", "win32")

    applied = pp.apply_boost()

    assert applied == {"cpu": False, "io": True}
    assert len(dlls["ntdll"].NtSetInformationProcess.calls) == 1


def test_io_privilege_denied_returns_false(monkeypatch):
    proc = _FakeProc()
    dlls = _install_fakes(monkeypatch, proc)
    monkeypatch.setattr(sys, "platform", "win32")
    dlls["ntdll"].NtSetInformationProcess.result = _STATUS_PRIVILEGE_NOT_HELD

    applied = pp.apply_boost()

    assert applied == {"cpu": True, "io": False}


def test_non_windows_is_a_noop(monkeypatch):
    touched = []

    def _boom(*args, **kwargs):
        touched.append(True)
        raise AssertionError("非 Windows 不得触达 WinDLL")

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(
        Process=lambda: touched.append("psutil") or _FakeProc(),
        HIGH_PRIORITY_CLASS=_HIGH_PRIORITY_CLASS,
        NORMAL_PRIORITY_CLASS=_NORMAL_PRIORITY_CLASS))
    # raising=False：Linux 的 ctypes 没有 WinDLL 属性（同 _install_fakes 的口径）。
    monkeypatch.setattr(ctypes, "WinDLL", _boom, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    assert pp.apply_boost() == {"cpu": False, "io": False}
    assert pp.revert() == {"cpu": False, "io": False}
    assert touched == []


def test_describe_lists_channels_in_order():
    assert pp.describe({"cpu": True, "io": True}) == "CPU、磁盘IO"
    assert pp.describe({"cpu": True, "io": False}) == "CPU"
    assert pp.describe({"cpu": False, "io": False}) == "无（全部未生效）"
    assert pp.describe({}) == "无（全部未生效）"
