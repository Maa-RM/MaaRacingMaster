# -*- coding: utf-8 -*-
"""性能卡「零前端改动」的静态锁（契约 §7 验收第 3、4 条）。

**为什么是静态锁**：前端没有 DOM 测试设施，但「模板是否又按模块分支」这件事
可以直接从源码文本判定。价值在于：有人再往模板里写 `mid === 'speedrush'` 时，
CI 立刻红，而不是等到下一个模块上线才发现卡片是空的。

**局限**：只能证明写法没退回旧样式，不能证明运行时渲染正确——后者由
`tests/test_perf_contract.py`（后端产出）与真机跑一局共同覆盖。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETTINGS_JS = (REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"
               / "js" / "settings.js")
RUN_JS = (REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "run.js")


def _slice_fn(src: str, header: str) -> str:
    """截出某个函数体（从 header 到下一个同缩进的 `function `）。"""
    start = src.index(header)
    m = re.search(r"\n  function ", src[start + len(header):])
    assert m, f"抽取失败：{header} 之后找不到下一个函数"
    return src[start:start + len(header) + m.start()]


def test_perf_card_template_has_no_module_branch():
    """模板不得认识任何模块：新增模块只声明行，前端零改动。"""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = _slice_fn(src, "function perfCard(mid) {")
    assert "perf-card" in body, "抽取到的不是性能卡模板"

    # 模块 id 比较 / 模块名出现在模板里，都是「前端认识模块」的证据
    assert "mid ===" not in body and "mid ==" not in body, (
        "性能卡模板出现模块 id 分支：新增模块会落到错误的卡片上")
    for module_id in ("speedrush", "treasure"):
        assert f"'{module_id}'" not in body and f'"{module_id}"' not in body, (
            f"性能卡模板里硬编码了模块名 {module_id}")

    # 三套旧模板必须彻底消失（留着就是第四套/第五套并存）
    for legacy in ("perfCardLegacy", "speedrushPerfCard", "treasurePerfCard"):
        assert legacy not in src, f"旧模板 {legacy} 仍存在"


def test_module_rows_come_from_backend():
    """模块行由后端下发（perf.rows）驱动，前端不自己算值、不自己判级。"""
    src = RUN_JS.read_text(encoding="utf-8")
    body = _slice_fn(src, "function renderPerfRows(mid, rows) {")
    assert "row.display" in body, "模块行应直接使用后端格式化后的 display"
    assert "row.label" in body, "模块行标签应来自声明"
    # 前端不得再对模块行做单位换算或阈值判级
    assert "次/秒" not in body and "1000 /" not in body, (
        "前端仍在计算模块指标的显示值：格式化应只在 core 一处产生")
    assert "warn_at" not in body and "error_at" not in body, (
        "前端在按阈值判级：判级权应只在 core")


def test_perf_health_exempt_not_reported_from_error():
    """健康度聚合不得把 not_reported（本轮该项不适用）点亮成异常。

    契约的缺失原因四值里只有 invalid / out_of_range / read_error 是「监控真没
    取到值」；not_reported 是模块的正常缺报（如导航阶段还没有驾驶拍），聚合时
    按行自带等级（idle）参与——任何缺失一律判 error 会让每局开跑前的正常等待
    都显示「异常」。
    """
    src = RUN_JS.read_text(encoding="utf-8")
    body = _slice_fn(src, "function renderPerfHealth(mid, rows) {")
    assert "PERF_LEVEL_RANK" in body, "抽取到的不是健康度聚合"
    assert "row.reason ? 'error'" not in body, (
        "健康度聚合把所有缺失原因一律判为异常：正常缺报（not_reported）"
        "会被点亮成异常灯")
    assert "not_reported" in body, (
        "健康度聚合应显式豁免 not_reported，其余缺失原因照常参与异常聚合")
