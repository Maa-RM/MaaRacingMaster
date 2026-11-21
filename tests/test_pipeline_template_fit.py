# -*- coding: utf-8 -*-
"""truth 模板容纳性机检（template_fit_checks）的回归锁。

实证缺陷（2026-11-21 真机）：rect 归一化 4 位小数在 720p 上取整截断，9 个节点的
搜索区比模板小 1px 或零边距——模板在窗内凑不出来，0.995 分的按钮只剩 0.59 分，
运行期表现为「按钮明明在却永远识别未命中」。本闸让这类数据进不了仓库。
"""

from __future__ import annotations

import cv2
import numpy as np

from tools.pipeline.truth import template_fit_checks

TPL_NAME = "t.png"


def _write_template(root, w: int = 60, h: int = 20) -> None:
    img_dir = root / "resources" / "image"
    img_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(img_dir / TPL_NAME), np.zeros((h, w, 3), np.uint8))


def _pipeline_file(root):
    p = root / "resources" / "pipeline" / "p.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _node(rect: list[float]) -> dict:
    return {"recognition": {"type": "Custom",
                            "param": {"custom_recognition": "MaaRM_Template",
                                      "custom_recognition_param": {
                                          "mode": "template",
                                          "templates": [TPL_NAME],
                                          "rect": rect}}},
            "action": {"type": "DoNothing", "param": {}}}


def _origin(root) -> dict:
    return {"t.a": _pipeline_file(root)}


def test_clipped_rect_is_rejected(tmp_path) -> None:
    """搜索区比模板小 1px（截断缺陷的原样）必须报错。"""
    _write_template(tmp_path, w=60, h=20)
    # rect 尺寸 61x21px：宽 61 < 60+2*2，高 21 < 20+2*2，两侧都越界
    graph = {"t.a": _node([0.5, 0.5, 0.5 + 61 / 1280, 0.5 + 21 / 720])}
    errs = template_fit_checks(graph, _origin(tmp_path))
    assert len(errs) == 1 and "未以" in errs[0] and "t.a.recognition" in errs[0]


def test_zero_margin_rect_is_rejected(tmp_path) -> None:
    """恰好等于模板尺寸（零边距）同样过不了闸——1px 漂移零容忍。"""
    _write_template(tmp_path, w=60, h=20)
    graph = {"t.a": _node([0.5, 0.5, 0.5 + 60 / 1280, 0.5 + 20 / 720])}
    assert template_fit_checks(graph, _origin(tmp_path))


def test_fitting_rect_passes(tmp_path) -> None:
    _write_template(tmp_path, w=60, h=20)
    graph = {"t.a": _node([0.4, 0.4, 0.4 + 80 / 1280, 0.4 + 30 / 720])}
    assert template_fit_checks(graph, _origin(tmp_path)) == []


def test_settle_rect_checked_too(tmp_path) -> None:
    """settle 规格与识别同源复制，同缺陷同闸。"""
    _write_template(tmp_path, w=60, h=20)
    graph = {"t.a": {
        "recognition": {"type": "Custom",
                        "param": {"custom_recognition": "MaaRM_Template",
                                  "custom_recognition_param": {
                                      "mode": "template",
                                      "templates": [TPL_NAME],
                                      "rect": [0.4, 0.4, 0.5, 0.5]}}},
        "action": {"type": "Custom",
                   "param": {"custom_action": "MaaRM_Click",
                             "custom_action_param": {"settle": {
                                 "templates": [TPL_NAME],
                                 "rect": [0.5, 0.5, 0.5 + 61 / 1280,
                                          0.5 + 21 / 720]}}}},
    }}
    errs = template_fit_checks(graph, _origin(tmp_path))
    assert len(errs) == 1 and "t.a.settle" in errs[0]


def test_missing_template_is_rejected(tmp_path) -> None:
    graph = {"t.a": _node([0.4, 0.4, 0.5, 0.5])}
    errs = template_fit_checks(graph, _origin(tmp_path))
    assert len(errs) == 1 and "不存在" in errs[0]
