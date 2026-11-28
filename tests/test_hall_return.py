# -*- coding: utf-8 -*-
"""崩溃层退路（`core.hall_return`）：退出类原语循环的边界与安全性。

它只在大厅锚点判不出来之后才进入，所以测试锁的是三件事：到了大厅立刻收手、
有可点的退出控件才点、点和判都有上限（不能在没有出口的页面上无限点）。
"""
from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from maaracing_master.core import hall_return as hr
from maaracing_master.core import template_match as tm

W, H = 1280, 720
ANCHOR = "module.hall.anchor"


def _checker() -> np.ndarray:
    """带纹理的小模板；纯色块方差为零，NCC 在平坦区域会给出无意义的高分。"""
    tile = np.zeros((10, 20, 3), np.uint8)
    for r in range(2):
        for c in range(4):
            tile[r * 5:(r + 1) * 5, c * 5:(c + 1) * 5] = (
                (255, 40, 40) if (r + c) % 2 == 0 else (30, 30, 200))
    return tile


@pytest.fixture()
def tpl_dir(tmp_path, monkeypatch):
    """有纹理的返回箭头替代图；缓存按模板名存，跨用例必须清空。"""
    cv2.imwrite(str(tmp_path / "common_back.png"), cv2.cvtColor(_checker(), cv2.COLOR_RGB2BGR))
    monkeypatch.setattr(tm, "_cache", {})
    return tmp_path


def _frame_with_back(tpl_dir) -> np.ndarray:
    """黑帧，左上角贴一块与模板相同的图案（落在返回箭头 ROI 内）。"""
    frame = np.zeros((H, W, 3), np.uint8)
    frame[10:20, 30:50] = _checker()
    return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)  # graph.frame() 契约是 BGR


class _Graph:
    def __init__(self, tpl_dir, frames, outcomes, running=True):
        self.image_dirs = [tpl_dir]
        self._frames = list(frames)
        self._outcomes = list(outcomes)
        self.clicks: list[tuple[float, float]] = []
        self.probes = 0
        self.ctx = SimpleNamespace(lifecycle=SimpleNamespace(running=running))

    def probe(self, node):
        self.probes += 1
        return self._outcomes.pop(0) if self._outcomes else "miss"

    def frame(self):
        return self._frames.pop(0) if len(self._frames) > 1 else self._frames[0]

    def click(self, cx, cy, box, timeout_s):
        self.clicks.append((cx, cy))
        return True


def test_hall_already_here_returns_immediately(tpl_dir) -> None:
    graph = _Graph(tpl_dir, [np.zeros((H, W, 3), np.uint8)], ["hit"])
    assert hr.return_to_hall(graph, ANCHOR) is True
    assert graph.clicks == [] and graph.probes == 1


def test_clicks_back_arrow_then_reports_hall(tpl_dir) -> None:
    graph = _Graph(tpl_dir, [_frame_with_back(tpl_dir)], ["miss", "hit"])
    assert hr.return_to_hall(graph, ANCHOR) is True
    assert len(graph.clicks) == 1
    cx, cy = graph.clicks[0]
    assert cx == pytest.approx(40 / W, abs=1e-3) and cy == pytest.approx(15 / H, abs=1e-3)


def test_no_exit_control_is_bounded(tpl_dir) -> None:
    """一个退出控件都没有时必须空转有界，不能无限点。"""
    blank = cv2.cvtColor(np.zeros((H, W, 3), np.uint8), cv2.COLOR_RGB2BGR)
    graph = _Graph(tpl_dir, [blank], ["miss"] * 10)
    assert hr.return_to_hall(graph, ANCHOR, max_rounds=3, deadline_s=30.0) is False
    assert graph.clicks == []
    assert graph.probes == 4  # 3 轮各判一次 + 末尾复核一次


def test_stop_signal_aborts_without_clicking(tpl_dir) -> None:
    graph = _Graph(tpl_dir, [_frame_with_back(tpl_dir)], ["miss", "hit"], running=False)
    assert hr.return_to_hall(graph, ANCHOR) is False
    assert graph.clicks == []
