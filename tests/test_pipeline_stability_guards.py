# -*- coding: utf-8 -*-
"""启动链未知页兜底的静态回归锁。

未知页文字兜底必须经节流门，不能直接挂在汇聚节点每轮候选里；门还要带回汇聚
节点的退路，否则兜底未命中会把整张图等超时带死。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STARTUP = REPO / "maaracing_master" / "core" / "resources" / "startup" / "pipeline.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_startup_advance_is_gated() -> None:
    graph = _load(STARTUP)
    nxt = graph["startup.boot"]["next"]
    assert "startup.advance.gate" in nxt
    assert "startup.advance" not in nxt, "未知页 OCR 不能直接挂在汇聚节点每轮候选里"
    gate = graph["startup.advance.gate"]
    # 兜底未命中必须能回到汇聚节点：只写 advance 一条会让门等超时后把整张图带死。
    assert gate["next"] == ["startup.advance", "startup.boot"]
    assert gate["on_error"] == ["startup.boot"]


def test_startup_gate_waits_and_timeout_covers_ocr() -> None:
    gate = _load(STARTUP)["startup.advance.gate"]
    assert gate["recognition"]["type"] == "DirectHit"
    assert gate["post_delay"] >= 2000, "节流窗口太短，OCR 仍会接近全速"
    assert gate["timeout"] >= 3000, "超时必须覆盖一次整帧/半屏 OCR 的耗时"
