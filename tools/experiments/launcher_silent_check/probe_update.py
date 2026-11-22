#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动器搬运工更新探测：走完整「Update → 下载 → Ready」链路，采集日志信号。

回答三个问题（README 问题 2）：
1. 有更新时，启动器是否需要人工点「更新游戏」才开始下载？
2. 屏幕外的窗口能否隔空点击（PostMessage）触发下载？
3. 下载/安装完成的日志信号是什么？补丁装完后是否自动拉起游戏？

手段：拉起启动器后立即进入 0.25s 节拍的藏窗循环（任何可见窗口一律移出屏幕），
同时按 0.25s 增量采日志（轮转感知，只采本轮会话）；出现 `Final download config`
后解析 update_list，确认停在 Update 态再隔空点「更新游戏」按钮（截图标定：按钮
中心 ≈ 客户区 (0.811W, 0.899H)）；点击至多 2 次（避免误触下载暂停），之后只等
Ready。全程原始日志留档 _out/。

判据（标尺独立）：窗口事实 OS 口径；更新事实 = 启动器自写日志 + 游戏目录补丁文件
的出现（必要时手动核对），不依赖本探针自述。

用法（**须在提权终端运行**——启动器程序清单要求管理员）：
    .venv/Scripts/python.exe tools/experiments/launcher_silent_check/probe_update.py [等待秒数]
超时未到 Ready 时保留启动器运行（可能正在下载），交人工接管，不半途强杀。
"""

from __future__ import annotations

import ast
import ctypes
import ctypes.wintypes as wt
import subprocess  # noqa: F401  Popen 经由下方 spawn 使用
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_silent import (  # noqa: E402  同区共用：窗口/进程/关闭/日志工具
    _dpi_aware, _enum_windows_of, _log_path, _OUT_DIR, close_launcher,
)
from maaracing_master.core import game_process  # noqa: E402  读取面复用

_POLL = 0.25
_OFFSCREEN_X = -2000
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_MK_LBUTTON = 0x0001
_MAX_CLICKS = 2
_CLICK_RETRY_AFTER = 45.0  # 点击后状态久留 Update 才补点；下载可能就在 Update 态内进行

# 按钮中心在客户区的比例位（1366×796 截图标定，按钮约 300×50，容差充足）
_BTN_FRAC = (0.811, 0.899)

_DOWNLOAD_HINTS = ("download", "patch", "speed", "percent", "progress",
                   "complete", "finish", "merge", "wpatch")
_MON_LINES = ("set launcher state", "Final download config",
              "real_start_game", "start game", "error", "exception",
              "Launcher version") + _DOWNLOAD_HINTS


def _transcript(all_lines: list[str], name: str) -> Path:
    out = _OUT_DIR / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out.write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    return out


def _pick_main_window(wins: list[dict]) -> dict | None:
    """挑主窗口：可见、面积最大、标题含「巅峰」者优先。"""
    cands = [w for w in wins if w["visible"]]
    if not cands:
        return None
    titled = [w for w in cands if "巅峰" in w["title"]]
    pool = titled or cands
    return max(pool, key=lambda w: (w["rect"][2] - w["rect"][0]) * (w["rect"][3] - w["rect"][1]))


def _client_click(hwnd: int, log) -> bool:
    """按标定比例往主窗口客户区投递一次左键点击。返回是否投出。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    r = wt.RECT()
    if not user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r)):
        return False
    cw, ch = r.right - r.left, r.bottom - r.top
    x, y = int(cw * _BTN_FRAC[0]), int(ch * _BTN_FRAC[1])
    lp = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(wt.HWND(hwnd), _WM_LBUTTONDOWN, _MK_LBUTTON, lp)
    time.sleep(0.08)
    user32.PostMessageW(wt.HWND(hwnd), _WM_LBUTTONUP, 0, lp)
    log(f"  已向 hwnd={hwnd} 客户区 ({x},{y})/{cw}x{ch} 投递点击")
    return True


def _hide_all_visible(moves: list[str]) -> int:
    """把启动器所有可见且在屏幕上的窗口移出屏幕。返回本次移动数（时间线另记）。"""
    moved = 0
    for w in _enum_windows_of("Launcher.exe"):
        if w["visible"] and w["rect"][0] > _OFFSCREEN_X:
            ctypes.WinDLL("user32").SetWindowPos(
                wt.HWND(w["hwnd"]), 0, -32000, -32000, 0, 0, 0x0001 | 0x0004 | 0x0010)
            moved += 1
            moves.append(f"{datetime.now():%H:%M:%S} hwnd={w['hwnd']} rect={w['rect']} title={w['title']!r}")
    return moved


def _parse_final_config(lines: list[str]) -> dict | None:
    """从日志行里找 `Final download config:` 的下一行 dict 并解析。"""
    for i, line in enumerate(lines):
        if "Final download config" in line and i + 1 < len(lines):
            raw = lines[i + 1].split("]: ", 1)[-1].strip()
            try:
                return ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return None
    return None


class LogTail:
    """按行增量读启动器日志（轮转感知）。

    `raw` 是本轮会话的累计全文：轮转前的旧内容一律不收，避免上一场会话的状态行
    污染 config 解析与留档。
    """

    def __init__(self):
        self.offset = 0
        self.rotated = False
        self.raw: list[str] = []
        self.pre_lines = self._current_lines()

    def _current_lines(self) -> int:
        try:
            return len(_log_path().read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return 0

    def poll(self) -> list[str]:
        try:
            lines = _log_path().read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        if not self.rotated:
            if len(lines) >= self.pre_lines:
                return []  # 还是旧会话文件，不采
            self.rotated = True
            self.offset = 0
            self.raw = []
        new = lines[self.offset:]
        self.offset = len(lines)
        self.raw.extend(new)
        return new


def main(argv: list[str]) -> int:
    _dpi_aware()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("!! 本探针必须在提权终端运行（启动器清单要求管理员）")
        return 3
    exe = game_process.detect_launcher_exe()
    if exe is None or not exe.is_file():
        print("!! 反查不到启动器")
        return 2
    budget = float(argv[1]) if len(argv) > 1 else 600.0

    all_lines: list[str] = [f"# probe_update {datetime.now():%Y-%m-%d %H:%M:%S}", f"启动器: {exe}"]

    def log(msg: str) -> None:
        stamp = f"[{datetime.now():%H:%M:%S}] "
        print(stamp + msg)
        all_lines.append(stamp + msg)

    if game_process.game_running("Launcher.exe"):
        log("启动器已在运行，先关闭")
        close_launcher(log)
        time.sleep(1.0)

    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),  # noqa: S603
                            creationflags=0x00000008 | 0x00000200, close_fds=True)
    log(f"已拉起启动器 (pid={proc.pid})，进入藏窗+采样循环（每 {_POLL}s）")

    tail = LogTail()
    config: dict | None = None
    state = ""
    clicks = 0
    clicked_at: float | None = None
    ready = False
    auto_game = False
    moved_windows: list[str] = []
    t0 = time.monotonic()

    while time.monotonic() - t0 < budget:
        t = time.monotonic() - t0
        n = _hide_all_visible(moved_windows)
        if n:
            log(f"  [{t:6.1f}s] 藏窗：移出 {n} 个窗口")

        for line in tail.poll():
            if any(kw in line for kw in _MON_LINES):
                log(f"  [{t:6.1f}s] {line}")
            if "set launcher state:" in line:
                state = line.split("LaunchStateEnum.")[-1].strip()

        if tail.rotated and config is None:
            config = _parse_final_config(tail.raw)
            if config is not None:
                n_files = len(config.get("update_list") or [])
                log(f"  [{t:6.1f}s] 解析 Final download config：待更 {n_files} 个文件，"
                    f"共 {config.get('size', 0) / 1e6:.1f} MB，当前状态={state or '?'}")

        # 阶段二：Update 态 + 主窗口在手 → 隔空点击（至多 _MAX_CLICKS 次）
        if (config is not None and state == "Update" and not ready
                and clicks < _MAX_CLICKS
                and (clicked_at is None or t - clicked_at >= _CLICK_RETRY_AFTER)):
            win = _pick_main_window(_enum_windows_of("Launcher.exe"))
            if win is not None and _client_click(win["hwnd"], log):
                clicks += 1
                clicked_at = t
                log(f"  [{t:6.1f}s] 第 {clicks} 次点击已投递（状态={state}）")

        if state == "Ready":
            ready = True
            log(f"  [{t:6.1f}s] 状态到达 Ready——补丁流程完成")
            break

        if game_process.game_running("g112.exe"):
            auto_game = True
            log(f"  [{t:6.1f}s] 检测到 g112.exe 进程——启动器自行拉起了游戏")
            break

        time.sleep(_POLL)

    elapsed = time.monotonic() - t0
    log(f"收尾：ready={ready} auto_game={auto_game} clicks={clicks} 用时={elapsed:.0f}s")
    if ready or auto_game:
        close_launcher(log)
        if auto_game:
            # 启动器自拉的游戏也一并带走，机器留干净；证据已进日志
            n1 = game_process.terminate_by_exe("g112.exe")
            n2 = game_process.terminate_by_exe("g112-Win64-Shipping.exe")
            log(f"已关闭自动拉起的游戏进程（g112={n1}, shipping={n2}）")
    else:
        log("!! 未到达 Ready：保留启动器运行（可能正在下载），请人工接管")

    if moved_windows:
        all_lines.append("")
        all_lines.append("# 藏窗记录（每个被移出屏幕的窗口事件）")
        all_lines.extend(moved_windows)

    out = _transcript(all_lines, "probe_update")
    raw = _OUT_DIR / f"update_raw_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
    raw.write_text("\n".join(tail.raw) + "\n", encoding="utf-8")
    print(f"时间线: {out}\n本轮原始日志: {raw}")
    return 0 if (ready or auto_game) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
