# -*- coding: utf-8 -*-
"""`MaaRM_Advance` 的区域裁剪与坐标映射回归锁。

裁剪是 CPU 治理的主要收益：只把 `zone` 裁出来送文本检测，命中框再映射回整帧
归一化。坐标语义一旦错位，点击会落到错误位置，所以这里锁三件事：
检测输入只含区域、映射后的框与整帧口径一致、退化区域回落到整帧。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import numpy as np

from maaracing_master.core import advance
from maaracing_master.core.ocr import OcrBlock

W, H = 1280, 720


class _FakeEngine:
    def __init__(self, blocks: list[OcrBlock]) -> None:
        self.blocks = blocks
        self.seen: list[tuple[int, ...]] = []

    def recognize_blocks(self, img, *, min_score=0.5):
        self.seen.append(img.shape)
        return self.blocks


def _argv(param: dict | None = None):
    a = MagicMock()
    a.image = np.zeros((H, W, 3), dtype=np.uint8)   # 注入帧为 BGR
    a.custom_recognition_param = json.dumps(param or {})
    a.node_name = "advance"
    return a


def _graph():
    g = MagicMock()
    g.frame = MagicMock(return_value=None)
    g._log = MagicMock()
    g._publish_nav = MagicMock()
    return g


def test_detects_only_zone_and_maps_box_back(monkeypatch) -> None:
    """默认 zone = 下半屏：检测输入高 396，框映射回整帧后 y 带 0.45 偏移。"""
    monkeypatch.setattr(advance, "_ENGINE",
                        _FakeEngine([OcrBlock("继续", (0.2, 0.1, 0.4, 0.2))]))
    rec = advance.AdvanceRecognizer(_graph())

    res = rec.analyze(None, _argv())

    assert advance._ENGINE.seen == [(396, W, 3)]
    assert res.box is not None
    bx, by, bw, bh = res.box
    assert abs(bx - 0.2 * W) < 1
    assert abs(by - (0.45 + 0.1 * 0.55) * H) < 1
    assert abs(bw - 0.2 * W) < 1
    assert abs(bh - 0.1 * 0.55 * H) < 1
    assert res.detail["word"] == "继续"


def test_degenerate_zone_falls_back_to_full_frame(monkeypatch) -> None:
    """区域裁到小于最小边长时退回整帧；zone 过滤仍照常生效。"""
    monkeypatch.setattr(advance, "_ENGINE",
                        _FakeEngine([OcrBlock("继续", (0.1, 0.2, 0.2, 0.3))]))
    rec = advance.AdvanceRecognizer(_graph())

    res = rec.analyze(None, _argv({"zone": [0.5, 0.5, 0.5001, 0.5001]}))

    assert advance._ENGINE.seen == [(H, W, 3)]      # 检测输入退回整帧
    assert res.box is None                         # 但块在 zone 外，仍不点


def test_inverted_zone_is_normalized(monkeypatch) -> None:
    """zone 左上/右下写反也能用（参数容错，不静默失效）。"""
    monkeypatch.setattr(advance, "_ENGINE",
                        _FakeEngine([OcrBlock("继续", (0.1, 0.1, 0.2, 0.2))]))
    rec = advance.AdvanceRecognizer(_graph())

    res = rec.analyze(None, _argv({"zone": [1.0, 1.0, 0.0, 0.0]}))

    assert advance._ENGINE.seen == [(H, W, 3)]
    assert res.box is not None
