#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用推进器：未知页面上「下一步在哪」的文字兜底（2026-11-12，赛季更新触发）。

**要解决的失败模式**：导航图是「一页一模板一节点」的枚举——每来一个新弹窗，就要
裁图、标定 ROI、加节点。赛季更新一次冒出七个弹窗（维护公告、段位结算、赛季奖励、
分数继承、赛季速览、月卡、新车排位分），五个既有模板全部落空，进大厅那一步耗到
300 秒上限才放弃。枚举法在「版本一次改一批界面」面前结构性地追不上。

**为什么靠文字而不是靠模板**：这批弹窗有六七种皮肤，字却只有那几组——继续、进入
新赛季、领取奖励、点击屏幕继续。皮肤随版本换，文案相对稳定。所以兜底判据取文案，
取到框就点框中心。

**为什么不能用 `RapidOcrEngine.recognize`（HUD 那条路）**：那个接口关着文本检测，
只回答「这块固定区域里写着什么」，先验必须已经知道字在哪。未知页面的先验恰恰是
没有的——位置本身就是要找的东西。所以走 `recognize_blocks`（开 det 拿框），
代价 1s/帧，只在「所有已知页面都不匹配」时才付。

**误点防护**（这是本识别器唯一的实质风险，三条都必须留着）：
  1. 位置先验 `zone`：默认只在下半屏找。推进按钮在游戏 UI 里恒在下半屏；顶部 HUD
     的「领取 / 任务 / 活动」不在候选范围，避免在大厅上误点。
  2. 文案白名单 + 长度上限：按钮文案短（≤8 字）。长段落即使含「继续」也不点。
  3. 黑名单：购买、消耗、钻石、点券、取消、分享……命中即整块淘汰。宁可这一页过不去，
     也不能点掉一次购买或分享。

**边界**：本识别器只负责"找到了就点那一个框"，不做连锁点击、不猜下一步；点完由节点
真源回入口重判。无字控件（关闭叉、返回箭头）不归它管——OCR 认不出图形，那类仍走模板。

节点参数（全部可选，缺省取模块常量）：
    words       ["继续", "进入", ...]   白名单文案（含即算命中，按表序优先）
    blocklist   ["购买", ...]           黑名单文案（含即淘汰）
    zone        [x1,y1,x2,y2]           归一化搜索区，缺省下半屏
    max_chars   8                       文案长度上限
    min_area    0.0008                  文字块面积下限（归一化，防点角落小字）
    min_score   0.5                     文字置信度下限
"""
from __future__ import annotations

import json
import re

import cv2
from maa.custom_recognition import CustomRecognition

from maaracing_master.core.ocr import RapidOcrEngine

RECOGNIZER_NAME = "MaaRM_Advance"

DEFAULT_WORDS: tuple[str, ...] = (
    "继续", "进入", "领取", "确定", "确认", "下一步", "点击屏幕", "点击空白", "跳过",
)
DEFAULT_BLOCKLIST: tuple[str, ...] = (
    "购买", "消耗", "钻石", "点券", "取消", "退出", "删除", "分享", "转发", "发送", "订阅", "充值",
)
DEFAULT_ZONE: tuple[float, float, float, float] = (0.0, 0.45, 1.0, 1.0)
MAX_TEXT_CHARS = 8
MIN_TEXT_AREA = 0.0008

# 引擎进程内单例：det 模型常驻几十 MB，每张图各建一份没有收益。
_ENGINE: RapidOcrEngine | None = None


def _engine() -> RapidOcrEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOcrEngine()
    return _ENGINE


def _parse(raw) -> dict:
    try:
        val = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


_PUNCT = re.compile(r"[\s，。、,\.:：!！?？\-—_·…（）()\[\]【】\"'“”‘’/\\|]+")


def normalize_text(text: str) -> str:
    """去掉空白与标点，只留实义字。

    OCR 会在汉字之间插空格、把「继续」读成全角带标点的串，比对前必须归一，
    否则「继 续」与「继续」会被当成两个词。
    """
    return _PUNCT.sub("", text or "")


def pick_block(blocks, *, words, blocklist, zone, max_chars, min_area):
    """在文字块里挑出「该点的那一个」。返回 (block, 命中的白名单词) 或 None。

    抽成纯函数是为了让判定规则可以被单测锁住——误点防护全在这几条筛子上，
    它们退化成「有字就点」之前，必须先有测试红。
    """
    words = [normalize_text(w) for w in words]
    blocklist = [normalize_text(w) for w in blocklist]
    best = None
    for block in blocks:
        text = normalize_text(block.text)
        if not text or len(text) > max_chars:
            continue
        if any(bad and bad in text for bad in blocklist):
            continue
        rank = next((i for i, w in enumerate(words) if w and w in text), None)
        if rank is None:
            continue
        x1, y1, x2, y2 = block.rect
        if x1 < zone[0] or y1 < zone[1] or x2 > zone[2] or y2 > zone[3]:
            continue
        area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        if area < min_area:
            continue
        key = (rank, -area)
        if best is None or key < best[0]:
            best = (key, block, words[rank])
    return None if best is None else (best[1], best[2])


class AdvanceRecognizer(CustomRecognition):
    """`MaaRM_Advance` 的实现：整帧找文字块 → 白/黑名单筛选 → 交出命中框。"""

    def __init__(self, graph) -> None:
        super().__init__()
        self._graph = graph
        self._miss_edges: set[str] = set()

    def analyze(self, context, argv):
        p = _parse(argv.custom_recognition_param)
        frame = getattr(argv, "image", None)
        if frame is None:
            frame = self._graph.frame()
        if frame is None:
            return self.AnalyzeResult(box=None, detail={"error": "截图失败"})
        # 注入帧为 BGR（框架契约），OCR 引擎入口收 RGB（见 core/image_io 通道序）
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        H, W = rgb.shape[:2]
        if H <= 0 or W <= 0:
            return self.AnalyzeResult(box=None, detail={"error": "帧尺寸非法"})

        blocks = _engine().recognize_blocks(rgb,
                                            min_score=float(p.get("min_score", 0.5)))
        if blocks is None:
            # 引擎不可用与「画面没字」必须分开：前者要告警一次，后者只是没命中。
            name = argv.node_name
            if name not in self._miss_edges:
                self._miss_edges.add(name)
                self._graph._log(f"[v4] 「{name}」文字推进不可用（OCR 引擎未就绪），"
                                 "本次不兜底", "WARNING")
            return self.AnalyzeResult(box=None, detail={"error": "ocr_unavailable"})

        words = [normalize_text(w) for w in (p.get("words") or DEFAULT_WORDS)]
        blocklist = [normalize_text(w) for w in (p.get("blocklist") or DEFAULT_BLOCKLIST)]
        zone = tuple(p.get("zone") or DEFAULT_ZONE)
        max_chars = int(p.get("max_chars") or MAX_TEXT_CHARS)
        min_area = float(p.get("min_area") or MIN_TEXT_AREA)

        hit = pick_block(blocks, words=words, blocklist=blocklist, zone=zone,
                         max_chars=max_chars, min_area=min_area)

        name = argv.node_name
        if hit is None:
            if name not in self._miss_edges:
                self._miss_edges.add(name)
                self._graph._log(f"[v4] 「{name}」文字推进未命中（检出 {len(blocks)} 块文字，"
                                 "无白名单文案）", "DEBUG")
            return self.AnalyzeResult(box=None, detail={"blocks": len(blocks)})
        self._miss_edges.discard(name)

        block, word = hit
        x1, y1, x2, y2 = block.rect
        box = (x1 * W, y1 * H, (x2 - x1) * W, (y2 - y1) * H)
        self._graph._publish_nav(name, phase="hit", template=f"ocr:{word}",
                                 score=1.0, box=(x1, y1, x2, y2))
        self._graph._log(f"[v4] 「{name}」文字推进：命中「{block.text}」"
                         f"@({(x1 + x2) / 2:.2f},{(y1 + y2) / 2:.2f})，点它", "DEBUG")
        return self.AnalyzeResult(
            # MaaFW rect 契约 = (x, y, w, h)
            box=(box[0], box[1], box[2], box[3]),
            detail={"text": block.text, "word": word, "name": name})
