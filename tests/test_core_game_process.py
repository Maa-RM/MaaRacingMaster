# -*- coding: utf-8 -*-
"""游戏进程原语（`core/game_process.py`）。

看护的是启动/关闭链的**编排与降级路径**：直启参数与进程组标志怎么传、窗口等待的
轮询/停止/超时三分支、按窗口关闭失败后改按进程名兜底的升级顺序。Windows API 与
子进程全部用伪对象（不碰真进程），ini 解析用 tmp_path 真文件。
"""
from __future__ import annotations

import types
from pathlib import Path

from maaracing_master.core import game_process as gp


def _write_ini(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 安装定位
# --------------------------------------------------------------------------

def test_read_ini_section_parses_and_skips_comments(tmp_path):
    ini = _write_ini(tmp_path / "config.ini", (
        "[Other]\n"
        "game_dir=WRONG\n"
        "\n"
        "; comment\n"
        "# comment\n"
        "[Info]\n"
        "GAME_DIR = C:/Windows/Game\n"
        "missing=ok\n"))
    out = gp._read_ini_section(ini, "info")  # 小节名与键名大小写不敏感
    assert out == {"game_dir": "C:/Windows/Game", "missing": "ok"}


def test_read_ini_section_missing_file_returns_empty(tmp_path):
    assert gp._read_ini_section(tmp_path / "nope.ini", "Info") == {}


def test_launcher_config_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert (gp.launcher_config_path()
            == tmp_path / "RacingMaster" / "CN" / "config" / "config.ini")


def test_detect_game_exe_returns_exe_when_installed(tmp_path, monkeypatch):
    game_dir = tmp_path / "install"
    exe = game_dir / "g112" / "Binaries" / "Win64" / "g112-Win64-Shipping.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    ini = _write_ini(tmp_path / "config.ini", f"[Info]\ngame_dir={game_dir}\n")
    monkeypatch.setattr(gp, "launcher_config_path", lambda: ini)

    assert gp.detect_game_exe() == exe


def test_detect_game_exe_without_game_dir_returns_none(tmp_path, monkeypatch):
    ini = _write_ini(tmp_path / "config.ini", "[Info]\n")
    monkeypatch.setattr(gp, "launcher_config_path", lambda: ini)
    assert gp.detect_game_exe() is None


def test_detect_game_exe_uninstalled_path_returns_none(tmp_path, monkeypatch):
    ini = _write_ini(tmp_path / "config.ini", f"[Info]\ngame_dir={tmp_path / 'nowhere'}\n")
    monkeypatch.setattr(gp, "launcher_config_path", lambda: ini)
    assert gp.detect_game_exe() is None


# --------------------------------------------------------------------------
# 启动 / 等待 / 关闭
# --------------------------------------------------------------------------

def test_spawn_passes_cwd_and_process_group_flags(tmp_path, monkeypatch):
    exe = tmp_path / "game.exe"
    captured: dict = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(gp, "subprocess", types.SimpleNamespace(Popen=_fake_popen))

    gp.spawn(exe, ["-nosound", "-nohmd"])

    assert captured["cmd"] == [str(exe), "-nosound", "-nohmd"]
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == str(exe.parent)
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：控制台与进程组归属，不改父子关系
    assert kwargs["creationflags"] == 0x00000008 | 0x00000200
    assert kwargs["close_fds"] is True


def test_wait_for_window_returns_first_hit(monkeypatch):
    hits = iter([0, 0, 4321])
    monkeypatch.setattr(gp, "find_window_by_exe", lambda name: next(hits))
    monkeypatch.setattr(gp.time, "sleep", lambda s: None)

    assert gp.wait_for_window("game.exe", timeout_s=10.0) == 4321


def test_wait_for_window_zero_timeout_returns_zero(monkeypatch):
    monkeypatch.setattr(gp, "find_window_by_exe", lambda name: 0)
    assert gp.wait_for_window("game.exe", timeout_s=0.0) == 0


def test_wait_for_window_stops_on_stop_event_before_window_lookup(monkeypatch):
    # 窗口已在也应先看停止信号：被停止的等待不得拿窗口句柄
    monkeypatch.setattr(gp, "find_window_by_exe",
                        lambda name: (_ for _ in ()).throw(AssertionError("不该查窗口")))

    class _Ev:
        def is_set(self):
            return True

    assert gp.wait_for_window("game.exe", timeout_s=10.0, stop_event=_Ev()) == 0


def test_find_window_by_process_targets_shipping_exe_name(monkeypatch):
    # 按进程可执行文件名匹配，不按标题：启动器窗口与游戏窗口同名，标题会认错
    got: dict = {}

    def _fake_find(name):
        got["name"] = name
        return 7

    monkeypatch.setattr(gp, "find_window_by_exe", _fake_find)
    assert gp.find_window_by_process() == 7
    assert got["name"] == "g112-Win64-Shipping.exe"


def test_close_game_by_hwnd(monkeypatch):
    hwnds: list = []
    monkeypatch.setattr(gp, "terminate_process_by_hwnd", lambda hwnd: hwnds.append(hwnd))
    monkeypatch.setattr(gp, "terminate_by_exe",
                        lambda name: (_ for _ in ()).throw(AssertionError("不该走进程名兜底")))

    assert gp.close_game(hwnd=123, exe_name="game.exe") == "游戏进程已关闭"
    assert hwnds == [123]


def test_close_game_hwnd_failure_falls_back_to_exe(monkeypatch):
    def _boom(hwnd):
        raise OSError("window gone")

    exe_names: list = []
    monkeypatch.setattr(gp, "terminate_process_by_hwnd", _boom)
    monkeypatch.setattr(gp, "terminate_by_exe", lambda name: exe_names.append(name) or 2)

    assert gp.close_game(hwnd=123, exe_name="game.exe") == "游戏进程已关闭（按进程名结束 2 个）"
    assert exe_names == ["game.exe"]


def test_close_game_by_exe_zero_hits_reports_not_found(monkeypatch):
    monkeypatch.setattr(gp, "terminate_by_exe", lambda name: 0)
    assert gp.close_game(hwnd=0, exe_name="game.exe") == "未找到运行中的游戏进程"


def test_close_game_without_target_skips(monkeypatch):
    monkeypatch.setattr(gp, "terminate_process_by_hwnd",
                        lambda hwnd: (_ for _ in ()).throw(AssertionError("无窗口不该按窗口关")))
    monkeypatch.setattr(gp, "terminate_by_exe",
                        lambda name: (_ for _ in ()).throw(AssertionError("无进程名不该按进程关")))
    assert gp.close_game() == "未找到游戏窗口，跳过关闭"
