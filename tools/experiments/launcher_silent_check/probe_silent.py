#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动器静默启动探测：回答「Launcher.exe 能否不出窗口完成一次版本检查」。

C 类实验（外部程序行为，官方无文档），标尺见同目录 README：
窗口事实用 OS 口径（EnumWindows/IsWindowVisible/IsIconic/GetWindowRect），检查事实用
启动器自写 Log.txt 的状态机行。两者都不依赖本探针的判定逻辑。

用例互不污染：每个用例开始前确认启动器已关闭；改 launcher.ini 的用例测完按字节还原
（基线快照在 _out/baseline_launcher.ini），还原后复核。
日志按运行轮转（每场会话一个 Log.txt），采样以「文件变短」为轮转信号，轮转前的
旧行一律不采，防上一场会话的状态行污染证据。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/launcher_silent_check/probe_silent.py [用例...]
用例缺省 = swhide minnoactive b_hide b_background offscreen 全跑。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from maaracing_master.core import game_process  # noqa: E402  读取面复用：卸载表反查

_LAUNCHER_EXE_NAME = "Launcher.exe"
_LOG_REL = Path("RacingMaster") / "CN" / "Log.txt"
_INI_REL = Path("RacingMaster") / "CN" / "config" / "launcher.ini"
_OUT_DIR = Path(__file__).resolve().parent / "_out"
_INI_BACKUP = _OUT_DIR / "baseline_launcher.ini"

# 日志里值得采的行：状态机推进、比对结果、窗口出现耗时、游戏拉起、异常
_LOG_KEYWORDS = (
    "set launcher state", "Final download config", "window_shown",
    "real_start_game", "start game", "Launcher version", "error", "exception",
)

_SW_HIDE = 0             # STARTUPINFO wShowWindow：隐藏
_SW_SHOWMINNOACTIVE = 7  # 最小化且不激活（任务栏仍会有条目）
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_OFFSCREEN_X = -2000     # 判「在屏幕外」的左界（经典藏窗位是 -32000）


def _dpi_aware() -> None:
    """探针按物理像素工作，避免坐标被 DPI 虚拟化。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001 —— 旧系统没有该入口，退回默认即可
        pass


def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base)


def _log_path() -> Path:
    return _local_appdata() / _LOG_REL


def _ini_path() -> Path:
    return _local_appdata() / _INI_REL


def _exe_of_pid(kernel32, pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(h)


def _enum_windows_of(exe_name: str) -> list[dict]:
    """枚举某进程名的全部顶层窗口（含不可见），OS 口径事实。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    out: list[dict] = []

    def _cb(hwnd, _lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of_pid(kernel32, int(pid.value)).lower() != exe_name.lower():
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1) if length else None
        if buf:
            user32.GetWindowTextW(hwnd, buf, length + 1)
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append({
            "hwnd": int(hwnd),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "minimized": bool(user32.IsIconic(hwnd)),
            "title": buf.value if buf else "",
            "rect": (r.left, r.top, r.right, r.bottom),
        })
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _pids_of(exe_name: str) -> list[int]:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == -1:
        return []
    pids: list[int] = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe_name.lower():
                pids.append(int(e.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return pids
    finally:
        k32.CloseHandle(snap)


def _running() -> bool:
    return bool(_pids_of(_LAUNCHER_EXE_NAME))


def close_launcher(log) -> None:
    """先礼后兵关闭启动器（taskkill 无 /F = 投递 WM_CLOSE），确认进程消失。"""
    if not _running():
        return
    for pid in _pids_of(_LAUNCHER_EXE_NAME):
        subprocess.run(["taskkill", "/pid", str(pid)], capture_output=True)
    for _ in range(16):
        if not _running():
            log("  启动器已正常退出")
            return
        time.sleep(0.5)
    subprocess.run(["taskkill", "/F", "/IM", _LAUNCHER_EXE_NAME], capture_output=True)
    for _ in range(8):
        if not _running():
            log("  启动器已强杀退出")
            return
        time.sleep(0.5)
    log("  !! 启动器未能关闭")


def _set_ini_flag(key: str, value: str, log) -> None:
    """改 launcher.ini 的一个布尔开关（键已存在则原地改，不存在则补到文件尾）。"""
    text = _ini_path().read_text(encoding="utf-8", errors="replace")
    lines, hit = [], False
    for line in text.splitlines():
        if line.strip().lower().startswith(f"{key.lower()} "):
            lines.append(f"{key} = {value}")
            hit = True
        else:
            lines.append(line)
    if not hit:
        lines.append(f"{key} = {value}")
    _ini_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"  已写 launcher.ini: {key} = {value}")


def _restore_ini(log) -> None:
    data = _INI_BACKUP.read_bytes()
    _ini_path().write_bytes(data)
    ok = _ini_path().read_bytes() == data
    log(f"  launcher.ini 已还原（字节级复核 {'通过' if ok else '失败!'})")


class Watcher:
    """按秒采样：窗口事实 + 日志新行（只采本轮轮转后的新会话行）。"""

    def __init__(self, log, pre_lines: int):
        self.log = log
        self.pre_lines = pre_lines       # 拉起前旧日志行数（轮转判据）
        self.rotated = False
        self.ever_visible: list[dict] = []   # 只收 IsWindowVisible=TRUE 的采样
        self.log_lines: list[str] = []
        self._log_offset = 0

    def sample(self, t: float) -> None:
        for w in _enum_windows_of(_LAUNCHER_EXE_NAME):
            if w["visible"]:
                self.ever_visible.append({"t": t, **w})
        self._poll_log(t)

    def _poll_log(self, t: float) -> None:
        try:
            lines = _log_path().read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if not self.rotated:
            if len(lines) >= self.pre_lines:
                return  # 还是旧文件（或旧文件被追加），不采
            self.rotated = True
            self._log_offset = 0
            self.log_lines.append(f"[{t:4.1f}s] (检测到日志轮转，开始采本轮会话)")
        for line in lines[self._log_offset:]:
            if any(k in line for k in _LOG_KEYWORDS):
                self.log_lines.append(f"[{t:4.1f}s] {line}")
        self._log_offset = len(lines)

    def check_reached(self) -> bool:
        return any("LaunchStateEnum" in l and ("Ready" in l or "Update" in l)
                   for l in self.log_lines)


def _classify(samples: list[dict]) -> str:
    """窗口形态归类：none / minimized / offscreen / onscreen。"""
    if not samples:
        return "none"
    if all(s["minimized"] for s in samples):
        return "minimized"
    if all(s["rect"][0] <= _OFFSCREEN_X for s in samples):
        return "offscreen"
    return "onscreen"


_VERDICTS = {
    "none": "✅ 静默成立——检查推进完成，全程无可见窗口",
    "minimized": "⚠ 窗口最小化驻留（任务栏有条目，非完全静默）",
    "offscreen": "⚠ 窗口在屏幕外驻留（OS 可见，肉眼不可见，可自动化）",
    "onscreen": "❌ 静默不成立——出现过屏幕上的可见窗口",
}


def _verdict(mode: str, w: Watcher, moved_at: float | None) -> str:
    if not w.check_reached():
        return f"{mode}: 检查未推进（15s 内没到 Ready/Update，先查卡点）"
    if mode == "offscreen" and moved_at is not None:
        after = [s for s in w.ever_visible if s["t"] >= moved_at]
        shape = _classify(after or w.ever_visible)
        if shape == "offscreen":
            return f"{mode}: ✅ 移屏生效——移屏后窗口全部在屏幕外（可自动化，肉眼不可见）"
        return f"{mode}: ❌ 移屏后形态={shape}，移屏无效或窗口自行回到屏幕"
    return f"{mode}: {_VERDICTS[_classify(w.ever_visible)]}"


def _pre_log_lines() -> int:
    try:
        return len(_log_path().read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def run_case(mode: str, exe: Path, log) -> str:
    """跑一个用例：布置 → 拉起 → 采样 15s → 关闭 → 还原。返回判定行。"""
    log(f"== 用例 {mode} ==")
    if _running():
        log("  启动器已在运行，先关闭")
        close_launcher(log)
        time.sleep(1.0)

    ini_touched = mode in ("b_hide", "b_background", "b_silent")
    if ini_touched:
        _set_ini_flag(mode, "True", log)

    creationflags = 0x00000008 | 0x00000200  # 与生产 spawn 一致：DETACHED | NEW_GROUP
    si = None
    if mode == "swhide":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = _SW_HIDE
    elif mode == "minnoactive":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = _SW_SHOWMINNOACTIVE

    moved_at: float | None = None
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                creationflags=creationflags, startupinfo=si,
                                close_fds=True)
    except OSError as exc:
        log(f"  拉起失败: {exc}")
        if ini_touched:
            _restore_ini(log)
        return f"{mode}: 拉起失败"

    w = Watcher(log, _pre_log_lines())
    t0 = time.monotonic()
    while time.monotonic() - t0 < 15.0:
        t = time.monotonic() - t0
        w.sample(t)
        if mode == "offscreen" and moved_at is None and w.ever_visible:
            hwnd = w.ever_visible[-1]["hwnd"]
            ctypes.WinDLL("user32").SetWindowPos(
                wt.HWND(hwnd), 0, -32000, -32000, 0, 0,
                _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
            moved_at = t
            log(f"  [{t:4.1f}s] 已把 hwnd={hwnd} 移出屏幕")
        time.sleep(1.0)

    for line in w.log_lines:
        log(f"  {line}")
    if not w.rotated:
        log("  !! 15s 内未见到日志轮转——本轮会话没有任何日志行被采到")
    log(f"  判定素材：{_classify(w.ever_visible)}（共 {len(w.ever_visible)} 次可见采样）")
    verdict = _verdict(mode, w, moved_at)
    log(f"  判定：{verdict}")

    close_launcher(log)
    if ini_touched:
        _restore_ini(log)
    time.sleep(1.0)
    return verdict


def main(argv: list[str]) -> int:
    _dpi_aware()
    exe = game_process.detect_launcher_exe()
    if exe is None or not exe.is_file():
        print("!! 卸载表反查不到启动器，实验无法进行")
        return 2
    print(f"启动器: {exe}")

    modes = argv[1:] or ["swhide", "minnoactive", "b_hide", "b_background", "offscreen"]
    all_lines: list[str] = [f"# probe_silent {datetime.now():%Y-%m-%d %H:%M:%S}", f"启动器: {exe}"]

    def log(msg: str, _lv: str = "") -> None:
        print(msg)
        all_lines.append(msg)

    verdicts = [run_case(m, exe, log) for m in modes]

    log("")
    log("===== 汇总 =====")
    for v in verdicts:
        log(f"  {v}")

    out = _OUT_DIR / f"probe_silent_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out.write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    print(f"\n时间线已存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
