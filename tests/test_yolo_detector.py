# -*- coding: utf-8 -*-
"""yolo_detector 类别参数化的回归锁（阶段 B 前置改造）。

锁住三件容易错的事：
1. **类别表来源**——默认从模型 ONNX 元数据（Ultralytics 导出的 names）读取，
   core 不再内置任何游戏的类别名；classes 参数可显式覆盖；
2. **class_conf 按键校验**——按类别名给阈值，含未知类名立即报错（防止静默无效）；
3. **返回形状契约**——(by_class, detections, all_raw_dets) 三元组，
   by_class 对每个已声明类别都有键（空结果为空列表，调用方不必判缺键）。

_to_dets 的分组与跳过逻辑用裸实例直测（不经 session）；
构造/元数据测试需要 archive 内的 racing 模型与 onnxruntime，缺则整文件跳过。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

try:
    import cv2
except Exception:  # noqa: BLE001 —— 与 yolo_detector 同一依赖集
    cv2 = None

try:
    from maaracing_master.core.yolo_detector import YOLODetector
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— 缺 onnxruntime 的机器上整文件 SKIP
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"yolo_detector 导入失败: {_ERR}")

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "archive" / "racing" / "resources" / "onnx" / "model.onnx"
PLUGIN_MODEL = (ROOT / "maaracing_master" / "plugins" / "speedrush" / "resources"
                / "models" / "perception" / "model.onnx")

needs_model = pytest.mark.skipif(
    not MODEL.exists(), reason="racing 归档模型不在树内（或资源目录未部署）")
needs_plugin_model = pytest.mark.skipif(
    not PLUGIN_MODEL.exists(), reason="speedrush 感知模型不在树内")


def _bare_detector(classes: dict[int, str]) -> YOLODetector:
    """绕过 __init__（不加载 session）构造只够 _to_dets 用的实例"""
    det = YOLODetector.__new__(YOLODetector)
    det.classes = classes
    return det


class TestToDets:
    def test_groups_by_class_name(self):
        det = _bare_detector({0: "coin", 1: "car", 2: "bonus_car"})
        xyxy = np.array([[10.0, 20.0, 30.0, 40.0], [0.0, 0.0, 50.0, 50.0]])
        scores = np.array([0.9, 0.8])
        cls = np.array([0, 2])
        by_class, dets = det._to_dets(
            xyxy, scores, cls, 0, 0, 1.0, 0, 0, 100, 100, [0, 1])
        assert by_class == {"coin": [(20, 30, 20, 20)], "car": [],
                            "bonus_car": [(25, 25, 50, 50)]}
        assert [d["class_name"] for d in dets] == ["coin", "bonus_car"]
        assert dets[0]["box"] == (10, 20, 30, 40)
        assert dets[0]["confidence"] == pytest.approx(0.9)

    def test_unknown_class_id_skipped(self):
        """模型输出类别维多于声明类别：未声明 id 不进任何结果"""
        det = _bare_detector({0: "coin"})
        xyxy = np.array([[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]])
        scores = np.array([0.9, 0.9])
        cls = np.array([0, 5])
        by_class, dets = det._to_dets(
            xyxy, scores, cls, 0, 0, 1.0, 0, 0, 100, 100, [0, 1])
        assert by_class == {"coin": [(2, 3, 2, 2)]}
        assert len(dets) == 1

    def test_min_score_filters(self):
        det = _bare_detector({0: "coin", 1: "car"})
        xyxy = np.array([[0.0, 0.0, 10.0, 10.0], [0.0, 0.0, 4.0, 4.0]])
        scores = np.array([0.9, 0.3])
        cls = np.array([0, 1])
        by_class, dets = det._to_dets(
            xyxy, scores, cls, 0, 0, 1.0, 0, 0, 100, 100, [0, 1], min_score=0.5)
        assert by_class["car"] == []
        assert len(dets) == 1


# onnxruntime-directml（1.24.4）原生缺陷：任一 session 析构后再对另一 session
# run() 会段错误（CPU provider 无此问题，多 session 全存活亦无）。测试必然创建
# 多个 detector，故全部保活至进程结束，杜绝 run 之前的任何析构。
_KEEP_ALIVE: list = []


@pytest.mark.local_asset
@needs_model
class TestModelDrivenContract:
    @pytest.fixture(scope="module")
    def detector(self):
        det = YOLODetector(str(MODEL), conf=0.4)
        _KEEP_ALIVE.append(det)
        return det

    def test_classes_from_metadata(self, detector):
        assert detector.classes == {0: "coin", 1: "car", 2: "bonus_car"}
        assert detector._conf_by_id == {}

    def test_class_conf_keyed_by_name(self):
        det = YOLODetector(str(MODEL), conf=0.5, class_conf={"coin": 0.2})
        _KEEP_ALIVE.append(det)
        assert det._conf_by_id == {0: 0.2}

    def test_class_conf_unknown_name_raises(self):
        # 显式 classes 时校验在模型加载前 fail-fast：不产生 session，无析构风险
        with pytest.raises(ValueError, match="未知类别名"):
            YOLODetector(str(MODEL), classes=["coin", "car", "bonus_car"],
                         class_conf={"nope": 0.1})

    def test_empty_classes_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            YOLODetector(str(MODEL), classes={})

    def test_explicit_classes_override(self):
        det = YOLODetector(str(MODEL), conf=0.4, classes=["a", "b", "c"])
        _KEEP_ALIVE.append(det)
        assert det.classes == {0: "a", 1: "b", 2: "c"}

    def test_call_returns_three_tuple(self, detector):
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        by_class, dets, raw = detector(img)
        assert set(by_class) == {"coin", "car", "bonus_car"}
        assert all(isinstance(v, list) for v in by_class.values())
        assert isinstance(dets, list) and isinstance(raw, list)


class TestNmsPerClass:
    """NMS 框格式回归锁：cv2.dnn.NMSBoxes 只认 [x,y,w,h]，喂 xyxy 会把
    x2/y2 当宽高算 IoU，横向错位不大的两辆真车互相误压（2026-10-07
    0068 帧实拍：conf 0.57 的车被 0.63 框误吞，两行复现定案）。"""

    def _run(self, xyxy: list[list[float]], scores: list[float]) -> list[int]:
        det = _bare_detector({0: "car"})
        det.iou = 0.45
        xyxy = np.asarray(xyxy, float)
        scores = np.asarray(scores, float)
        classes = np.zeros(len(scores), int)
        mask = np.ones(len(scores), bool)
        return det._nms_per_class(xyxy, scores, classes, mask,
                                  0, 0, 1.0, 0, 0, 1280, 720)

    def test_keeps_disjoint_nearby_boxes(self):
        # 0068 帧实例坐标：真框不重叠，误判格式下 IoU≈0.88 被误压
        keep = self._run([[596, 319, 613, 334], [624, 319, 637, 333]],
                         [0.63, 0.57])
        assert sorted(keep) == [0, 1]

    def test_still_collapses_duplicates(self):
        # 原本正常场景：同车近重复框仍须压到 1 个
        keep = self._run([[596, 319, 613, 334], [598, 319, 611, 334]],
                         [0.77, 0.75])
        assert len(keep) == 1


class TestDmlLock:
    """DML 互斥（core.dml_lock）：两个 DML 会话并发 run 会段错误杀进程
    （2026-09-25 实机 + 双线程复现），本会话的 run 必须在锁内。"""

    def test_call_holds_dml_lock_during_run(self):
        from types import SimpleNamespace

        from maaracing_master.core import dml_lock

        det = YOLODetector.__new__(YOLODetector)   # 裸实例：不加载真模型
        det.classes = {0: "coin"}
        det._conf_by_id = {0: 0.5}
        det.conf, det.iou, det.input_size = 0.5, 0.45, 640
        det._call_count = 0
        det._class_dim_checked = True

        class _LockedSess:
            def get_inputs(self):
                return [SimpleNamespace(name="images")]

            def run(self, _names, _feeds):
                assert dml_lock.LOCK.locked(), "session.run 期间必须持有 DML 锁"
                return [np.zeros((1, 5, 8400), np.float32)]   # 4 box + 1 cls，全零

        det.session = _LockedSess()
        det.input_name = "images"
        by_class, dets, raw = det(np.zeros((720, 1280, 3), dtype=np.uint8))
        assert by_class == {"coin": []} and dets == [] and raw == []


class TestLetterbox:
    """预处理缓冲复用：输出必须与旧实现逐位一致（省时间不能改数值）。"""

    @staticmethod
    def _reference(img: np.ndarray, size: int = 640):
        height, width = img.shape[:2]
        scale = min(size / height, size / width)
        new_h, new_w = int(height * scale), int(width * scale)
        pad_y, pad_x = (size - new_h) // 2, (size - new_w) // 2
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = cv2.resize(
            img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        return canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255.0, scale, pad_x, pad_y

    def test_bit_identical_to_reference(self):
        det = _bare_detector({0: "car"})
        det.input_size = 640
        rng = np.random.default_rng(0)
        for shape in ((720, 1280, 3), (1080, 1920, 3), (360, 640, 3), (64, 64, 3)):
            img = rng.integers(0, 256, shape, dtype=np.uint8)
            expected, scale, pad_x, pad_y = self._reference(img)
            got, got_scale, got_x, got_y = det._letterbox(img)
            assert (got_scale, got_x, got_y) == (scale, pad_x, pad_y)
            np.testing.assert_array_equal(got, expected)

    def test_reuses_buffer_and_resets_on_geometry_change(self):
        det = _bare_detector({0: "car"})
        det.input_size = 640
        rng = np.random.default_rng(1)
        big = rng.integers(0, 256, (720, 1280, 3), dtype=np.uint8)
        small = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
        first, _s, _x, _y = det._letterbox(big)
        second, _s, _x, _y = det._letterbox(big)
        assert second is first                      # 同几何复用同一块缓冲
        third, _s, _x, _y = det._letterbox(small)
        expected, _s, _x, _y = self._reference(small)
        np.testing.assert_array_equal(third, expected)   # 几何变化后不残留旧内容


class TestSessionOptions:
    """DirectML 会话配置锁（2026-11-09）。

    DML 官方要求顺序执行且关闭内存模式；**不能设 optimized_model_filepath**——
    设了之后运行期图融合失效，整图从 1 个融合节点退化为 239 个独立节点，
    前向实测 3.5→6.0 ms。融合是否生效由 tools/probe_infer.py 诊断，生产路径不跑
    profiler。
    """

    def test_dml_requirements_explicit(self):
        ort = pytest.importorskip("onnxruntime")
        from maaracing_master.core.yolo_detector import build_session_options

        opts = build_session_options()
        assert opts.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL
        assert opts.enable_mem_pattern is False
        assert opts.optimized_model_filepath == ""

    def test_fallback_order_is_dml_then_cpu(self, monkeypatch):
        """DML 创建失败时回退 CPU，且保留原始原因；不再尝试 CUDA。"""
        from types import SimpleNamespace

        import maaracing_master.core.yolo_detector as mod

        calls: list[list[str]] = []

        class _FakeSession:
            def __init__(self, _path, sess_options=None, providers=None):
                calls.append(list(providers or []))
                if providers == ["DmlExecutionProvider"]:
                    raise RuntimeError("DML init failed")

            def get_providers(self):
                return ["CPUExecutionProvider"]

            def get_inputs(self):
                return [SimpleNamespace(name="images")]

        monkeypatch.setattr(mod.ort, "InferenceSession", _FakeSession)
        det = mod.YOLODetector("dummy.onnx", classes=["car"])
        assert calls == [["DmlExecutionProvider"], ["CPUExecutionProvider"]]
        assert det.provider == "cpu"
        assert det.session_error and "DirectML" in det.session_error
        assert all("CUDA" not in name for call in calls for name in call)


@pytest.mark.local_asset
@needs_plugin_model
class TestSerializedCacheEquivalence:
    """写盘缓存选项的图与生产图数值一致：修复只改会话选项，不改推理结果。"""

    def test_outputs_identical(self, tmp_path):
        ort = pytest.importorskip("onnxruntime")
        from maaracing_master.core.yolo_detector import build_session_options

        plain = ort.InferenceSession(
            str(PLUGIN_MODEL), build_session_options(),
            providers=["DmlExecutionProvider", "CPUExecutionProvider"])
        _KEEP_ALIVE.append(plain)

        legacy_opts = build_session_options()
        legacy_opts.optimized_model_filepath = str(tmp_path / "legacy.onnx")
        legacy = ort.InferenceSession(
            str(PLUGIN_MODEL), legacy_opts,
            providers=["DmlExecutionProvider", "CPUExecutionProvider"])
        _KEEP_ALIVE.append(legacy)

        rng = np.random.default_rng(0)
        feed = {plain.get_inputs()[0].name: rng.random((1, 3, 640, 640), dtype=np.float32)}
        fused = plain.run(None, feed)[0]
        serialized = legacy.run(None, feed)[0]
        np.testing.assert_allclose(fused, serialized, rtol=1e-5, atol=1e-6)
