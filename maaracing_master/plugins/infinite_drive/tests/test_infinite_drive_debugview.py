# -*- coding: utf-8 -*-
"""无限驾驶调试图落盘器的回归锁。

覆盖：raw-only 模式（render_hud=False）只落原帧、索引 hud=null、无叠加图；
meta 与叠加局同构。布局与索引字段与录制局同一套（session_layout 唯一口径），
后续控制链接上策略快照切回 render_hud=True 时零迁移。
"""

from __future__ import annotations

import json

import numpy as np

from maaracing_master.plugins.infinite_drive.debugview import DebugPairWriter
from maaracing_master.plugins.infinite_drive.session_layout import (
    SCHEMA_VERSION,
    SOURCE_CONTROL,
)


def _read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_raw_only_writer_saves_raw_with_null_hud(tmp_path) -> None:
    """render_hud=False 只存原帧：raw 落盘、索引 hud=null、无叠加图文件。

    控制链还没有策略快照可叠时的过渡形态（2026-11-21）：策略面未接，叠加图
    暂无内容可画；缺席必须用索引 hud=null 表达，不许整行不留。"""
    out = tmp_path / "seg"
    writer = DebugPairWriter(out, cal=object(), render_hud=False)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    writer.tick(frame, {"fid": 7, "ts_ns": 123, "age_ms": 4.0})
    writer.stop("chain_end")

    rows = _read_jsonl(out / "frames.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["frame_id"] == 7 and row["ts_ns"] == 123
    assert row["hud"] is None                       # 叠加图缺席=索引 null
    assert (out / row["file"]).is_file()            # raw/0001_raw.jpg 落盘
    assert not (out / "0001_hud.jpg").exists()      # 不产叠加图
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == SOURCE_CONTROL         # 与叠加局同构 meta
    assert meta["schema"] == SCHEMA_VERSION
    assert meta["frames_written"] == 1
    assert meta["hud_written"] == 0


def test_writer_dir_matches_session_dir(tmp_path) -> None:
    """dir 暴露会话根：模块收尾重建行为索引用它，不伸手拿私有字段。"""
    out = tmp_path / "seg"
    writer = DebugPairWriter(out, cal=object(), render_hud=False)
    writer.stop("chain_end")
    assert writer.dir == out
