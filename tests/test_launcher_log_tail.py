# -*- coding: utf-8 -*-
"""启动器日志读取：会话边界识别与读取游标。

用真实文件读写覆盖三种实际情形（只替换日志路径，不桩读取函数本身）：
  拉起前日志不存在 → 启动器随后新建；
  同一份日志被续写 → 自动更新与 Ready 落在同一个文件里；
  新会话重开文件 → 内容被截断重写，长短都可能与旧文件不同。
反向约束：初始化前的旧行不论内容如何都不采，防上一场会话的状态行污染判定。
"""
from __future__ import annotations

import threading
import time

import pytest

from maaracing_master.core import game_process as gp

OLD_LINES = 40
REPLACED_LINES = 60
CONFIG_UPDATE = (
    "Final download config:\n"
    "[2026-11-22.10-00-00:000][Log]: {'size': 2048, 'update_list': ['a.pak', 'b.pak']}\n"
)
CONFIG_FRESH = (
    "Final download config:\n"
    "[2026-11-22.10-00-01:000][Log]: {'size': 0, 'update_list': []}\n"
)
READY = "set launcher state: LaunchStateEnum.Ready\n"


@pytest.fixture()
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "Log.txt"
    monkeypatch.setattr(gp, "launcher_log_path", lambda: path)
    return path


def _old_session(lines: int = OLD_LINES) -> str:
    return "\n".join(f"old session line {i}" for i in range(lines)) + "\n"


def _append_later(path, text: str, delay: float = 0.1) -> threading.Thread:
    def run():
        time.sleep(delay)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- 读取器本身


def test_tail_takes_lines_of_log_created_after_init(log_path) -> None:
    tail = gp._LauncherLogTail()
    assert not log_path.exists()
    log_path.write_text(CONFIG_FRESH, encoding="utf-8")
    assert tail.poll() == CONFIG_FRESH.splitlines()
    assert tail.poll() == []


def test_tail_reads_only_appended_lines(log_path) -> None:
    log_path.write_text(_old_session(), encoding="utf-8")
    tail = gp._LauncherLogTail()
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(READY)
    assert tail.poll() == [READY.rstrip("\n")]


def test_tail_rereads_replaced_file_even_if_longer(log_path) -> None:
    log_path.write_text("old\n", encoding="utf-8")
    tail = gp._LauncherLogTail()
    fresh = [f"new session line {i}" for i in range(REPLACED_LINES)]
    log_path.write_text("\n".join(fresh) + "\n", encoding="utf-8")
    assert tail.poll() == fresh


def test_tail_holds_back_incomplete_last_line(log_path) -> None:
    log_path.write_text("", encoding="utf-8")
    tail = gp._LauncherLogTail()
    log_path.write_text("half a li", encoding="utf-8")
    assert tail.poll() == []
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("ne\n")
    assert tail.poll() == ["half a line"]


# ------------------------------------------------- 上层等待函数（走真实读取器）


def test_check_reads_conclusion_from_log_created_after_spawn(log_path) -> None:
    thread = _append_later(log_path, CONFIG_UPDATE)
    try:
        result = gp.wait_launcher_check(None, timeout_s=5.0)
    finally:
        thread.join()
    assert result == {"update_needed": True, "size": 2048, "files": 2}


def test_ready_reads_state_appended_to_same_log(log_path) -> None:
    log_path.write_text(_old_session(), encoding="utf-8")
    thread = _append_later(log_path, READY)
    try:
        assert gp.wait_launcher_ready(None, timeout_s=5.0) == "ready"
    finally:
        thread.join()


def test_check_takes_new_conclusion_when_session_rewrites_log(log_path) -> None:
    log_path.write_text(_old_session() + CONFIG_UPDATE, encoding="utf-8")

    def rewrite():
        time.sleep(0.1)
        log_path.write_text(CONFIG_FRESH, encoding="utf-8")

    thread = threading.Thread(target=rewrite, daemon=True)
    thread.start()
    try:
        # 旧结论（需要更新）被新会话整体替换后，结果只反映新结论
        assert gp.wait_launcher_check(None, timeout_s=5.0) == {
            "update_needed": False, "size": 0, "files": 0}
    finally:
        thread.join()


def test_old_session_lines_are_never_consumed(log_path) -> None:
    log_path.write_text(_old_session() + READY + CONFIG_UPDATE, encoding="utf-8")
    assert gp.wait_launcher_check(None, timeout_s=1.0) is None
    assert gp.wait_launcher_ready(None, timeout_s=1.0) == "timeout"
