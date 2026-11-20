# -*- coding: utf-8 -*-
"""宏录制/回放（``plugins/infinite_drive/macro.py``）的回归锁。

分四层，全部不碰真手柄/真游戏：
- 合并规则：键盘覆盖轴/扳机、A+D 回中、W/S 是两个独立扳机；
- 文件层：读写校验、越界/时间倒退/缺文件拒绝、宏组枚举与最新组解析；
- 录制器：短线程写 p1.jsonl + meta（g0 由 HUD 外推，meta 记槽位）；
- 回放器：等待对齐、播完松手、漂移重锚、非法输入拒绝。
"""
from __future__ import annotations

import contextlib
import json
import time

import pytest

from maaracing_master.core import xinput
from maaracing_master.plugins.infinite_drive import macro




def _pad(lx=0, ly=0, rx=0, ry=0, lt=0, rt=0, btn=0) -> xinput.PadState:
    return xinput.PadState(index=0, packet=1, buttons=btn,
                           left_trigger=lt, right_trigger=rt,
                           left_stick=(lx, ly), right_stick=(rx, ry))


# ---------- 合并规则 ----------

def test_merge_without_pad_or_keys_is_neutral():
    assert macro.merge_sample(None, {}) == {
        "lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "btn": 0}


def test_merge_keyboard_steer_overrides_pad():
    pad = _pad(lx=1000)
    assert macro.merge_sample(pad, {"a": True})["lx"] == -xinput.STICK_MAX
    assert macro.merge_sample(pad, {"right": True})["lx"] == xinput.STICK_MAX
    assert macro.merge_sample(pad, {"a": True, "d": True})["lx"] == 0
    assert macro.merge_sample(pad, {})["lx"] == 1000


def test_merge_w_and_s_are_independent_triggers():
    pad = _pad(rt=120, lt=30)
    both = macro.merge_sample(pad, {"w": True, "s": True})
    assert both["rt"] == xinput.TRIGGER_MAX
    assert both["lt"] == xinput.TRIGGER_MAX
    assert macro.merge_sample(pad, {"w": True})["lt"] == 30
    assert macro.merge_sample(pad, {"s": True})["rt"] == 120
    assert macro.merge_sample(pad, {})["rt"] == 120


# ---------- 文件层 ----------

def _write_group(root, name, rows, *, phase=1, meta=None):
    d = root / name
    d.mkdir(parents=True)
    (d / f"p{phase}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def _row(t_ms, **over):
    base = {"t_ms": t_ms, "lx": 0, "ly": 0, "rx": 0, "ry": 0,
            "lt": 0, "rt": 0, "btn": 0}
    base.update(over)
    return base


def _meta(phases: dict) -> dict:
    return {"schema": macro.SCHEMA_VERSION, "phases": phases}


def _done(reason: str = "phase_end") -> dict:
    """一段录完的元数据记录：正常收尾 + 时长（时长不再进标签，只留作内容）。"""
    return {"stop_reason": reason, "samples": 10, "duration_ms": 32000}


def test_load_phase_roundtrip_and_g0(tmp_path):
    d = _write_group(tmp_path, "20261103_120000", [_row(0, rt=255), _row(16.6, lx=-100)],
                     meta={"phases": {"1": {"g0": 34.5}}})
    samples, g0 = macro.load_phase(d, 1)
    assert [s["rt"] for s in samples] == [255, 0]
    assert g0 == 34.5


def test_load_phase_rejects_bad_files(tmp_path):
    with pytest.raises(macro.MacroError):
        macro.load_phase(tmp_path, 1)  # 缺文件

    d = _write_group(tmp_path, "a", [{"t_ms": 0}])      # 缺字段
    d2 = _write_group(tmp_path, "b", [_row(0), _row(-1)])
    d3 = _write_group(tmp_path, "c", [_row(0, lx=99999)])
    d4 = _write_group(tmp_path, "d", [_row(5), _row(1)])
    for bad in (d, d2, d3, d4):
        with pytest.raises(macro.MacroError):
            macro.load_phase(bad, 1)


def test_group_listing_requires_recording_record(tmp_path):
    """没有收尾记录的目录不列（半截产物不冒充素材）；只录了一半的列出并标不完整。"""
    _write_group(tmp_path, "20261104_090000", [_row(0)])          # 崩溃留下的一半
    _write_group(tmp_path, "20261103_120000", [_row(0)],
                 meta=_meta({"1": _done()}))                       # 缺阶段 2
    groups = macro.list_groups(tmp_path)
    assert [g["value"] for g in groups] == ["20261103_120000"]
    assert groups[0]["label"] == "2026-11-03 12:00:00（不完整）"
    assert macro.resolve_group("", tmp_path).name == "20261103_120000"
    with pytest.raises(macro.MacroError):
        macro.resolve_group("20261104_090000", tmp_path)
    with pytest.raises(macro.MacroError):
        macro.resolve_group("../evil", tmp_path)
    with pytest.raises(macro.MacroError):
        macro.resolve_group("missing", tmp_path)


def test_group_listing_labels_seconds_and_collision_suffix(tmp_path):
    """标签给到秒；同秒撞名的序号要显出来，否则两组在下拉里长得一样。"""
    _write_group(tmp_path, "20261103_142233", [_row(0)],
                 meta=_meta({"1": _done(), "2": _done()}))
    _write_group(tmp_path, "20261103_142233_2", [_row(0)],
                 meta=_meta({"1": _done("stopped"), "2": _done()}))
    groups = macro.list_groups(tmp_path)
    assert [g["label"] for g in groups] == [
        "2026-11-03 14:22:33 (2)（不完整）", "2026-11-03 14:22:33"]


def test_group_listing_skips_non_token_dirs(tmp_path):
    """目录名不合会话编号格式的（人工改过、别的用途）不进下拉。"""
    _write_group(tmp_path, "hand_named", [_row(0)], meta=_meta({"1": _done()}))
    _write_group(tmp_path, "20261103_120000", [_row(0)], meta=_meta({"1": _done()}))
    assert [g["value"] for g in macro.list_groups(tmp_path)] == ["20261103_120000"]


def test_choice_label_covers_latest_locked_and_stale():
    assert macro.choice_label("", []) == "无"
    items = [{"value": "20261103_120000", "label": "2026-11-03 12:00:00"}]
    assert macro.choice_label(None, items) == "最新 · 2026-11-03 12:00:00"
    assert macro.choice_label("20261103_120000", items) == "2026-11-03 12:00:00"
    assert macro.choice_label("20260101_000000", items) == "无（原选项已失效）"


# ---------- 录制器 ----------

def test_recorder_writes_rows_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(macro.xinput, "read_state",
                        lambda i: _pad(lx=-9000, rt=120) if i == 0 else None)
    rec = macro.MacroRecorder(tmp_path / "g", phase=1, poll_hz=100,
                              foreground=lambda: True,
                              keys_reader=lambda: {"w": True})
    rec.start()
    time.sleep(0.06)
    rec.note_clock(30, time.perf_counter_ns())
    time.sleep(0.06)
    rec.stop()

    rows = [json.loads(line) for line in
            (tmp_path / "g" / "p1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows
    assert all(r["rt"] == xinput.TRIGGER_MAX for r in rows), "W 应覆盖手柄扳机"
    assert any(r["lx"] == -9000 for r in rows)
    meta = json.loads((tmp_path / "g" / "meta.json").read_text(encoding="utf-8"))
    assert meta["schema"] == macro.SCHEMA_VERSION
    assert meta["pad_slot"] == 0
    assert meta["phases"]["1"]["g0"] > 30
    assert "C:\\" not in json.dumps(meta) and "/Users/" not in json.dumps(meta)


class _StuckThread:
    """join 立即返回但线程未死：把 stop() 的超时分支变成确定时序。"""

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return True


def test_recorder_stop_timeout_leaves_file_to_thread(tmp_path, monkeypatch):
    """join 超时线程还活着：不抢关文件、不抢写 meta、保留线程引用，降级告警。"""
    rec = macro.MacroRecorder(tmp_path / "g", phase=1)
    logs = []
    monkeypatch.setattr(rec, "_log", lambda msg, level="INFO": logs.append((level, msg)))
    rec._running = True
    rec._file = (tmp_path / "p1.jsonl").open("w", encoding="utf-8", buffering=1)
    stuck = _StuckThread()
    rec._thread = stuck
    try:
        rec.stop()
        assert not rec._file.closed, "活线程还在写，stop 不得关文件"
        assert rec._thread is stuck, "线程引用保留（后续可再 join 排查）"
        assert not (tmp_path / "g" / "meta.json").exists(), "统计仍归线程，不抢写 meta"
        assert any(level == "WARNING" for level, _ in logs)
    finally:
        rec._file.close()


def test_recorder_thread_closes_file_on_exit(tmp_path, monkeypatch):
    """文件关闭权归线程：不经 stop()，线程退出时自己关文件。"""
    monkeypatch.setattr(macro.xinput, "read_state", lambda i: _pad())
    rec = macro.MacroRecorder(tmp_path / "g", phase=1, poll_hz=100,
                              foreground=lambda: True)
    rec.start()
    time.sleep(0.03)
    f = rec._file
    rec._stop.set()
    rec._thread.join(timeout=2.0)
    assert not rec._thread.is_alive()
    assert f.closed, "线程退出必须自己关文件（stop 超时放手时这是唯一关闭点）"


# ---------- 回放器 ----------

class _StubPad:
    def __init__(self):
        self.calls = []

    def left_joystick(self, x_value=0, y_value=0):
        self.calls.append(("joy", x_value, y_value))

    def left_trigger(self, value=0):
        self.calls.append(("lt", value))

    def right_trigger(self, value=0):
        self.calls.append(("rt", value))

    def update(self):
        self.calls.append(("update",))


def _samples():
    return [
        {"t_ms": 0.0, "lx": -100, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 255, "btn": 0},
        {"t_ms": 20.0, "lx": 100, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "btn": 0},
    ]


def test_player_waits_for_alignment_then_plays_and_releases():
    pad = _StubPad()
    clock = {"v": (31.0, time.perf_counter_ns())}
    player = macro.MacroPlayer(_samples(), 30.0,
                               pad_cm=lambda: contextlib.nullcontext(pad),
                               phase=1, clock=lambda: clock["v"])
    player.start()
    time.sleep(0.05)
    assert player.stats["state"] in ("waiting", "playing")
    clock["v"] = (30.0, time.perf_counter_ns())
    deadline = time.time() + 2.0
    while time.time() < deadline and player.stats["state"] != "finished":
        time.sleep(0.01)
    player.stop()
    assert player.stats["state"] == "finished"
    assert player.stats["sent"] == 2
    assert ("joy", -100, 0) in pad.calls
    assert ("rt", 255) in pad.calls
    assert ("joy", 100, 0) in pad.calls
    assert ("update",) in pad.calls


def test_player_drift_reanchors_only_beyond_threshold():
    player = macro.MacroPlayer(_samples(), 30.0,
                               pad_cm=lambda: contextlib.nullcontext(_StubPad()),
                               phase=1)
    player._t0_ns = 1_000_000_000
    player._correct_drift(29.9, 1_000_000_000)   # 偏差 0.1s：不动
    assert player._t0_ns == 1_000_000_000
    player._correct_drift(33.0, 1_500_000_000)   # 期望 29.5 → 偏差 +3.5s
    assert player._t0_ns == 1_000_000_000 + int(3.5e9)


def test_player_without_g0_starts_immediately():
    player = macro.MacroPlayer(_samples(), None,
                               pad_cm=lambda: contextlib.nullcontext(_StubPad()),
                               phase=1)
    now = time.perf_counter_ns()
    assert player._align((None, 0), now, time.monotonic() + 5.0) is True
    assert player._t0_ns == now


def test_player_stop_timeout_keeps_thread_ref(monkeypatch):
    """join 超时线程还活着：保留引用、降级告警，手柄租约由线程自己释放。"""
    player = macro.MacroPlayer(_samples(), None,
                               pad_cm=lambda: contextlib.nullcontext(_StubPad()),
                               phase=1)
    logs = []
    monkeypatch.setattr(player, "_log",
                        lambda msg, level="INFO": logs.append((level, msg)))
    player._running = True
    stuck = _StuckThread()
    player._thread = stuck
    assert player.stop() is False, "超时未退必须上报失败（调用方据此保留手柄设备）"
    assert player._thread is stuck
    assert any(level == "WARNING" for level, _ in logs)


def test_player_stop_returns_true_when_thread_exits():
    player = macro.MacroPlayer(_samples(), None,
                               pad_cm=lambda: contextlib.nullcontext(_StubPad()),
                               phase=1)
    player.start()
    assert player.stop() is True
    assert player._thread is None
