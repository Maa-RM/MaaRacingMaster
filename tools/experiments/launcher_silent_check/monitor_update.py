#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新链路只读监视器：跟随「下载 → 安装 → Ready」，不碰启动器。

probe_update.py 被中断后，下载已在启动器内进行；本脚本只做三件事：
1. 按秒采日志，把状态机、逐文件完成、异常行打上时间戳；
2. 观察补丁装完后启动器是否自行拉起游戏（real_start_game / g112.exe 进程）；
3. 到达 Ready 或超时为止，全程不投递任何输入、不结束任何进程。

无需提权（只读日志与进程表）。到 Ready 后再观察 15s 才退出，以便捕获 Ready 后
的自动行为。原始会话日志由调用方自行拷贝留档。

用法（仓库根）：
    .venv/Scripts/python.exe tools/experiments/launcher_silent_check/monitor_update.py [超时秒]
"""

from __future__ import annotations

import ctypes
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_silent import _OUT_DIR, _log_path  # noqa: E402
from maaracing_master.core import game_process  # noqa: E402  读取面复用

_MON_KEYWORDS = (
    "set launcher state", "Final download config", "[Finish]",
    "download file", "set key,value", "real_start_game", "start game",
    "error", "exception", "patch", "merge", "verify", "check dll",
    "compare local remote md5", "Launcher version",
)
_READY_GRACE_S = 15.0


def main(argv: list[str]) -> int:
    budget = float(argv[1]) if len(argv) > 1 else 540.0
    all_lines: list[str] = [f"# monitor_update {datetime.now():%Y-%m-%d %H:%M:%S}"]

    def log(msg: str) -> None:
        stamp = f"[{datetime.now():%H:%M:%S}] "
        print(stamp + msg)
        all_lines.append(stamp + msg)

    log_path = _log_path()
    try:
        offset = len(log_path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        offset = 0
    log(f"从第 {offset} 行起跟随 {log_path.name}")

    state = ""
    ready_at: float | None = None
    auto_game = False
    finishes = 0
    t0 = time.monotonic()

    while time.monotonic() - t0 < budget:
        t = time.monotonic() - t0
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        new = lines[offset:] if len(lines) > offset else []
        offset = max(offset, len(lines))
        for line in new:
            if not any(k in line for k in _MON_KEYWORDS):
                continue
            log(f"  [{t:6.1f}s] {line}")
            if "set launcher state:" in line:
                state = line.split("LaunchStateEnum.")[-1].strip()
                if state == "Ready" and ready_at is None:
                    ready_at = t
            elif "[Finish]" in line:
                finishes += 1
            elif "real_start_game" in line or "start game:" in line:
                auto_game = True

        if ready_at is None and not auto_game and game_process.game_running("g112.exe"):
            auto_game = True
            log(f"  [{t:6.1f}s] 检测到 g112.exe 进程——启动器自行拉起了游戏")

        if ready_at is not None and t - ready_at >= _READY_GRACE_S:
            break
        time.sleep(1.0)

    elapsed = time.monotonic() - t0
    log(f"结束：state={state or '?'} ready_at={ready_at} auto_game={auto_game} "
        f"完成文件数={finishes} 用时={elapsed:.0f}s")

    if ready_at is not None:
        raw = _OUT_DIR / f"update_raw_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
        shutil.copyfile(log_path, raw)
        log(f"本轮会话原始日志已留档: {raw}")
    out = _OUT_DIR / f"monitor_update_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out.write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    print(f"时间线: {out}")
    return 0 if ready_at is not None else 1


if __name__ == "__main__":
    if not hasattr(ctypes, "WinDLL"):  # 非 Windows：本实验无法进行
        raise SystemExit(3)
    raise SystemExit(main(sys.argv))
