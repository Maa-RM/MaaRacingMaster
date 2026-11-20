# -*- coding: utf-8 -*-
"""无限驾驶活动模块：v2 持久流程状态机（骨架）。

感知/几何/控制基建自 speedrush 归档搬迁；本模块接线 v2 的**持久流程**导航：
入口 → 关卡挑战 → 准备 → 选卡 → 驾驶 → 结算 → 放弃回环。一次性弹窗（赛季重置 /
规则教学 / 得分规则门 / 事件介绍等）按裁定押后，不在本骨架内驱赶。

驾驶阶段当前为**等待结束信号的占位实现**（不施加控制）：v2 阶段按计时推进，
骨架据此走通全链路；驾驶控制方案后续接入 ``_drive``。
"""

from __future__ import annotations

import time

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger
from maaracing_master.core.nav_graph import NavGraph
from maaracing_master.core.ocr import RapidOcrEngine
from maaracing_master.core.advance import normalize_text
from maaracing_master.core.step_machine import Command, StepMachine, Verdict
from maaracing_master.plugins.infinite_drive import IMAGE_DIR, PIPELINE_DIR
from maaracing_master.plugins.infinite_drive import flow_data

# 流程表：(阶段名, 动作节点, phase)。node=None 表示非点击段：
# phase="card1"/"card2" 走选卡，"p1"/"p2" 走驾驶等待。
FLOW: list[tuple[str, str | None, str | None]] = [
    ("入口·比赛", "infinite_drive.点比赛", None),
    ("入口·娱乐玩法", "infinite_drive.点娱乐玩法", None),
    ("入口·极速狂飙", "infinite_drive.点极速狂飙卡片", None),
    ("关卡挑战", "infinite_drive.点关卡挑战", None),
    ("选车·首车", "infinite_drive.选车.r1", None),
    ("选车·确定", "infinite_drive.选车.r2", None),
    ("准备·开始", "infinite_drive.点开始挑战", None),
    ("卡牌·阶段1", None, "card1"),
    ("驾驶·阶段1", None, "p1"),
    ("卡牌·阶段2", None, "card2"),
    ("驾驶·阶段2", None, "p2"),
    ("结算·下一关", "infinite_drive.点下一关", None),
    ("准备·放弃", "infinite_drive.点放弃本轮", None),
    ("放弃·确认", "infinite_drive.点继续放弃", None),
]
# 循环体起点：放弃确认回模式页后，从关卡挑战再开一轮
LOOP_START_INDEX = 3

# 条件页步骤：选车页只在无预设车/预设被拒时出现；锚点不在即整步跳过
OPTIONAL_STAGES = {"选车·首车", "选车·确定"}
_CAR_SELECT_ANCHOR = "infinite_drive.__confirm.选车页"

# 每步的落地确认锚点（画面确实切到落地页的判据）
STEP_CONFIRM: dict[str, str] = {
    "入口·比赛": "infinite_drive.__confirm.比赛页",
    "入口·娱乐玩法": "infinite_drive.__confirm.娱乐玩法页",
    "入口·极速狂飙": "infinite_drive.__confirm.模式页",
    "关卡挑战": "infinite_drive.__confirm.准备页",
    "选车·确定": "infinite_drive.__confirm.准备页",
    "准备·开始": "infinite_drive.__confirm.卡牌选择",
    "结算·下一关": "infinite_drive.__confirm.准备页",
    "准备·放弃": "infinite_drive.__confirm.放弃确认框",
    "放弃·确认": "infinite_drive.__confirm.模式页",
}

# 步级重试剂量（不含首点的重发上限 / 观察窗 / 窗内轮询）
STEP_MAX_RETRIES = 3
STEP_WINDOW_S = 2.0
STEP_POLL_S = 0.3

_CARD_ANCHOR = "infinite_drive.__confirm.卡牌选择"
_DRIVE_END = {"p1": "infinite_drive.__confirm.驾驶1结束",
              "p2": "infinite_drive.__confirm.驾驶2结束"}
_DRIVE_DURATION = {"p1": flow_data.STAGE1_DURATION_S,
                   "p2": flow_data.STAGE2_DURATION_S}


class InfiniteDriveModule(ActivityModule):
    """极速狂飙 v2 活动模块（持久流程骨架）。"""

    ID = "infinite_drive"
    NAME = "无限驾驶"
    STAGE_ORDER: list[str] = [stage for stage, _, _ in FLOW]
    REQUIRES_GAMEPAD_EXCLUSIVE = True
    REQUIRES: frozenset[str] = frozenset({"capture", "gamepad"})
    REQUIRED_ASSETS: tuple[str, ...] = ()
    COVER = "cover.jpg"
    DEBUG_SAVE = "self"

    def __init__(self, ctx: ActivityContext):
        super().__init__(ctx)
        self._graph: NavGraph | None = None
        self._running = False
        self._stage_name: str | None = None
        self._ocr: RapidOcrEngine | None = None
        self.last_settlement: dict[str, str] = {}

    @property
    def current_stage(self) -> str | None:
        return self._stage_name

    def start(self, start_from: str | None = None) -> bool:
        assert self.ctx is not None, "离线模式（ctx=None）不可调用 start()"
        if not self.ctx.connect():
            logger.log("[无限驾驶] 窗口连接失败，模块终止", "ERROR")
            return False
        if not self.ctx.check_window_aspect():
            logger.log("[无限驾驶] 游戏窗口不是 16:9（模板与 ROI 按 720p 设计），模块终止", "ERROR")
            return False
        self._running = True
        graph = NavGraph(self.ctx)
        graph.add_plugin(PIPELINE_DIR, IMAGE_DIR)
        self._graph = graph
        if not graph.load():
            logger.log("[无限驾驶] 导航图加载失败，模块终止", "ERROR")
            self._graph = None
            return False
        index = 0
        if start_from:
            names = [s for s, _, _ in FLOW]
            if start_from in names:
                index = names.index(start_from)
        ok = False
        try:
            ok = self._run_flow(index)
        finally:
            self._stage_name = None
        return ok

    def stop(self) -> None:
        self._running = False
        if self._graph is not None:
            self._graph.request_stop()

    def cleanup(self) -> bool:
        self._running = False
        if self._graph is not None:
            ok = self._graph.shutdown()
            self._graph = None
            return ok
        return True

    # ---------- 流程推进 ----------

    def _run_flow(self, index: int) -> bool:
        assert self._graph is not None
        while self._running:
            while index < len(FLOW):
                if not self._running:
                    return True
                stage, node, phase = FLOW[index]
                self._stage_name = stage
                if stage in OPTIONAL_STAGES:
                    if not self._graph.run(_CAR_SELECT_ANCHOR, _CAR_SELECT_ANCHOR):
                        index += 1
                        continue
                if stage == "结算·下一关":
                    self._read_settlement()
                if node is None:
                    if phase in ("card1", "card2"):
                        ok = self._pick_cards(phase)
                    else:
                        ok = self._drive(phase)
                    if not ok:
                        logger.log(f"[无限驾驶] 「{stage}」未正常结束，本轮中止", "WARNING")
                        return self._stop_received()
                else:
                    if not self._run_step(node, stage):
                        return self._stop_received()
                index += 1
            index = LOOP_START_INDEX
        return True

    def _stop_received(self) -> bool:
        return not self._running

    def _run_step(self, action_node: str, stage: str) -> bool:
        """发一次 UI 点击并有限重发；成功 = 源按钮消失 或 落地锚点从无到有，且锚点此刻在位。"""
        assert self._graph is not None
        assert self.ctx is not None
        confirm_node = STEP_CONFIRM.get(stage)
        if confirm_node is None:
            return self._graph.run(action_node, action_node)
        machine = StepMachine(max_retries=STEP_MAX_RETRIES, window_s=STEP_WINDOW_S,
                              clock=time.monotonic)
        send_click = True
        landed_ever = False
        clicked_once = False
        confirm_pre = self._graph.run(confirm_node, confirm_node)
        while self._running and self.ctx.lifecycle.running:
            if send_click:
                machine.arm()
                self._graph.last_click_landed = None
                self._graph.last_click_sent = False
                self._graph.run(action_node, action_node)
                send_click = False
                landed = getattr(self._graph, "last_click_landed", None)
                click_sent = bool(getattr(self._graph, "last_click_sent", False))
                if landed is True:
                    landed_ever = True
                elif landed is None and clicked_once and not click_sent:
                    landed_ever = True
                if click_sent:
                    clicked_once = True
            page_ok = self._graph.run(confirm_node, confirm_node)
            page_changed = page_ok and not confirm_pre and clicked_once
            confirm_pre = page_ok
            cmd = machine.observe(
                Verdict.SUCCESS if (page_ok and (landed_ever or page_changed))
                else Verdict.PENDING)
            if cmd is Command.DONE:
                return True
            if cmd is Command.RETRY:
                if landed_ever:
                    machine.arm()
                else:
                    send_click = True
                continue
            if cmd is Command.WAIT:
                if not self.ctx.lifecycle.sleep(STEP_POLL_S):
                    break
                continue
            if cmd is Command.FAIL:
                logger.log(f"[无限驾驶] 步骤「{stage}」重发 {STEP_MAX_RETRIES} 次仍未落地，本轮中止",
                           "WARNING")
                return False
            return False
        return False

    def _pick_cards(self, phase: str) -> bool:
        """选卡：识别槽内能力名字排优先级，点该槽固定坐标；阶段2可能连选两轮。"""
        assert self._graph is not None
        assert self.ctx is not None
        max_picks = 1 if phase == "card1" else flow_data.CARD2_MAX_PICKS
        picked = 0
        for _ in range(max_picks):
            if not self._running:
                return False
            if not self._graph.run(_CARD_ANCHOR, _CARD_ANCHOR):
                break
            slot = self._choose_card_slot()
            self._graph.run(f"infinite_drive.选卡.r{slot + 1}",
                            f"infinite_drive.选卡.r{slot + 1}")
            picked += 1
            if not self.ctx.lifecycle.sleep(0.6):
                return False
        if picked == 0:
            logger.log(f"[无限驾驶] 「{phase}」未出现选卡页", "WARNING")
            return False
        return not self._graph.run(_CARD_ANCHOR, _CARD_ANCHOR)

    def _choose_card_slot(self) -> int:
        """OCR 读三槽名字 → 按优先级表取最优槽；读不出回退默认槽。"""
        assert self.ctx is not None
        rgb = self.ctx.capture.frame_with_age()[0]
        if self._ocr is None:
            self._ocr = RapidOcrEngine()
        best_rank = flow_data.CARD_PRIORITY_DEFAULT
        best_slot = flow_data.CARD_PICK_SLOT
        for i, roi in enumerate(flow_data.CARD_NAME_ROIS):
            result = self._ocr.recognize(rgb, roi)
            name = normalize_text(getattr(result, "text", "") or "")
            rank = flow_data.CARD_PRIORITY.get(name, flow_data.CARD_PRIORITY_DEFAULT)
            if rank < best_rank:
                best_rank, best_slot = rank, i
        return best_slot

    def _read_settlement(self) -> None:
        """读结算变动量（本场得分 / 进度与累计分），供日志与后续判定；固定量不 OCR。"""
        assert self.ctx is not None
        rgb = self.ctx.capture.frame_with_age()[0]
        if self._ocr is None:
            self._ocr = RapidOcrEngine()
        score = self._ocr.recognize(rgb, flow_data.RESULT_SCORE_ROI)
        progress = self._ocr.recognize(rgb, flow_data.RESULT_PROGRESS_ROI)
        score_text = getattr(score, "text", "") or ""
        progress_text = getattr(progress, "text", "") or ""
        self.last_settlement = {"score": score_text, "progress": progress_text}
        logger.log(f"[无限驾驶] 结算读数: 得分={score_text} 进度={progress_text}", "DEBUG")

    def _drive(self, phase: str) -> bool:
        """驾驶阶段占位：等待本阶段结束信号（不施加控制），响应停止信号。"""
        assert self._graph is not None
        assert self.ctx is not None
        end_node = _DRIVE_END[phase]
        deadline = time.monotonic() + _DRIVE_DURATION[phase] + flow_data.DRIVE_END_MARGIN_S
        while self._running and self.ctx.lifecycle.running:
            if self._graph.run(end_node, end_node):
                return True
            if time.monotonic() >= deadline:
                logger.log(f"[无限驾驶] 驾驶{phase} 超时未见结束信号", "WARNING")
                return False
            if not self.ctx.lifecycle.sleep(STEP_POLL_S):
                return False
        return False