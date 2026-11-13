# -*- coding: utf-8 -*-
"""感知评测脚本的度量回归锁：匹配规则、AP、标注解析。

这些函数决定所有模型对比的数字，写错会安静地给出错误结论——锁住最小可证性质：
一对一匹配、完美检测 AP=1、无检出 AP=0、无 GT 返回 nan、标注坐标换算。
"""

from __future__ import annotations

import numpy as np
import pytest

try:
    from maaracing_master.plugins.speedrush.tools.training.eval_detector import (
        average_precision,
        load_labels,
        match_flags,
    )
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺 onnxruntime 的机器整文件 SKIP
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"eval_detector 导入失败: {_ERR}")


class TestMatchFlags:
    def test_one_to_one_by_iou(self):
        gts = [(0.0, 0.0, 10.0, 10.0), (20.0, 0.0, 30.0, 10.0)]
        dets = [
            {"box": (0.0, 0.0, 10.0, 10.0), "confidence": 0.9},
            {"box": (0.0, 0.0, 10.0, 10.0), "confidence": 0.8},   # 同一 GT 的重复框
            {"box": (100.0, 100.0, 110.0, 110.0), "confidence": 0.7},
        ]
        assert match_flags(dets, gts, 0.5) == [True, False, False]

    def test_iou_threshold_respected(self):
        gts = [(0.0, 0.0, 10.0, 10.0)]
        dets = [{"box": (0.0, 0.0, 10.0, 5.0), "confidence": 0.9}]   # IoU = 0.5
        assert match_flags(dets, gts, 0.51) == [False]
        assert match_flags(dets, gts, 0.5) == [True]


class TestAveragePrecision:
    def test_perfect_detector(self):
        assert average_precision([(0.9, True), (0.8, True)], 2) == 1.0

    def test_no_detections(self):
        assert average_precision([], 2) == 0.0

    def test_all_false_positive(self):
        assert average_precision([(0.9, False)], 1) == 0.0

    def test_no_ground_truth(self):
        assert np.isnan(average_precision([(0.5, True)], 0))


class TestLoadLabels:
    def test_normalized_to_pixels(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("0 0.5 0.5 0.2 0.1\n1 0.6 0.2 0.4 0.5\n", encoding="utf-8")
        labels = load_labels(path, 1000, 500)
        assert labels[0][0] == 0
        assert labels[0][1] == pytest.approx((400.0, 225.0, 600.0, 275.0))

    def test_missing_file_is_empty(self, tmp_path):
        assert load_labels(tmp_path / "none.txt", 100, 100) == []
