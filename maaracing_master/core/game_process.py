#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏进程原语：定位安装、直启主程序、等待游戏窗口、关闭进程；
以及官方启动器的版本检查搬运工（拉起、读日志判定、代点更新、优雅关闭）。

**为什么在 core**：进程启停不属于任何活动玩法，所有模块共用同一份「游戏在哪、
怎么起、怎么关」。插件不该各自实现一遍，也不该反过来操纵宿主窗口句柄。

**平台事实**（2026-10-29 实测结论）：
- 官方启动器先做版本比对与逐文件 MD5 校验，再拼参数拉起游戏主程序；
- 游戏主程序可直接拉起，不经启动器：13s 出窗口，39s 登录完成进大厅；
- 直启参数与启动器一致（含 -nosound / -nohmd 等），照抄即等价；
- 启动器窗口标题与游戏窗口同名，按标题找窗口会认错——这里一律按进程名匹配。
"""

from __future__ import annotations

import ast
import ctypes
import ctypes.wintypes as wt
import hashlib
import os
import subprocess
import time
from pathlib import Path

from maaracing_master.core.logger import logger
from maaracing_master.core.window_utils import terminate_process_by_hwnd

# 游戏主程序相对安装目录的位置（安装根 = 启动器配置里的 game_dir）
_GAME_EXE_REL = Path("g112") / "Binaries" / "Win64" / "g112-Win64-Shipping.exe"

# 直启参数：与官方启动器实测命令行逐字一致。少一个都可能影响加载（-force_load_content
# 关系到补丁 pak 的加载），所以默认值不由使用者手打，界面给的是这一份。
DEFAULT_ARGS = (
    "-nosound",
    "-nohmd",
    "--MuMuLauncher",
    "-noexceptionhandler",
    "-Hdd",
    "-force_load_content",
)

# 启动器配置（记录 game_dir 的那份）：%LOCALAPPDATA%\RacingMaster\CN\config\config.ini
_LAUNCHER_CONFIG_REL = Path("RacingMaster") / "CN" / "config" / "config.ini"
# 启动器安装包的卸载表名（用于反查 Launcher.exe，回退启动时用）
_LAUNCHER_DISPLAY_HINTS = ("巅峰极速",)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_TH32CS_SNAPPROCESS = 0x00000002

if hasattr(ctypes, "WinDLL"):
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    # Linux CI 只做收集/单测；Windows 专有入口在真调用时才报错。
    class _UnavailableWinDll:
        def __getattr__(self, name):
            def _unavailable(*args, **kwargs):
                raise OSError("Windows API is unavailable on this platform")

            return _unavailable

    _user32 = _kernel32 = _UnavailableWinDll()


# --------------------------------------------------------------------------
# 安装定位
# --------------------------------------------------------------------------

def _read_ini_section(path: Path, section: str) -> dict[str, str]:
    """读 ini 的一个小节（只做本模块需要的简单解析，不引第三方库）。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            continue
        if current != section.lower() or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip().lower()] = v.strip()
    return out


def launcher_config_path() -> Path:
    """官方启动器的配置文件路径（不保证存在）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / _LAUNCHER_CONFIG_REL


def detect_game_exe() -> Path | None:
    """从启动器配置里读出游戏安装目录并拼出主程序路径；探测失败返回 None。

    只读别人的配置文件、不写、不猜多个候选路径——猜出来的路径填进界面比留空更糟。
    """
    info = _read_ini_section(launcher_config_path(), "Info")
    game_dir = info.get("game_dir", "").strip()
    if not game_dir:
        return None
    exe = Path(game_dir.replace("\\", "/")) / _GAME_EXE_REL
    return exe if exe.is_file() else None


def detect_launcher_exe() -> Path | None:
    """反查官方启动器路径（回退启动时用）：扫卸载表里的显示名。

    找不到返回 None——回退是"有则更好"的能力，不因探测失败而阻断启动步骤。
    """
    try:
        import winreg
    except Exception:  # noqa: BLE001 —— 非 Windows 平台不应走到这里
        return None
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as k:
                            display = str(winreg.QueryValueEx(k, "DisplayName")[0])
                            if not any(h in display for h in _LAUNCHER_DISPLAY_HINTS):
                                continue
                            icon = str(winreg.QueryValueEx(k, "DisplayIcon")[0])
                    except OSError:
                        continue
                    exe = Path(icon.split(",")[0].strip().strip('"'))
                    if exe.is_file():
                        return exe
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# 窗口与进程
# --------------------------------------------------------------------------

def _exe_of_pid(pid: int) -> str:
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        _kernel32.CloseHandle(h)


def find_window_by_exe(exe_name: str) -> int:
    """按**进程可执行文件名**找可见窗口，返回 hwnd（找不到为 0）。

    不按标题：启动器窗口与游戏窗口同名，标题匹配会认错窗口。同名进程可能有多个窗口
    （启动画面/主窗口），取面积最大的那个——主窗口才是要连的那个。
    """
    target = exe_name.lower()
    best: tuple[int, int] = (0, 0)  # (面积, hwnd)

    def _cb(hwnd, _lparam):
        nonlocal best
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of_pid(int(pid.value)).lower() != target:
            return True
        r = wt.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        if area > best[0]:
            best = (area, int(hwnd))
        return True

    _user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(_cb), 0)
    return best[1]


def find_window_by_process() -> int:
    """按游戏主程序进程名找窗口，不需要知道安装路径；找不到返回 0。

    给"游戏不是本程序起的"这条路径用：用户手动开的游戏，安装路径没配、启动器配置
    也读不到时，进程名仍然认得出。匹配的是默认发行渠道的主程序包名，非标准安装或
    别的渠道会在这里落空——那是调用方回退到按标题找的场景，不代表游戏没开。同理，
    画面由模拟器进程承载时窗口不属于游戏主程序进程，也走那条回退。
    """
    return find_window_by_exe(_GAME_EXE_REL.name)


def game_running(exe_name: str) -> bool:
    """游戏主程序是否已在运行（按进程名，不依赖窗口是否已出现）。"""
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return False
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe_name.lower():
                return True
            ok = _kernel32.Process32NextW(snap, ctypes.byref(e))
        return False
    finally:
        _kernel32.CloseHandle(snap)


def terminate_by_exe(exe_name: str) -> int:
    """按进程名结束全部同名进程，返回结束的数量（窗口找不到时的兜底）。"""
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    pids: list[int] = []
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return 0
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == exe_name.lower():
                pids.append(int(e.th32ProcessID))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _kernel32.CloseHandle(snap)

    n = 0
    for pid in pids:
        h = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not h:
            continue
        try:
            if _kernel32.TerminateProcess(h, 1):
                n += 1
        finally:
            _kernel32.CloseHandle(h)
    return n


# --------------------------------------------------------------------------
# 官方启动器：版本检查搬运工
#
# 事实与判据全部来自实测（2026-11-22，证据以 commit 46727db 为指针）：启动器把
# 版本比对结论写进本地日志
# （%LOCALAPPDATA%\RacingMaster\CN\Log.txt），`Final download config:` 的下一行
# JSON 里 `update_list` 空 = 无需更新（或补丁刚装好）、非空 = 待更新，两种情形
# 同一信号。检查在拉起后 ~5-6.4s 落地，主窗口 ~8.5-9.4s 才出现——先于窗口拿到
# 结论并关闭它，即可全程无窗静默检查。启动器要求管理员提权，本程序未提权时
# 拉起会走 UAC，且跨权限关窗/点击会被 UIPI 拦截（如实降级，不硬闯）。
# --------------------------------------------------------------------------

LAUNCHER_EXE_NAME = "Launcher.exe"
_LAUNCHER_LOG_REL = Path("RacingMaster") / "CN" / "Log.txt"
# 「更新游戏」按钮中心在客户区的比例位（1366×796 截图标定，按钮约 300×50）
_UPDATE_BTN_FRAC = (0.811, 0.899)
_WM_CLOSE = 0x0010
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_MK_LBUTTON = 0x0001
_FINAL_CONFIG_MARK = "Final download config"


def launcher_log_path() -> Path:
    """官方启动器的日志路径（拉起前可能不存在；存在时也可能被续写）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / _LAUNCHER_LOG_REL


def is_elevated() -> bool:
    """本进程是否以管理员运行（决定启动器相关操作走静默还是 UAC/降级）。"""
    if not hasattr(ctypes, "WinDLL"):
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 —— 非 Windows / 调用失败一律按未提权
        return False


def spawn_launcher(exe: Path) -> int:
    """按权限拉起官方启动器，返回 pid（0 = 失败或用户取消 UAC）。

    已提权直接 Popen（无 UAC、进程留在本进程树里随宿主收尾）；未提权走
    ShellExecute runas——弹一次 UAC，**新进程不在本进程树里**：宿主侧超时清理
    带不走它，关闭只能靠 close_launcher()（同样受权限对齐约束）。
    """
    if not hasattr(ctypes, "WinDLL"):
        return 0
    if is_elevated():
        try:
            return spawn(exe, []).pid
        except OSError:
            return 0
    try:
        import ctypes.wintypes as _wt  # noqa: F401 —— 结构体字段要用

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD), ("fMask", wt.ULONG), ("hwnd", wt.HWND),
                ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR),
                ("lpParameters", wt.LPCWSTR), ("lpDirectory", wt.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR),
                ("hkeyClass", wt.HKEY), ("dwHotKey", wt.DWORD),
                ("hIconOrMonitor", wt.HANDLE), ("hProcess", wt.HANDLE),
            ]

        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        sei.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | FLAG_DDEWAIT
        sei.lpVerb = "runas"
        sei.lpFile = str(exe)
        sei.lpDirectory = str(exe.parent)
        sei.nShow = 1  # SW_SHOWNORMAL
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        if not shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
            return 0
        pid = _kernel32.GetProcessId(sei.hProcess)
        _kernel32.CloseHandle(sei.hProcess)
        return int(pid)
    except Exception:  # noqa: BLE001 —— 用户取消 UAC 等一律按拉起失败
        return 0


def _window_handles_of(exe_name: str) -> list[int]:
    """按进程名枚举可见顶层窗口句柄（给投递 WM_CLOSE 用）。"""
    handles: list[int] = []

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of_pid(int(pid.value)).lower() == exe_name.lower():
            handles.append(int(hwnd))
        return True

    _user32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(_cb), 0)
    return handles


def close_launcher() -> str:
    """优雅关闭官方启动器；返回 'closed' / 'left_running' / 'not_running'。

    先向其顶层窗口投 WM_CLOSE（实测无退出确认框，能干净退出）；8s 不退且拿到
    终止权才强杀。跨权限（本程序未提权、启动器提权）时 WM_CLOSE 被 UIPI 拦、
    OpenProcess 拿不到终止权——如实返回 left_running，不硬闯。
    """
    if not game_running(LAUNCHER_EXE_NAME):
        return "not_running"
    if hasattr(ctypes, "WinDLL"):
        for hwnd in _window_handles_of(LAUNCHER_EXE_NAME):
            _user32.PostMessageW(wt.HWND(hwnd), _WM_CLOSE, 0, 0)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if not game_running(LAUNCHER_EXE_NAME):
            return "closed"
        time.sleep(0.5)
    if terminate_by_exe(LAUNCHER_EXE_NAME) and not game_running(LAUNCHER_EXE_NAME):
        return "closed"
    return "left_running"


def click_launcher_update_button(hwnd: int) -> bool:
    """向「更新游戏」按钮投递一次左键点击（客户区比例位标定）。

    PostMessage 直达窗口消息队列：不挪窗口、不动物理鼠标。跨权限投递会被
    UIPI 拦下——只在权限对齐时可靠，拦掉了也只会表现为「点了没反应」，
    由调用方转人工兜底。
    """
    if not hasattr(ctypes, "WinDLL"):
        return False
    try:
        r = wt.RECT()
        if not _user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r)):
            return False
        cw, ch = r.right - r.left, r.bottom - r.top
        if cw <= 0 or ch <= 0:
            return False
        x, y = int(cw * _UPDATE_BTN_FRAC[0]), int(ch * _UPDATE_BTN_FRAC[1])
        lp = (y << 16) | (x & 0xFFFF)
        _user32.PostMessageW(wt.HWND(hwnd), _WM_LBUTTONDOWN, _MK_LBUTTON, lp)
        time.sleep(0.08)
        _user32.PostMessageW(wt.HWND(hwnd), _WM_LBUTTONUP, 0, lp)
        return True
    except OSError:
        return False


class _LauncherLogTail:
    """启动器会话日志的增量读取。

    只采「本次拉起之后」写下的行：初始化时记住已读过哪些行（按内容摘要），
    此后已读部分被改写或文件被换成另一份，就判定为新一轮会话、从头读；否则
    只读尾部新增。初始化前的旧行一律不采，防上一场会话的状态行污染判定。

    判据是「已读的那些行还在原位吗」，不是行长：行长会漏掉两类真实情形——
    日志初始不存在（拉起后才新建，行长从 0 起算），以及同一进程续写同一份
    日志（自动更新与 Ready 落在同一个文件里，行长只增不减）。
    """

    def __init__(self):
        lines = self._read()
        self._offset = len(lines)
        self._seen = self._digest_of(lines)

    @staticmethod
    def _read() -> list[str]:
        """读回完整行；文件不存在时返回空表。"""
        try:
            text = launcher_log_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines = text.splitlines()
        # 末尾没有换行说明最后一行还在写：留到下次再读，免得半行被拆成两条
        if lines and not text.endswith(("\n", "\r")):
            lines = lines[:-1]
        return lines

    @staticmethod
    def _digest_of(lines: list[str]) -> bytes:
        h = hashlib.sha1()
        for line in lines:
            h.update(line.encode("utf-8", "replace"))
            h.update(b"\n")
        return h.digest()

    def poll(self) -> list[str]:
        lines = self._read()
        if self._offset > len(lines) or self._digest_of(lines[:self._offset]) != self._seen:
            self._offset = 0  # 已读部分变了（新会话重开或截断重写）：从头读
        new = lines[self._offset:]
        self._offset = len(lines)
        self._seen = self._digest_of(lines)
        return new


def _parse_final_download_config(lines: list[str]) -> dict | None:
    """从日志行里解析 `Final download config:` 的下一行 dict（无则 None）。"""
    for i, line in enumerate(lines):
        if _FINAL_CONFIG_MARK in line and i + 1 < len(lines):
            raw = lines[i + 1].split("]: ", 1)[-1].strip()
            try:
                return ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return None
    return None


def wait_launcher_check(stop_event=None, timeout_s: float = 20.0) -> dict | None:
    """跟随启动器日志等版本比对结论落地。

    返回 {'update_needed': bool, 'size': int, 'files': int}；超时或被停止返回
    None（调用方用 stop_event 区分两者）。只采本轮会话的日志行。
    """
    tail = _LauncherLogTail()
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    captured: list[str] = []
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return None
        captured.extend(tail.poll())
        config = _parse_final_download_config(captured)
        if config is not None:
            files = list(config.get("update_list") or [])
            return {"update_needed": bool(files),
                    "size": int(config.get("size") or 0), "files": len(files)}
        time.sleep(0.25)
    return None


def wait_launcher_ready(stop_event=None, timeout_s: float = 1800.0,
                        log=None) -> str:
    """跟随启动器日志等补丁流程完成。返回 'ready' / 'game_started' / 'timeout'。

    期间把状态迁移与逐文件完成写进 log——那是补丁进度唯一的可观察面。
    'game_started' = 启动器自行拉起了游戏（Ready 后它并不自动开游戏，出现此
    结局说明有人在启动器里点了开始）。
    """
    tail = _LauncherLogTail()
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    state = ""
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return "timeout"
        for line in tail.poll():
            if "set launcher state:" in line:
                state = line.split("LaunchStateEnum.")[-1].strip()
                if log:
                    log(f"[启动器] 状态 → {state}")
            elif "download file:" in line and log:
                log(f"[启动器] {line.split(']: ', 1)[-1].strip()}")
            elif "real_start_game" in line or "start game:" in line:
                if log:
                    log("[启动器] 启动器自行拉起了游戏")
                return "game_started"
        if state == "Ready":
            return "ready"
        time.sleep(0.25)
    return "timeout"


# --------------------------------------------------------------------------
# 启动 / 等待 / 关闭
# --------------------------------------------------------------------------

def spawn(exe: Path, args: list[str]) -> subprocess.Popen:
    """拉起进程：不共享控制台、不收本进程的 Ctrl+Break。

    这两个标志只管控制台与进程组归属，**不改父子关系**：壳侧关停后端超时后会强杀
    整棵进程树，这里拉起的进程照样在树里被带走。别把「游戏在本程序被结束后还活着」
    当成这两个标志能兑现的承诺。
    """
    return subprocess.Popen(
        [str(exe), *args],
        cwd=str(exe.parent),
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        creationflags=0x00000008 | 0x00000200,
        close_fds=True,
    )


def wait_for_window(exe_name: str, timeout_s: float, stop_event=None) -> int:
    """轮询等待游戏窗口出现，返回 hwnd；超时/被停止返回 0。

    每 0.5s 查一次：窗口出现是秒级事件，更密的轮询只是空耗 CPU。
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return 0
        hwnd = find_window_by_exe(exe_name)
        if hwnd:
            return hwnd
        time.sleep(0.5)
    return 0


def close_game(hwnd: int = 0, exe_name: str = "") -> str:
    """关闭游戏：优先按窗口句柄结束进程，找不到窗口时按进程名兜底。

    返回人类可读的结果串（供调用方写日志），失败不抛异常——关闭失败要能在日志里
    看见原因，而不是把整轮打成异常退出。
    """
    if hwnd:
        try:
            terminate_process_by_hwnd(hwnd)
            return "游戏进程已关闭"
        except Exception as exc:  # noqa: BLE001
            logger.log(f"[game] 按窗口关闭失败，改按进程名兜底: {exc!r}", "WARNING")
    if exe_name:
        n = terminate_by_exe(exe_name)
        if n:
            return f"游戏进程已关闭（按进程名结束 {n} 个）"
        return "未找到运行中的游戏进程"
    return "未找到游戏窗口，跳过关闭"
