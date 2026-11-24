# -*- coding: utf-8 -*-
"""稳定性配置的静态回归锁：未知页 OCR 节流 + 落地确认连续命中。

两条都不是模板匹配实现，而是 pipeline 结构：
- 启动链的未知页文字兜底必须经节流门，不能直接挂在汇聚节点每轮候选里；
- 一步到位的落地确认锚点必须带 `pre_wait_freezes`（原生连续命中）。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STARTUP = REPO / "maaracing_master" / "core" / "resources" / "startup" / "pipeline.json"
INFINITE = (REPO / "maaracing_master" / "plugins" / "infinite_drive"
            / "resources" / "pipeline" / "infinite_drive.json")

STEP_CONFIRMS = [
    "infinite_drive.__confirm.比赛页",
    "infinite_drive.__confirm.娱乐玩法页",
    "infinite_drive.__confirm.模式页",
    "infinite_drive.__confirm.准备页",
    "infinite_drive.__confirm.卡牌选择",
    "infinite_drive.__confirm.结算页",
    "infinite_drive.__confirm.放弃确认框",
    "infinite_drive.__confirm.选车页",
    "infinite_drive.__confirm.设置页",
    "infinite_drive.__confirm.退出确认框",
]


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


def _freeze_ms(node: dict) -> int:
    value = node.get("pre_wait_freezes", 0)
    return int(value.get("time", 0)) if isinstance(value, dict) else int(value)


def test_step_confirm_anchors_require_consecutive_hits() -> None:
    graph = _load(INFINITE)
    for name in STEP_CONFIRMS:
        assert _freeze_ms(graph[name]) > 0, f"{name} 缺连续命中窗口"
        value = graph[name]["pre_wait_freezes"]
        if isinstance(value, dict):
            assert value.get("timeout", 0) > 0, f"{name} 的等待窗口没有总上限"


def test_poll_only_anchor_stays_fast() -> None:
    """事件介绍是等待循环里的驱赶触发，逐拍轮询，不加窗口拖慢循环。"""
    graph = _load(INFINITE)
    assert "pre_wait_freezes" not in graph["infinite_drive.__confirm.事件介绍"]
