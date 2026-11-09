#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLO 目标检测模块：基于 ONNX Runtime 的推理封装（YOLOv8/11/26 默认导出的输出布局一致）

类别语义不内置于本模块：默认从模型 ONNX 元数据（Ultralytics 导出的 names 字段）
读取类别表，或由构造参数 classes 显式给定；不内置任何游戏的类别名与阈值。
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import numpy as np
import cv2
import onnxruntime as ort

from maaracing_master.core import dml_lock
from maaracing_master.core.logger import logger


def _dml_cache_dir() -> Path:
    """DirectML 内核编译缓存目录（只放内核缓存，不放序列化优化图）。"""
    cache_dir = Path(__file__).resolve().parent / "__pycache__" / "ort_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def build_session_options() -> "ort.SessionOptions":
    """DirectML 会话选项（DirectML 官方要求：顺序执行 + 关闭内存模式）。

    **不要设置 ``optimized_model_filepath``**：设置它之后 DirectML 的运行期图融合
    会失效，整图退化成数百个算子逐个派发——本机实测（RTX 4060 Laptop）前向
    3.5 ms → 6.0 ms，且融合计数从 1 个节点变成 239 个。融合是否生效用
    ``plugins/speedrush/tools/probe_infer.py`` 查，不要在生产启动路径里跑 profiler。
    """
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.enable_mem_pattern = False
    opts.enable_cpu_mem_arena = True
    opts.intra_op_num_threads = 4
    opts.inter_op_num_threads = 4
    cache_dir = _dml_cache_dir()
    opts.add_session_config_entry("session.dml_kernel_cache_path", str(cache_dir))
    opts.add_session_config_entry("session.dml_kernel_cache_enabled", "1")
    return opts


def _names_from_metadata(session: "ort.InferenceSession") -> dict[int, str] | None:
    """读 Ultralytics ONNX 导出内嵌的 names 元数据；无或格式异常返回 None"""
    raw = session.get_modelmeta().custom_metadata_map.get("names")
    if not raw:
        return None
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {int(k): str(v) for k, v in parsed.items()}


def _normalize_classes(classes: dict | list) -> dict[int, str]:
    """类别表归一为 {id: 名称}；显式给定时的校验前置到 session 创建之前"""
    if isinstance(classes, dict):
        norm = {int(k): str(v) for k, v in classes.items()}
    else:
        norm = {i: str(n) for i, n in enumerate(classes)}
    if not norm:
        raise ValueError("classes 不能为空")
    return norm


def _resolve_conf_by_id(class_map: dict[int, str], class_conf: dict[str, float] | None) -> dict[int, float]:
    """class_conf 按类别名校验并转为 {id: 阈值}；未知类名报错（防止静默无效）"""
    cc = class_conf or {}
    unknown = set(cc) - set(class_map.values())
    if unknown:
        raise ValueError(f"class_conf 含未知类别名: {sorted(unknown)}")
    return {cid: cc[name] for cid, name in class_map.items() if name in cc}


class YOLODetector:
    def __init__(
        self,
        model_path: str,
        conf: float = 0.5,
        iou: float = 0.45,
        classes: dict[int, str] | list[str] | None = None,
        class_conf: dict[str, float] | None = None,
    ):
        """
        Args:
            classes: 类别表，{类别id: 名称} 或按 id 升序的名称列表；
                     None 时从模型元数据读取，读不到则报错
            class_conf: 按类别置信度阈值，键为类别名；未覆盖的类别回退 conf
        """
        # ── 显式类别表：在模型加载前完成解析与校验（配置错误 fail-fast） ──
        explicit = _normalize_classes(classes) if classes is not None else None
        if explicit is not None:
            self.classes: dict[int, str] = explicit
            self._conf_by_id = _resolve_conf_by_id(explicit, class_conf)

        # ── Session：DirectML 优先，失败才回退 CPU（两种原因分开留痕） ──
        sess_opts = build_session_options()
        self.session_error: str | None = None
        try:
            self.session = ort.InferenceSession(
                model_path,
                sess_options=sess_opts,
                providers=["DmlExecutionProvider"],
            )
        except Exception as exc:  # noqa: BLE001 —— 保留原始原因后回退 CPU
            self.session_error = f"DirectML: {exc!r}"
            logger.log(f"DirectML 会话创建失败，回退 CPU：{exc!r}", "WARNING")
            self.session = ort.InferenceSession(
                model_path,
                sess_options=build_session_options(),
                providers=["CPUExecutionProvider"],
            )
        self.providers: tuple[str, ...] = tuple(self.session.get_providers())
        self.provider = "dml" if "DmlExecutionProvider" in self.providers else "cpu"
        # 加载判定只写文件、不上面板（面板降噪）：设备与缓存路径属加载细节；
        # 回退 CPU 的**原因**已在上面按 WARNING 留痕，这里不再重复。
        logger.log(f"ONNX 缓存: {_dml_cache_dir()}", "DEBUG")
        if self.provider == "dml":
            logger.log("YOLO 使用 GPU (DirectML)", "DEBUG")
        else:
            logger.log("YOLO 使用 CPU (DirectML 不可用)", "DEBUG")
        self.input_name = self.session.get_inputs()[0].name
        self.conf = conf
        self.iou = iou
        self.input_size = 640
        self._call_count = 0

        # ── 类别表缺省：从模型元数据读取（Ultralytics 导出内嵌 names） ──
        if explicit is None:
            meta = _names_from_metadata(self.session)
            if meta is None:
                raise ValueError(
                    "模型未携带 names 元数据，请通过 classes 参数显式给定类别表 "
                    "（{id: 名称} 或按 id 升序的名称列表）"
                )
            self.classes = meta
            self._conf_by_id = _resolve_conf_by_id(meta, class_conf)
        # 模型输出类别维与类别表不一致时只告警一次（避免静默错映射）
        self._class_dim_checked = False
        # 最近一次推理的分段耗时（等 DML 锁 / run 本体，ms）——时序归因插桩，
        # 调用方单线程，无跨线程竞争
        self.last_wait_ms = 0.0
        self.last_run_ms = 0.0

    def _empty_by_class(self) -> dict[str, list]:
        """按类别表建空结果容器（每个已声明类别都有键，值为空列表）"""
        return {name: [] for name in self.classes.values()}

    def _to_dets(self, xyxy, scores, classes, pad_x, pad_y, scale, ox, oy, orig_w, orig_h, indices, min_score=0.0):
        """将索引列表转为 (by_class, dets)

        by_class: {类别名: [(cx, cy, bw, bh), ...]}；dets 为检测结果 dict 列表。
        类别 id 不在类别表内（模型输出维多于声明类别）时跳过。
        """
        by_class = self._empty_by_class()
        dets = []
        for i in indices:
            i = int(i)
            if scores[i] < min_score:
                continue
            cls = int(classes[i])
            cls_name = self.classes.get(cls)
            if cls_name is None:
                continue
            x1, y1, x2, y2 = xyxy[i]
            x1, x2 = (x1 - pad_x) / scale + ox, (x2 - pad_x) / scale + ox
            y1, y2 = (y1 - pad_y) / scale + oy, (y2 - pad_y) / scale + oy
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(orig_w + ox, x2), min(orig_h + oy, y2)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            bw, bh = x2 - x1, y2 - y1
            dets.append({
                "box": (int(x1), int(y1), int(x2), int(y2)),
                "confidence": float(scores[i]),
                "class_name": cls_name,
            })
            by_class[cls_name].append((int(cx), int(cy), int(bw), int(bh)))
        return by_class, dets

    def _nms_per_class(self, xyxy, scores, classes, mask,
                       pad_x, pad_y, scale, ox, oy, orig_w, orig_h):
        """按类别分别做 NMS，避免跨类抑制（如 car 压掉 bonus_car）

        返回的索引是原始数组下标（供 _to_dets 使用）
        """
        classes_arr = classes[mask]
        unique_classes = np.unique(classes_arr)
        # mask_indices[i] = 原始数组中第 i 个被 mask 选中的下标
        mask_indices = np.where(mask)[0]
        all_indices = []
        for cls in unique_classes:
            cls_mask = classes_arr == cls
            # cls_local[j] = 第 j 个属于该类别的元素在 masked 数组中的位置
            cls_local = np.where(cls_mask)[0]
            b = xyxy[mask][cls_mask]
            # NMSBoxes 只认 [x,y,w,h]：直接喂 xyxy 会把 x2/y2 当宽高算 IoU，
            # 横向错位不大的两辆真车互相误压（2026-10-07 0068 帧实拍 + 两行复现）
            cls_boxes = np.stack([b[:, 0], b[:, 1],
                                  b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]], 1).tolist()
            cls_scores = scores[mask][cls_mask].tolist()
            if not cls_boxes:
                continue
            nms_idx = cv2.dnn.NMSBoxes(cls_boxes, cls_scores, 0.0, self.iou)
            if len(nms_idx) > 0:
                # 映射链：NMS下标 → 类别内下标 → masked下标 → 原始下标
                orig = mask_indices[cls_local[np.array(nms_idx)]]
                all_indices.extend(orig.tolist())
        return all_indices

    def _letterbox(self, img: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        """letterbox 到 (1,3,S,S) 浮点张量，复用中间缓冲。

        原路径每帧新建 640×640×3 uint8 + 一次 transpose/astype 产生 1.2M 浮点临时
        数组；改为常驻 uint8 画布 + 预分配 NCHW 输出、用 ``np.divide(out=...)``
        一次落位。本机实测 1.66 → 0.76 ms，输出逐位一致（见
        ``tests/test_yolo_detector.py::TestLetterbox``）。
        画布只在几何变化时重建——内容区每帧整块覆写，灰边保持不变。
        """
        size = self.input_size
        height, width = img.shape[:2]
        scale = min(size / height, size / width)
        new_h, new_w = int(height * scale), int(width * scale)
        pad_y, pad_x = (size - new_h) // 2, (size - new_w) // 2
        key = (size, new_h, new_w, pad_y, pad_x)
        if getattr(self, "_pad_key", None) != key:
            self._pad_u8 = np.full((size, size, 3), 114, dtype=np.uint8)
            self._blob_f32 = np.empty((1, 3, size, size), dtype=np.float32)
            self._pad_key = key
        self._pad_u8[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = cv2.resize(
            img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        np.divide(self._pad_u8.transpose(2, 0, 1), 255.0,
                  out=self._blob_f32[0], dtype=np.float32)
        return self._blob_f32, scale, pad_x, pad_y

    def __call__(self, img_rgb: np.ndarray, roi: tuple | None = None):
        """YOLO 推理

        Args:
            img_rgb: 全屏 RGB 图像
            roi: (x1, y1, x2, y2) 裁剪区域（原始图坐标），None 表示全图

        Returns:
            (by_class, debug_dets, all_raw_dets)
            by_class: {类别名: [(cx, cy, bw, bh), ...]}，每个已声明类别都有键
            debug_dets: 过滤后检测的 dict 列表（box/confidence/class_name）
            all_raw_dets: 低阈值全量检测（debug 可视化用）
        """
        if roi is not None:
            x1, y1, x2, y2 = roi
            orig = img_rgb[y1:y2, x1:x2].copy()
            ox, oy = x1, y1
        else:
            orig = img_rgb
            ox, oy = 0, 0

        orig_h, orig_w = orig.shape[:2]
        blob, scale, pad_x, pad_y = self._letterbox(orig)

        # DML 互斥（core.dml_lock）：控制拍感知是锁的阻塞持有方——本会话 run 与
        # 其他 DML 会话（如深度 worker）并发 run 会段错误杀进程（2026-09-25
        # 双线程复现），必须串行；感知不能跳拍，故阻塞等锁而非让锁。
        # 等锁与 run 分段计时：环频归因要分得清「锁等出来的」和「run 本身慢」。
        t_wait = time.perf_counter()
        dml_lock.RUN_WATCH.enter("yolo", waiting=True)
        try:
            with dml_lock.LOCK:
                self.last_wait_ms = (time.perf_counter() - t_wait) * 1000.0
                dml_lock.RUN_WATCH.acquired()
                t_run = time.perf_counter()
                raw_outputs = self.session.run(None, {self.input_name: blob})
                self.last_run_ms = (time.perf_counter() - t_run) * 1000.0
        finally:
            dml_lock.RUN_WATCH.exit()
        # 错峰节拍：深度 worker 据此把 54ms 前向排在感知放锁之后（见 dml_lock）。
        dml_lock.TICK_BEAT.note()
        outputs = raw_outputs[0]
        assert isinstance(outputs, np.ndarray), f"ONNX 返回非数组: {type(outputs)}"
        preds = outputs[0].transpose(1, 0)

        xywh = preds[:, :4]
        cls_conf = preds[:, 4:]
        if not self._class_dim_checked:
            self._class_dim_checked = True
            if cls_conf.shape[1] != len(self.classes):
                logger.log(f"模型输出类别维 {cls_conf.shape[1]} ≠ 声明类别数 {len(self.classes)}，"
                           f"请核对 classes 参数", "WARNING")
        max_scores = np.max(cls_conf, axis=1)
        max_classes = np.argmax(cls_conf, axis=1)

        # ── xyxy 坐标（统一计算一次） ──
        xyxy = np.zeros_like(xywh)
        xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
        xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
        xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
        xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2

        # ── 诊断：统计各类别原始置信度分布（每 10 帧一次） ──
        self._call_count += 1
        if self._call_count % 10 == 0:
            for cls_id, cls_name in sorted(self.classes.items()):
                confs = max_scores[max_classes == cls_id]
                if len(confs) > 0:
                    logger.log(f"[RAW] {cls_name}: {len(confs)}个 pred, "
                               f"max={confs.max():.3f}, mean={confs.mean():.3f}", "DEBUG")
                else:
                    logger.log(f"[RAW] {cls_name}: 无 pred（均低于0.01）", "DEBUG")

        # ── 全量低阈值检测（debug 可视化用，每类最多 20 个） ──
        RAW_CONF = 0.05
        raw_mask = max_scores > RAW_CONF
        all_raw_dets = []
        if np.any(raw_mask):
            raw_indices = np.where(raw_mask)[0]
            # 按置信度降序，每类取 top 20
            raw_sorted = raw_indices[np.argsort(-max_scores[raw_indices])]
            per_class_count = dict.fromkeys(self.classes, 0)
            top_raw = []
            for idx in raw_sorted:
                cls = int(max_classes[idx])
                if per_class_count.get(cls, 0) < 20:
                    top_raw.append(idx)
                    per_class_count[cls] = per_class_count.get(cls, 0) + 1
            _, all_raw_dets = self._to_dets(
                xyxy, max_scores, max_classes, pad_x, pad_y, scale, ox, oy,
                orig_w, orig_h, top_raw, min_score=RAW_CONF)

        # ── 正式过滤：按类别置信度阈值 + 按类分别做 NMS ──
        per_class_thresholds = np.array([self._conf_by_id.get(int(c), self.conf) for c in max_classes])
        mask = max_scores > per_class_thresholds
        if not np.any(mask):
            return self._empty_by_class(), [], all_raw_dets

        real_indices = self._nms_per_class(
            xyxy, max_scores, max_classes, mask,
            pad_x, pad_y, scale, ox, oy, orig_w, orig_h)
        if len(real_indices) == 0:
            return self._empty_by_class(), [], all_raw_dets
        by_class, debug_dets = self._to_dets(
            xyxy, max_scores, max_classes, pad_x, pad_y, scale, ox, oy,
            orig_w, orig_h, real_indices)

        return by_class, debug_dets, all_raw_dets
