#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃层退路：退出类原语循环，直到大厅锚点命中（有界）。

三层失败设计里的第三层：

* 正常层——按流程逐步推进，每步等落地确认，失败了重发；
* 异常层——清掉挡住按钮的偶发弹窗（推进型文字兜底 advance），再按页面探针路由；
* 崩溃层——页面已经认不出来，只能往回退，也就是本模块。

**只点退出类控件**（关闭叉 / 返回箭头 / 主页按钮），不跑 advance：它的白名单是
「继续 / 进入 / 领取」，语义是往前推进，在流程中段点它可能点进更深的页面。
退出类控件点完是往回走，方向一致。

大厅判据由调用方传入节点名：大厅锚点是各模块自己的知识，core 不点名业务层
（红线：core 不得引用 `<module>.` 节点）。本模块只提供机制——循环、有界、退出原语。
"""
from __future__ import annotations

import time

import cv2

from maaracing_master.core.logger import logger
from maaracing_master.core.template_match import find_any_cs

# 退出类原语：模板都住全局模板目录（`core/resources/image`），所有图都能按名解析。
# ROI 与启动链同名控件一致：关闭叉在右上象限（弹窗尺寸浮动），返回/主页在左上角。
_PRIMITIVES: tuple[tuple[str, tuple[str, ...], tuple[float, float, float, float]],
                   ...] = (
    ("关闭叉", ("common_close_dark.png", "common_close_light.png"),
     (0.70, 0.02, 0.99, 0.42)),
    ("返回箭头", ("common_back.png",), (0.0, 0.0, 0.14, 0.10)),
    ("主页按钮", ("common_home.png",), (0.0, 0.0, 0.14, 0.10)),
)
PRIMITIVE_THRESHOLD = 0.85
CLICK_TIMEOUT_S = 5.0   # 崩溃层要快：导航超时就换下一个原语，不在此地长等
SETTLE_S = 0.8          # 点完等页面切换
IDLE_POLL_S = 0.6       # 本轮没有可点的原语时，等一拍再重判


def return_to_hall(graph, hall_anchor: str, *, max_rounds: int = 8,
                   deadline_s: float = 20.0, sleep=time.sleep) -> bool:
    """按序点退出原语，直到大厅锚点命中；返回是否回到了大厅。

    每轮先判大厅（probe 不点击），再找第一个命中的退出控件点它，然后回到轮首
    重新判大厅——页面可能一直在变，不能点完就假定到位。轮数与总时长双上限，
    防止在无法退出的页面上无限点。
    """
    deadline = time.monotonic() + deadline_s
    for _ in range(max_rounds):
        if time.monotonic() >= deadline:
            break
        lifecycle = getattr(graph.ctx, "lifecycle", None)
        if lifecycle is not None and not lifecycle.running:
            return False
        at_hall = _at_hall(graph, hall_anchor)
        if at_hall is True:
            return True
        if at_hall is None:
            # 框架收摊中（查不成 ≠ 页面不在）：不动作，等一拍重判，不能在此时点击。
            sleep(IDLE_POLL_S)
            continue
        clicked = _click_one_primitive(graph)
        sleep(SETTLE_S if clicked else IDLE_POLL_S)
    return _at_hall(graph, hall_anchor) is True


def _at_hall(graph, hall_anchor: str) -> bool | None:
    """判大厅：True/False；框架收摊中（查不成）返回 None，调用方按「还没到」处理。"""
    outcome = graph.probe(hall_anchor)
    if outcome == "hit":       # ProbeOutcome 是 str Enum，与字面量比较即可
        return True
    return None if outcome == "not_ready" else False


def _click_one_primitive(graph) -> bool:
    """在当前帧里找第一个命中的退出控件并点它；一个都没找到返回 False。"""
    frame = graph.frame()
    if frame is None:
        return False
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)   # 图侧统一 RGB 语义
    h, w = rgb.shape[:2]
    for label, names, (x1, y1, x2, y2) in _PRIMITIVES:
        roi = (int(x1 * w), int(y1 * h), int((x2 - x1) * w), int((y2 - y1) * h))
        box, score, hit = find_any_cs(rgb, list(names), graph.image_dirs,
                                      threshold=PRIMITIVE_THRESHOLD, roi=roi)
        if box is None:
            continue
        cx = (box[0] + box[2]) / 2.0 / w
        cy = (box[1] + box[3]) / 2.0 / h
        logger.log(f"[退大厅] 点{label}（{hit} {score:.3f}）", "DEBUG")
        graph.click(cx, cy, None, timeout_s=CLICK_TIMEOUT_S)
        return True
    return False
