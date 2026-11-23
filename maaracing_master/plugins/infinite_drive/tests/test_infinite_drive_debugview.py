# -*- coding: utf-8 -*-
"""无限驾驶调试图落盘器的回归锁。

覆盖：raw + 单图叠加图双落盘；索引 hud 指向实际文件；渲染异常时 hud=null
而原帧照常进索引；缺字段快照不抛、照常出图。布局与索引字段与录制局同一套
（session_layout 唯一口径）。
"""

from __future__ import annotations

import json

import numpy as np

from maaracing_master.plugins.infinite_drive import debugview
from maaracing_master.plugins.infinite_drive.debugview import DebugPairWriter
from maaracing_master.plugins.infinite_drive.session_layout import (
    SCHEMA_VERSION,
    SOURCE_CONTROL,
)


def _read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _drive_snapshot() -> dict:
    """最小可渲染的驾驶快照：一条线、一辆车、六个点位状态。"""
    lane = {
        "state": "TRACK", "n_locked": 2, "det_age_ms": 30.0,
        "theta_age_ms": 20.0, "y_v": 360.0, "usable": True,
        "hold": False, "pred": False, "det_fid": 12, "det_ts_ns": 1000,
        "traffic": None,
        "lines": [{"L": 1.2, "k": 1, "accepted": True, "resid": 0.1,
                   "fits": [{"s": 0.35, "a": 100.0, "y0": 400.0,
                             "y1": 700.0, "eval_y0": 300.0,
                             "eval_y1": 720.0, "band": "near"}]}],
    }
    return {
        "lane": lane, "ego_x": 0.2, "target": 0.2,
        "cars": [(0.2, 12.0)], "walled": False, "steer": 0.1,
        "throttle_scale": 1.0, "heading": 0.01, "target_heading": 0.02,
        "yaw_rate": 0.001, "usable": True, "fresh": True, "half_lanes": 2,
        "edge_states": [(-1.2, False), (-0.8, False), (-0.2, True),
                        (0.2, True), (0.8, False), (1.2, False)],
        "clearance": 0.6, "horizon_m": 40.0,
    }


def test_writer_saves_raw_and_overlay(tmp_path) -> None:
    """有决策快照时 raw 与叠加图都落盘，索引 hud 指向真实文件。"""
    out = tmp_path / "seg"
    writer = DebugPairWriter(out)
    frame = np.zeros((90, 160, 3), dtype=np.uint8)
    writer.tick(frame, {"fid": 7, "ts_ns": 123, "age_ms": 4.0,
                        "drive": _drive_snapshot()})
    writer.stop("chain_end")

    rows = _read_jsonl(out / "frames.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["frame_id"] == 7 and row["ts_ns"] == 123
    assert (out / row["file"]).is_file()            # raw/0001_raw.jpg 落盘
    assert row["hud"] is not None                   # 叠加图有实际文件名
    assert (out / row["hud"]).is_file()             # 索引不指向不存在的文件
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == SOURCE_CONTROL         # 与录制局同构 meta
    assert meta["schema"] == SCHEMA_VERSION
    assert meta["frames_written"] == 1
    assert meta["hud_written"] == 1
    assert writer.errors == 0


def test_writer_without_drive_still_draws_overlay(tmp_path) -> None:
    """非驾驶画面（无决策快照）仍出图：读数带显式标「无决策」，不整帧不留。"""
    out = tmp_path / "seg"
    writer = DebugPairWriter(out)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    writer.tick(frame, {"fid": 3, "ts_ns": 55, "age_ms": 2.0})
    writer.stop("chain_end")

    row = _read_jsonl(out / "frames.jsonl")[0]
    assert row["hud"] is not None and (out / row["hud"]).is_file()
    assert writer.errors == 0


def test_overlay_tolerates_missing_fields(tmp_path) -> None:
    """快照字段残缺（线缺参数、无车账、无点位）只让相应层缺席，不抛。"""
    out = tmp_path / "seg"
    writer = DebugPairWriter(out)
    drive = {"lane": {"lines": [{"fits": [{"s": None}]}, "junk"]},
             "ego_x": None, "target": None, "cars": None,
             "edge_states": None, "usable": False}
    writer.tick(np.zeros((60, 80, 3), dtype=np.uint8),
                {"fid": 4, "ts_ns": 66, "age_ms": 1.0, "drive": drive})
    writer.stop("chain_end")

    row = _read_jsonl(out / "frames.jsonl")[0]
    assert row["hud"] is not None and (out / row["hud"]).is_file()
    assert writer.errors == 0


def test_render_failure_keeps_index_hud_null(tmp_path, monkeypatch) -> None:
    """渲染异常：索引 hud=null、原帧照常落盘、异常只计数。"""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("render boom")

    monkeypatch.setattr(debugview, "render_debug", _boom)
    out = tmp_path / "seg"
    writer = DebugPairWriter(out)
    writer.tick(np.zeros((48, 64, 3), dtype=np.uint8),
                {"fid": 9, "ts_ns": 99, "age_ms": 3.0})
    writer.stop("chain_end")

    row = _read_jsonl(out / "frames.jsonl")[0]
    assert row["hud"] is None                       # 不写悬空指针
    assert (out / row["file"]).is_file()            # 原帧仍然可用
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["hud_written"] == 0
    assert meta["write_errors"] >= 1
    assert not (out / "0001_hud.jpg").exists()


def test_points_caption_distinguishes_lane_from_traffic() -> None:
    """点位缺席的文案分因：车道不可用 ≠ 同拍车账缺失；有点位时不写文案。"""
    assert debugview._edge_caption(None, "lane") == "points: lane not usable"
    assert debugview._edge_caption(None, "traffic") == "points: no car evidence"
    assert debugview._edge_caption([(0.0, False)], "lane") is None


def test_lane_label_skips_out_of_frame_endpoints() -> None:
    """线端点出界不画标签（不做边缘夹取），界内仍限制在横位尺之上。"""
    assert debugview._lane_label_pos(-30.0, 500.0, 1280, 720) is None
    assert debugview._lane_label_pos(1300.0, 500.0, 1280, 720) is None
    x, y = debugview._lane_label_pos(900.0, 800.0, 1280, 720)
    assert (x, y) == (900, 624)                 # 底界 = 高 - 96


def test_writer_dir_matches_session_dir(tmp_path) -> None:
    """dir 暴露会话根：模块收尾重建行为索引用它，不伸手拿私有字段。"""
    out = tmp_path / "seg"
    writer = DebugPairWriter(out)
    writer.stop("chain_end")
    assert writer.dir == out
