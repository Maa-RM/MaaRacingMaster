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
    # 返回箭头排在文字识别之后：OCR 认不出图形，但文字按钮更精准，先试它不让既有兜底降级。
    assert gate["next"] == ["startup.advance", "startup.popup.exit", "startup.boot"]
    assert gate["on_error"] == ["startup.boot"]


def test_startup_exit_controls_stay_out_of_main_candidates() -> None:
    """退出控件（返回箭头 / 主页按钮）只能从兜底门出手，搜索区限左上角控件区。

    它们不进主候选表的原因：大厅、活动页与比赛页每轮都会评估候选，误命中就变成
    日常误点；走兜底门只在「已知页面全部落空」时出手。
    """
    graph = _load(STARTUP)
    exit_node = graph["startup.popup.exit"]
    for name in ("startup.boot", "startup.hall.confirm.1",
                 "startup.hall.confirm.2", "startup.hall.confirm.3"):
        assert "startup.popup.exit" not in graph[name]["next"], name
    assert exit_node["next"] == ["startup.boot"]
    assert exit_node["on_error"] == ["startup.boot"]

    param = exit_node["recognition"]["param"]["custom_recognition_param"]
    assert param["templates"] == ["common_back.png", "common_home.png"]
    rect = param["rect"]
    assert rect[2] <= 0.15 and rect[3] <= 0.11, f"搜索区越出左上控件区: {rect}"
    # 两张模板都是紧裁的本体（跨页同分），门槛按弹窗模板的通用口径，别为兼容旧图放宽
    assert param["threshold"] >= 0.85


def test_startup_gate_waits_and_timeout_covers_ocr() -> None:
    gate = _load(STARTUP)["startup.advance.gate"]
    assert gate["recognition"]["type"] == "DirectHit"
    # 节流窗口不是越长越好：未知页每轮还要付候选扫描（~600ms）与半屏 OCR（~500ms），
    # 1.2 秒间隔下 OCR 占空比约两成，仍远低于「每轮全速」那条烧 CPU 的线。
    assert gate["post_delay"] >= 1000, "节流窗口太短，OCR 会接近全速"
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
