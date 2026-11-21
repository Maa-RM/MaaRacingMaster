# -*- coding: utf-8 -*-
"""无限驾驶活动模块：v2 持久流程状态机（骨架）。

感知/几何/控制基建自 speedrush 归档搬迁；本模块接线 v2 的**持久流程**导航：
入口 → 关卡挑战 → 准备 → 选卡 → 驾驶 → 结算 → 放弃回环。弹窗驱赶两层：每回合
必现、有稳定图形锚点的（阶段2事件介绍页）用紧 ROI 模板在选卡等待循环内驱赶；
偶发、文字提示驱动的（结算奖励链「点击屏幕继续」/通行证升级等）用 core advance
OCR 文案白名单做限频兜底（等待循环与步级重发前各挂一处，词表真源在 core）。
一次性弹窗（赛季重置 / 规则教学 / 得分规则门）按裁定押后——兜底若命中其推进
文案会顺带覆盖，但不做确定性驱赶。

驾驶阶段当前为**等待结束信号的占位实现**（不施加控制）：v2 阶段按计时推进，
骨架据此走通全链路；驾驶控制方案后续接入 ``_drive``。

**录制模式**（配置开关，默认关）：开启后整个流程会话落盘取证（画面 + 物理手柄
→ ``debug/infinite_drive/``），供真机验证流程与识别框落地。录制是旁路：取帧
走独立线程读 WGC 中心缓存，队列满丢帧不阻塞流程；录制失败只告警不中止流程。
录制态不要求虚拟手柄能力、不独占手柄（驾驶阶段可由用户以物理手柄驾驶）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger
from maaracing_master.core.nav_graph import NavGraph
from maaracing_master.core.ocr import RapidOcrEngine
from maaracing_master.core.advance import normalize_text
from maaracing_master.core.paths import debug_dir
from maaracing_master.core.session_layout import fresh_session_dir
from maaracing_master.core.step_machine import Command, StepMachine, Verdict
from maaracing_master.plugins.infinite_drive import IMAGE_DIR, PIPELINE_DIR
from maaracing_master.plugins.infinite_drive import dashboard_store
from maaracing_master.plugins.infinite_drive import flow_data
from maaracing_master.plugins.infinite_drive.recorder import DriveRecorder, make_session_dir
from maaracing_master.plugins.infinite_drive.session_layout import SOURCE_CONTROL

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
# 阶段2商店自动弹事件介绍页（每回合，点空白才关，盖住选卡页）：等选卡页的循环里
# 每拍先驱赶再等。检测锚点=「点击空白处关闭」提示（紧 ROI 模板，52 帧实测 1.0 分），
# 点的是右上角空白（真机帧实测全暗区，左上角是返回箭头不能碰）；动作节点自带同一
# 锚点识别，页面不在位时点击不会发出。其余一次性弹窗仍按裁定押后。
_EVENT_ANCHOR = "infinite_drive.__confirm.事件介绍"
_EVENT_DISMISS = "infinite_drive.关闭事件介绍"
# 偶发弹窗兜底（奖励链「点击屏幕继续」/通行证升级「继续」等无专模板弹窗）：OCR
# 文案白名单扫下半屏，命中推进提示就点它——core advance 机制，启动链同款，词表
# 真源在 core/advance 默认值。只在等待循环里低频调用（被挡住才扫，OCR 成本有界），
# 黑名单挡住购买类文案。
_ADVANCE_DISMISS = "infinite_drive.驱赶偶发弹窗"
ADVANCE_SWEEP_INTERVAL_S = 2.5
_DRIVE_END = {"p1": "infinite_drive.__confirm.驾驶1结束",
              "p2": "infinite_drive.__confirm.驾驶2结束"}
_DRIVE_DURATION = {"p1": flow_data.STAGE1_DURATION_S,
                   "p2": flow_data.STAGE2_DURATION_S}
# 驾驶阶段的阶段名（人工接管期判定用：录制轮驾驶阶段可由用户物理手柄驾驶）
_DRIVE_STAGES = frozenset({"驾驶·阶段1", "驾驶·阶段2"})

# 选卡页出现/消失的等待上限（秒）：驾驶结束信号命中横幅时商店尚未开（源码流程：
# 计时归零→横幅动画→开店），阶段2事件介绍页（每回合必弹，选卡循环内驱赶）需
# 关闭——bounded 等待吃掉开店延迟、刷新动画与关闭耗时；超时仍不在即按失败走。
CARD_ANCHOR_WAIT_S = 15.0
# 点一槽后等商店自关的窗口（秒，硬上限）：末次选中后商店立即自关（源码
# PickRemain<=0 → _leaveShopAndClose）；非末次是候选刷新动画，锚点可能短暂
# 消失——窗口内没关 = 点击被吞或点在已选卡上 → 重选重发。
PICK_CLOSE_WINDOW_S = 6.0
# 判「商店已关」的静默门槛（秒）：锚点持续不在达此时长才算关，候选刷新的
# 短暂闪失（约 1~2s）不误判；门槛必须大于闪失时长、窗口必须容得下门槛。
PICK_CLOSE_QUIET_S = 3.0
# 选卡点击的重发预算（不含首次）：点击可能被过渡动画吞掉；重复点已选卡在游戏里
# 只弹提示不改状态（源码 CardAlreadySelected），重发无副作用。
PICK_RESEND_BUDGET = 2

# 录制全程帧率：验证用不需要控制环的 30fps——流程页停留秒级、驾驶段有结束信号
# 把关，5fps 足以覆盖每次页面切换与点击落地，落盘体积同比例可控。
REC_FPS = 5.0
# 录制会话目录标签（<时间戳>_flowcheck）：程序驱动的全流程取证局
_RECORD_TAG = "flowcheck"


def _record_root() -> Path:
    """录制语料根：``debug/infinite_drive/``（用户数据目录，与调试产物同家）。"""
    return debug_dir() / "infinite_drive"


class InfiniteDriveModule(ActivityModule):
    """极速狂飙 v2 活动模块（持久流程骨架）。"""

    ID = "infinite_drive"
    # 对外模块名沿用玩法名；infinite_drive/无限驾驶只是内部代号（插件 ID 与文档用语）
    NAME = "极速狂飙"
    STAGE_ORDER: list[str] = [stage for stage, _, _ in FLOW]
    REQUIRES_GAMEPAD_EXCLUSIVE = True
    REQUIRES: frozenset[str] = frozenset({"capture", "gamepad"})
    REQUIRED_ASSETS: tuple[str, ...] = ()
    COVER = "cover.jpg"
    DEBUG_SAVE = "self"
    DASHBOARD_CONTRACT = 1

    # ---------- 配置面（录制开关；契约见 core/config_ui_contract.py） ----------

    DEFAULT_MODULE_CONFIG: dict = {"record_mode": False}
    CONFIG_UI_CONTRACT = 1
    CONFIG_UI = {
        "groups": [
            {"title": "录制", "fields": [
                {"key": "record_mode", "type": "switch", "label": "录制全程画面",
                 "description": "开启后运行时把全程画面与手柄输入落盘到调试目录，"
                                "供流程与识别框的真机核验；驾驶阶段不接管车辆，"
                                "可用手柄自行驾驶。"},
            ]},
        ],
    }

    # 线程名 → 中文别名（core 嗅探线程后按此贴标签）
    THREAD_LABELS: dict[str, str] = {
        "infinite_drive-rec-capture": "录制取帧",
        "infinite_drive-rec-frames": "录制写帧",
        "infinite_drive-rec-pad": "录制手柄",
    }

    # ---------- 启动约束（按本次配置求值，见基类说明） ----------

    @classmethod
    def required_capabilities(cls, config: dict | None = None) -> frozenset[str]:
        """录制模式不需要虚拟手柄能力：骨架不施加控制，也不创建虚拟设备。

        不这么分的话，机器上没装虚拟手柄驱动时连"只录不控"都启动不了；录制期
        用户以物理手柄驾驶，虚拟手柄本就不该在场。
        """
        caps = set(cls.REQUIRES)
        if bool((config or {}).get("record_mode")):
            caps.discard("gamepad")
        return frozenset(caps)

    @classmethod
    def requires_exclusive_gamepad(cls, config: dict | None = None) -> bool:
        """录制模式不独占手柄：验证轮的驾驶阶段可由用户以物理手柄驾驶。"""
        if bool((config or {}).get("record_mode")):
            return False
        return cls.REQUIRES_GAMEPAD_EXCLUSIVE

    @classmethod
    def config_summary(cls, config: dict) -> list[str]:
        if bool(config.get("record_mode")):
            return ["录制: 开"]
        return []

    def __init__(self, ctx: ActivityContext):
        super().__init__(ctx)
        self._graph: NavGraph | None = None
        self._running = False
        self._stage_name: str | None = None
        self._ocr: RapidOcrEngine | None = None
        self.last_settlement: dict[str, str] = {}
        # 录制模式：开启后全程落盘取证（真源配置键 record_mode，默认关）
        self._record_mode = bool(self.DEFAULT_MODULE_CONFIG["record_mode"])
        self._recorder: DriveRecorder | None = None
        self._record_stop: threading.Event | None = None
        self._record_thread: threading.Thread | None = None
        # 偶发弹窗 OCR 兜底的限频时刻（单调钟；0 = 立即可扫）
        self._next_sweep_at = 0.0
        # 本次运行的结算计数（看板记录的局内序号）
        self._settlement_round = 0

    @property
    def current_stage(self) -> str | None:
        return self._stage_name

    # ---------- 模块配置（GUI 读写；运行实况走 _state） ----------

    def get_module_config(self) -> dict:
        """读配置。运行中由 sidecar 路由到本实例，``_state`` 是录制实况。"""
        rec = self._recorder
        stats = rec.stats if rec is not None else None
        return {
            "record_mode": bool(self._record_mode),
            "_state": {
                "recording": bool(rec is not None and rec.running),
                "frames": int(stats["frames_written"]) if stats else 0,
                "frames_dropped": int(stats["frames_dropped"]) if stats else 0,
                "pad_samples": int(stats["pad_samples"]) if stats else 0,
                "record_dir": str(_record_root()),
            },
        }

    def set_module_config(self, config: dict) -> dict:
        """写配置。控制器在启动时注入实例；非法值静默修正并返回最终值。"""
        if isinstance(config, dict) and "record_mode" in config:
            self._record_mode = bool(config["record_mode"])
        return self.get_module_config()

    def config_status(self) -> dict | None:
        """录制进度状态行；未开录制或未在跑返回 None（正常态不占行）。"""
        rec = self._recorder
        if rec is None or not rec.running:
            return None
        st = rec.stats
        dropped = int(st["frames_dropped"])
        text = f"录制中：已写 {st['frames_written']} 帧"
        if dropped:
            text += f" · 丢帧 {dropped}"
        return {"text": text, "level": "ok" if not dropped else "warn"}

    def manual_input_active(self) -> bool:
        """录制轮的驾驶阶段由用户自行驾驶：急停按键检测暂停（急停只对程序操作期生效）。"""
        return bool(self._record_mode and self._recorder is not None
                    and self._stage_name in _DRIVE_STAGES)

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
        if self._record_mode:
            self._begin_recording()
        try:
            ok = self._run_flow(index)
        finally:
            self._stop_recording("stopped" if not self._running else "normal")
            self._stage_name = None
        return ok

    def stop(self) -> None:
        self._running = False
        if self._graph is not None:
            self._graph.request_stop()

    def cleanup(self) -> bool:
        self._running = False
        # start 的 finally 通常已收；防御性幂等调用，异常路径漏收时兜底
        self._stop_recording("cleanup")
        if self._graph is not None:
            ok = self._graph.shutdown()
            self._graph = None
            return ok
        return True

    # ---------- 全程录制（录制模式；旁路，失败不阻断流程） ----------

    def _begin_recording(self) -> None:
        """起全程录制器与取帧线程；失败只告警不中止（录制失败不该拦验证跑）。

        录的是**整个流程会话**（UI 步进 + 驾驶阶段），不是 v1 式按驾驶阶段分会话：
        v2 验证目标是全链路落地（页面衔接、点击落地、选卡与结算读数），驾驶段只是
        其中两段。source 记 control（程序驱动会话）；驾驶阶段用户以物理手柄驾驶时
        手柄样本照录，meta.pad_samples 可查。叠加图不产——本骨架无控制链决策可叠。
        """
        assert self.ctx is not None
        try:
            root = _record_root()
            stamp = logger.session_token or time.strftime("%Y%m%d_%H%M%S")
            session = make_session_dir(root, tag=_RECORD_TAG, stamp=stamp)
            picked = fresh_session_dir(root, session.name)
            if picked != session:
                logger.log(f"[无限驾驶] 会话目录 {session.name} 已存在，改写到 {picked.name}",
                           "WARNING")
            rec = DriveRecorder(picked, source=SOURCE_CONTROL)
            rec.start()
            stop = threading.Event()
            self._record_stop = stop
            self._record_thread = threading.Thread(
                target=self._record_sampler, args=(rec, stop),
                name="infinite_drive-rec-capture", daemon=True)
            self._record_thread.start()
            self._recorder = rec
            logger.log(f"[无限驾驶] 全程录制开启 → {picked}", "INFO")
        except Exception as exc:  # noqa: BLE001 —— 录制失败不阻断流程
            self._recorder = None
            self._record_thread = None
            self._record_stop = None
            logger.log(f"[无限驾驶] 录制器启动失败（本轮不录制）: {exc!r}", "WARNING")

    def _record_sampler(self, rec: DriveRecorder, stop: threading.Event) -> None:
        """全程取帧线程：与流程线程同源 ``frame_with_age``（只读 WGC 中心缓存），
        按 REC_FPS 喂录制器；帧缺席跳过——录制是旁路，不得反压流程。"""
        assert self.ctx is not None
        interval = 1.0 / REC_FPS
        while not stop.is_set():
            t0 = time.perf_counter()
            frame, fid, ts_ns, age_ms = self.ctx.capture.frame_with_age()
            if frame is not None:
                rec.record_frame(frame, frame_id=fid, ts_ns=ts_ns, age_ms=age_ms)
            stop.wait(max(0.0, interval - (time.perf_counter() - t0)))

    def _stop_recording(self, reason: str = "normal") -> None:
        """收录制：先停取帧线程（不再喂帧），再停录制器 flush 落 meta。幂等。"""
        stop = self._record_stop
        if stop is not None:
            stop.set()
        thread = self._record_thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._record_thread = None
        self._record_stop = None
        rec = self._recorder
        self._recorder = None
        if rec is not None:
            rec.stop(reason)

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
        """发一次 UI 点击并有限重发；成功 = 源按钮消失 或 落地锚点从无到有，且锚点此刻在位。

        **落地确认不过窗就重发点击**——settle 的「源按钮已消失」只是提前成功的
        旁证，不是免重发的豁免凭据：它盯着的是模板分数，页面焦点抖动/转场动画
        的中间帧能让分数瞬间掉到阈值下（2026-11-21 真机实测：娱乐玩法页签静止态
        0.985 分，点击后 125ms 的抖动帧却读到 0.766 < 0.8 被判「已生效」，而游戏
        实际没切页，旧逻辑随后只等落地页不重发，11 秒后预算耗尽中止）。若点击
        真已生效，重发这边的识别会因源按钮已消失而快速落空（不吃输入、不发点击），
        代价只是一次识别超时；若被吞（本例），重发就是唯一的自救路径。
        """
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
                # 落地确认没过窗就重发点击，settle 判「已生效」不豁免（见方法
                # docstring）；重发前做一次偶发弹窗兜底（限频）——奖励链/通行证
                # 升级这类弹窗盖住按钮时，重发的识别永远落空，先赶再发。
                self._sweep_transient_popup()
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
        """选卡直到商店自关：每轮 OCR 选最优槽点击，等关闭；没关就重选重发。

        终止条件 = 商店关闭（配额归零自动关店，源码口径），等价于全部配额选完，
        所以不数点击次数。点击可能被过渡动画吞掉（真机 2026-11-21 实测：第 2 次
        点击落在候选刷新动画尾巴上被吞，商店停在待选态）；也可能点在已选卡上
        （游戏只弹提示不改状态）——两种情况商店都不关，下一轮重选重发即可自愈，
        重发无副作用。
        """
        assert self._graph is not None
        assert self.ctx is not None
        max_picks = 1 if phase == "card1" else flow_data.CARD2_MAX_PICKS
        for _ in range(max_picks + PICK_RESEND_BUDGET):
            if not self._running:
                return False
            if not self._wait_card_anchor(want_present=True):
                logger.log(f"[无限驾驶] 「{phase}」等待内未出现选卡页", "WARNING")
                return False
            slot = self._choose_card_slot()
            self._graph.run(f"infinite_drive.选卡.r{slot + 1}",
                            f"infinite_drive.选卡.r{slot + 1}")
            if self._wait_card_anchor(want_present=False,
                                      wait_s=PICK_CLOSE_WINDOW_S,
                                      quiet_s=PICK_CLOSE_QUIET_S):
                return True
        logger.log(f"[无限驾驶] 「{phase}」重发预算内商店未关闭，本轮中止", "WARNING")
        return False

    def _wait_card_anchor(self, *, want_present: bool,
                          wait_s: float | None = None,
                          quiet_s: float = 0.0) -> bool:
        """等选卡页锚点到达期望状态（出现 / 持续消失）；bounded，响应停止信号。

        want_present 时每拍先驱赶事件介绍页再查选卡页：页面在位就点右上角空白
        （动作节点自带锚点识别，不在位不会发出），点没生效下一拍会再次进入重发，
        直到页面消失露出选卡页——每回合必弹，主动查的成本是每拍一次紧 ROI 模板。

        want_present=False 是「持续消失」判据：锚点连续不在达 ``quiet_s`` 才算
        消失（候选刷新动画会让锚点闪失 1~2s，不能算商店已关）；重见即重新起算，
        ``wait_s`` 是硬上限，锚点一直在位也会到期返回 False，不会无限等。
        quiet_s=0 退化为「首次不在即返回」。
        """
        assert self._graph is not None
        assert self.ctx is not None
        if wait_s is None:
            wait_s = CARD_ANCHOR_WAIT_S
        deadline = time.monotonic() + wait_s
        last_seen = time.monotonic()
        while True:
            if want_present and self._graph.run(_EVENT_ANCHOR, _EVENT_ANCHOR):
                self._graph.run(_EVENT_DISMISS, _EVENT_DISMISS)
                if not self.ctx.lifecycle.sleep(STEP_POLL_S):
                    return False
                continue
            present = bool(self._graph.run(_CARD_ANCHOR, _CARD_ANCHOR))
            if want_present:
                if present:
                    return True
            elif present:
                last_seen = time.monotonic()  # 重见即重新起算静默期
            elif time.monotonic() - last_seen >= quiet_s:
                return True   # 持续不在达门槛 = 商店已关
            if not self._running or time.monotonic() >= deadline:
                return False
            if not self.ctx.lifecycle.sleep(STEP_POLL_S):
                return False

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
        """读结算变动量并落一条战绩记录（程序局门槛：只在本流程内产生）。

        字段按 v2 最终结算页定（第 N 关 / 挑战结果 / 本场得分 / 过关条件 / 关卡
        进度 / 本轮总得分）；解析失败的字段显式置 None 不静默丢。落库失败只告警
        ——记录是旁路，不阻断流程。
        """
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
        self._settlement_round += 1
        try:
            entry = dashboard_store.record_settlement(
                score_text, progress_text,
                session_token=logger.session_token,
                round_no=self._settlement_round)
            dashboard_store.append(entry)
        except Exception as exc:  # noqa: BLE001 —— 记录失败不阻断流程
            logger.log(f"[无限驾驶] 结算记录落库失败: {exc!r}", "WARNING")
            return
        self.last_settlement["record"] = entry

    @classmethod
    def read_dashboard(cls) -> dict:
        """极速狂飙战绩看板：只读结算记录，不创建模块实例。

        记录只在程序局（模块流程内）产生——玩家在程序外自己开的局不进看板。
        读库失败返回 warn 态兜底，不把异常抛给 sidecar 的轮询。
        """
        try:
            return dashboard_store.read_dashboard_payload()
        except Exception as exc:  # noqa: BLE001 —— 轮询入口不得打崩数据页
            logger.log(f"[无限驾驶] 读取战绩失败: {exc!r}", "DEBUG")
            return {
                "version": dashboard_store.DASHBOARD_CONTRACT_VERSION,
                "title": "极速狂飙战绩",
                "badge": {"text": "读取失败", "tone": "warn"},
                "blocks": [{"type": "text", "text": "稍后自动重试。"}],
            }

    def _sweep_transient_popup(self) -> None:
        """偶发弹窗兜底（限频）：OCR 白名单扫下半屏，命中推进文案就点它。

        奖励链「点击屏幕继续」、通行证升级「继续」这类偶发弹窗没有也不该逐个裁
        模板——通用解是 core advance 文案识别（启动链同款，词表真源在 core）。
        只在等待循环里按 ``ADVANCE_SWEEP_INTERVAL_S`` 限频调用：被挡住才扫，
        OCR 成本有界；黑名单挡住购买类文案，不会误点扣费按钮。
        """
        assert self._graph is not None
        now = time.monotonic()
        if now < self._next_sweep_at:
            return
        self._next_sweep_at = now + ADVANCE_SWEEP_INTERVAL_S
        self._graph.run(_ADVANCE_DISMISS, _ADVANCE_DISMISS)

    def _drive(self, phase: str) -> bool:
        """驾驶阶段占位：等待本阶段结束信号（不施加控制），响应停止信号。

        等待期每拍做一次偶发弹窗兜底（内部限频）：结算奖励链这类弹窗会盖住
        结算页，让结束信号永远等不到——兜底点了它，信号自然露出。
        """
        assert self._graph is not None
        assert self.ctx is not None
        end_node = _DRIVE_END[phase]
        deadline = time.monotonic() + _DRIVE_DURATION[phase] + flow_data.DRIVE_END_MARGIN_S
        while self._running and self.ctx.lifecycle.running:
            if self._graph.run(end_node, end_node):
                return True
            self._sweep_transient_popup()
            if time.monotonic() >= deadline:
                logger.log(f"[无限驾驶] 驾驶{phase} 超时未见结束信号", "WARNING")
                return False
            if not self.ctx.lifecycle.sleep(STEP_POLL_S):
                return False
        return False