# -*- coding: utf-8 -*-
"""通用推进器的判定规则（`core/advance.py`）。

看护的是**误点防护**，不是识别率：这些筛子一旦退化成「有字就点」，后果是自动流程
去点购买、分享、退出之类的按钮。所以每条筛子都要有一条红得起来的用例。

识别率本身由真机帧验证（2026-11-12 赛季弹窗七帧），不在此处伪造。
"""
from __future__ import annotations

from maaracing_master.core.advance import (
    DEFAULT_BLOCKLIST,
    DEFAULT_WORDS,
    DEFAULT_ZONE,
    MAX_TEXT_CHARS,
    MIN_TEXT_AREA,
    normalize_text,
    pick_block,
)
from maaracing_master.core.ocr import OcrBlock


def _block(text, rect):
    return OcrBlock(text=text, rect=rect)


def _pick(blocks, **kw):
    args = dict(words=DEFAULT_WORDS, blocklist=DEFAULT_BLOCKLIST,
                zone=DEFAULT_ZONE, max_chars=MAX_TEXT_CHARS, min_area=MIN_TEXT_AREA)
    args.update(kw)
    return pick_block(blocks, **args)


def test_normalize_strips_spaces_and_punctuation():
    assert normalize_text("继 续") == "继续"
    assert normalize_text("继续。") == "继续"
    assert normalize_text(" 点击屏幕继续 ") == "点击屏幕继续"


def test_hits_whitelist_word_lower_half():
    blocks = [_block("继续", (0.85, 0.90, 0.92, 0.95))]
    hit = _pick(blocks)
    assert hit is not None
    assert hit[0].text == "继续"


def test_blocklist_wins_over_whitelist():
    # 「确认购买」同时含白名单「确认」与黑名单「购买」——必须先被黑名单淘汰
    blocks = [_block("确认购买", (0.6, 0.8, 0.8, 0.86))]
    assert _pick(blocks) is None


def test_long_paragraph_is_not_clicked():
    long_text = "点击继续以进入下一赛季并领取全部奖励"
    assert len(long_text) > MAX_TEXT_CHARS
    assert _pick([_block(long_text, (0.3, 0.6, 0.7, 0.65))]) is None


def test_top_hud_is_out_of_zone():
    # 顶部 HUD 的「领取」不在下半屏，不得成为候选（否则大厅上会误点任务入口）
    blocks = [_block("领取", (0.75, 0.10, 0.80, 0.14))]
    assert _pick(blocks) is None


def test_word_order_beats_area():
    small = _block("继续", (0.85, 0.90, 0.90, 0.94))
    big = _block("领取奖励", (0.30, 0.60, 0.45, 0.68))
    hit = _pick([big, small])
    assert hit is not None and hit[0].text == "继续"


def test_same_word_prefers_bigger_block():
    small = _block("继续", (0.86, 0.91, 0.88, 0.93))
    big = _block("继续", (0.55, 0.90, 0.70, 0.96))
    hit = _pick([small, big])
    assert hit is not None and hit[0].rect == big.rect


def test_tiny_text_block_ignored():
    tiny = _block("继续", (0.60, 0.90, 0.605, 0.905))   # 面积远小于 min_area
    assert _pick([tiny]) is None


def test_empty_text_ignored():
    assert _pick([_block("   ", (0.5, 0.9, 0.6, 0.95))]) is None
