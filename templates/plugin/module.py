# -*- coding: utf-8 -*-
"""<插件名> 活动模块骨架：演示 ActivityModule 契约与 core 能力门面的最小完整实现。

生命周期：controller.start_module() → start()（新线程）→ stop()/异常 → cleanup()。
主循环约定：每帧检查 ctx.lifecycle.running，睡眠一律用 ctx.lifecycle.sleep（100ms 级响应停止）。

本骨架演示的契约面：
- 必填声明与启动约束（REQUIRES / REQUIRED_ASSETS / 断点解析）
- 配置面板（激活示范：复制后 GUI 即出现面板，字段键 loop_interval_s，按需删减）
- 封面 / 调试存盘档位（激活声明）；性能行 / 数据看板 / 人工接管期（注释示例，按需启用）
- 能力门面 ctx（capture / lifecycle / gamepad / debug_renderer / 窗口事实）
模板匹配 / OCR / YOLO / 玩法流水 / 成就等共享基建的取用见 templates/plugin/README.md。
"""

from __future__ import annotations

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger


class SampleModule(ActivityModule):
    """把一个游戏活动（导航 → 循环 → 结算）实现为独立模块的最小骨架。"""

    ID = "sample"
    NAME = "示例模块"

    # 阶段清单：GUI 阶段列表与断点下拉的唯一来源；首阶段通常为「归位/进入活动」
    STAGE_ORDER = [
        "归位",
        "导航",
        "主循环",
        "结算",
    ]

    # 启动前校验的能力集；可用值见 ctx.capabilities（core/base.py）：
    #   "capture"（恒可用） / "gamepad"（需 ViGEmBus 驱动） / "debug_renderer"（恒可用）
    # 同一模块在不同配置下需要的启动条件可能不同（如录制模式不需要手柄）：
    # 覆盖 required_capabilities(config) / requires_exclusive_gamepad(config) 按配置求值，
    # 类属性仍是完整声明，覆盖时基于 cls.REQUIRES 增删，不另抄一份清单。
    # 示例：REQUIRES = frozenset({"gamepad"})
    REQUIRES: frozenset[str] = frozenset()

    # 本模块要求虚拟手柄独占时置 True（启动前会提示断开物理手柄）
    REQUIRES_GAMEPAD_EXCLUSIVE = False

    # 插件自带必需资源（相对插件目录），启动前逐项检查存在性，缺失即拦截
    # 示例：REQUIRED_ASSETS = ("resources/models/perception/model.onnx",)
    # 权重按用途在 onnx/ 下分子目录，许可声明 README 与权重同目录（真源在插件内）
    REQUIRED_ASSETS: tuple[str, ...] = ()

    # 模块封面（相对 resources/ 的文件名）：模块管理弹窗的卡片底图；未声明用通用默认封面
    # 示例：COVER = "cover.jpg"
    COVER: str = ""

    # 统一调试流的存盘档位：on=写运行目录（默认） | self=模块自有取证，统一流只记段边界
    # 与指向、不复制帧 | off=不存盘；PEEP 实时预览不受影响
    DEBUG_SAVE: str = "on"

    # 配置面声明：GUI 配置项的键与初值；也是 profile 回填的白名单——不加进这里就不会被保存
    DEFAULT_MODULE_CONFIG: dict = {
        "loop_interval_s": 0.5,
    }

    # 配置表单（契约见 core/config_ui_contract.py）：宿主通用渲染，插件不提供自带 HTML/JS。
    # 默认值 / 动态候选分别由 DEFAULT_MODULE_CONFIG / config_choices() 提供，这里只描述呈现。
    # 控件六型：switch / segmented / select / number / stepper / text；字段键必须在 DEFAULT_MODULE_CONFIG 内。
    CONFIG_UI_CONTRACT = 2
    CONFIG_UI = {
        "groups": [
            {
                "title": "运行",
                "fields": [
                    {
                        "key": "loop_interval_s", "type": "number", "label": "循环间隔",
                        "description": "每轮阶段处理后的等待时长",
                        "min": 0.1, "max": 5.0, "step": 0.1, "suffix": "秒",
                    },
                ],
            },
        ],
    }

    # 性能面板契约（可选；细则见 core/perf_contract.py）：不声明就只显示资源面。
    # 模块声明行（key / label / kind / 阈值），运行期在 read_perf_snapshot() 只给值，
    # 判级归 core；声明了却不报的行在面板恒显示「—」。示例（照抄前先想好谁在报值）：
    # PERF_CONTRACT = 1
    # PERF_ROWS = (
    #     dict(key="tick_rate", label="处理拍率", kind="rate", stat="p50_gap_inverse",
    #          category="player", precision=1, warn_at=8.0, error_at=4.0,
    #          worse_when="below", hint="处理变慢：降低调试落盘或游戏画质"),
    # )
    # THREAD_LABELS = {"sample-worker": "工作线程"}   # 线程由 core 嗅探，模块只贴中文别名

    # 数据看板（可选；契约见 core/dashboard_contract.py）：DASHBOARD_CONTRACT = 1 并实现
    # read_dashboard()（classmethod；不得依赖模块实例——读持久化或既有 RPC）。
    # 载荷 = block 列表（stats / list / text / path / action / empty），非法块被 core 丢弃。
    # DASHBOARD_CONTRACT = 1
    # @classmethod
    # def read_dashboard(cls) -> dict:
    #     return {"blocks": [{"type": "empty", "title": "暂无记录"}]}

    def __init__(self, ctx: ActivityContext | None):
        super().__init__(ctx)
        self._current_stage: str | None = None
        self._loop_interval_s: float = type(self).DEFAULT_MODULE_CONFIG["loop_interval_s"]

    # ------------------------------------------------------------------
    #  配置挂钩（GUI 写入与启动注入都走这里）
    # ------------------------------------------------------------------

    def set_module_config(self, config: dict) -> dict:
        """写配置。controller 在启动时注入实例；GUI 经 RPC 改缓存也调它。

        非法值一律静默修正并返回最终值，不抛给调用方；旧键折算与互斥约束写在
        normalize_config()（宿主在读回存档与接收写入前各调一次），不在这里长堆 if。
        """
        value = config.get("loop_interval_s")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self._loop_interval_s = min(max(float(value), 0.1), 5.0)
        return {"loop_interval_s": self._loop_interval_s}

    @classmethod
    def config_summary(cls, config: dict) -> list[str]:
        """任务卡摘要（入参为宿主投影后的有效配置；只读纯函数，不创建实例）"""
        return [f"循环间隔 {config.get('loop_interval_s')} 秒"]

    def config_status(self) -> dict | None:
        """配置面板下的运行状态行（仅运行中的实例被调用）"""
        return {"text": f"循环间隔 {self._loop_interval_s} 秒", "level": "ok"}

    # 人工接管期声明（可选）：返回 True 的时段，紧急停止不把按键当急停信号
    # （录制驾驶等「玩家自己操作」阶段；判据与急停循环见 core/CODE_WIKI.md §5.3.1）
    # def manual_input_active(self) -> bool:
    #     return self._current_stage == "人工驾驶"

    # ------------------------------------------------------------------
    #  ActivityModule 契约（core/base.py）
    # ------------------------------------------------------------------

    @property
    def current_stage(self) -> str | None:
        """当前阶段名；GUI 运行状态展示与断点定位都读它"""
        return self._current_stage

    def start(self, start_from: str | None = None) -> bool:
        """启动模块：连接窗口 → 断点解析 → 主循环。

        返回 True = 自然结束（响应停止信号）；False = 启动校验失败。
        编排层按返回值判定该项成败，不得返回 None。
        """
        # 1. 连接游戏窗口（幂等；失败即终止，由 GUI 展示错误）
        if not self.ctx.connect():
            logger.log(f"[{self.NAME}] 窗口连接失败，模块终止", "ERROR")
            return False

        # 2. 断点解析：start_from 为 None 从头跑；非法值显式抛错。controller 启动前会按
        #    STAGE_ORDER 拦一道，这里仍不静默忽略——模块被别的路径直接调用时错误要看得见
        if start_from is None:
            self._current_stage = self.STAGE_ORDER[0]
        elif start_from in self.STAGE_ORDER:
            self._current_stage = start_from
            logger.log(f"[{self.NAME}] 从断点开始: 「{self._current_stage}」")
        else:
            raise ValueError(f"[{self.ID}] 未知断点「{start_from}」，合法值见 STAGE_ORDER")

        # 3. 能力取用示例（按需删减；全部经 ctx 门面，插件不接触高权限宿主对象）：
        #    frame = self.ctx.capture.screenshot()      # RGB ndarray；None=采集链路故障
        #    frame, fid, ts_ns, age_ms = self.ctx.capture.frame_with_age()  # 帧龄（新鲜度）
        #    self.ctx.verify_frame_client(frame.shape[1], frame.shape[0])   # 帧 vs 客户区 1:1
        #    self.ctx.check_window_aspect()             # 客户区约 16:9（ROI 按 720p 归一化的前提）
        #    if not self.ctx.window_foreground: ...     # 前台鼠标点击的安全前置（程序不抢前台）
        #    with self.ctx.gamepad.acquire() as pad:    # 手柄租约：退出自动归零归还
        #        pad.left_joystick(x_value=0, y_value=-32768); pad.update()
        #    renderer = self.ctx.enter_context(         # 调试渲染租约 → 生命周期托管
        #        self.ctx.debug_renderer.renderer(my_renderer))
        #    self._loop_interval_s                      # 配置已由宿主经 set_module_config 注入

        # 4. 阶段驱动：主循环 + 阶段分发
        logger.log(f"[{self.NAME}] 模块启动")
        while self.ctx.lifecycle.running:
            handler = self._stage_handlers().get(self._current_stage)
            if handler is not None:
                handler()

            if self._advance_stage():
                continue

            # 睡眠必须走 lifecycle（可中断）；不要用 time.sleep
            self.ctx.lifecycle.sleep(self._loop_interval_s)

        logger.log(f"[{self.NAME}] 模块停止")
        return True

    def stop(self) -> None:
        """请求停止：置停止信号，主循环在下一个 100ms 检查点退出"""
        self.ctx.lifecycle.request_stop()

    def cleanup(self) -> bool:
        """释放模块自有资源；经 enter_context 登记的资源由 Context 统一释放，无需重复归还。

        返回 True = 收尾完成；持有原生线程/句柄的模块若未能确认其退出，必须返回 False
        （编排层据此保留资源并拒绝下一轮），不得返回 None。
        """
        self._current_stage = None
        return True

    # ------------------------------------------------------------------
    #  内部实现
    # ------------------------------------------------------------------

    def _stage_handlers(self) -> dict[str, callable]:
        """阶段名 → 处理函数；返回 None 的阶段走默认推进"""
        return {
            "归位": self._stage_home,
            "导航": self._stage_navigate,
            "主循环": self._stage_loop,
            "结算": self._stage_settle,
        }

    def _advance_stage(self) -> bool:
        """推进到下一阶段；末阶段返回 False（主循环可直接收尾或重置回首阶段）"""
        assert self._current_stage is not None
        idx = self.STAGE_ORDER.index(self._current_stage)
        if idx + 1 >= len(self.STAGE_ORDER):
            return False
        self._current_stage = self.STAGE_ORDER[idx + 1]
        logger.log(f"[{self.NAME}] 阶段切换 → 「{self._current_stage}」")
        return True

    # 各阶段示例实现：识别与点击按活动类型取用共享引擎（模板匹配 / OCR / YOLO /
    # 手柄光标导航）与导航图通路，见 templates/plugin/README.md「core 共享引擎速查」
    def _stage_home(self) -> None:
        frame = self.ctx.capture.screenshot()
        if frame is None:
            logger.log(f"[{self.NAME}] 截图失败，重试", "WARNING")
        logger.log(f"[{self.NAME}] 归位：识别当前界面（示例占位）")

    def _stage_navigate(self) -> None:
        logger.log(f"[{self.NAME}] 导航：点进活动入口（示例占位）")

    def _stage_loop(self) -> None:
        logger.log(f"[{self.NAME}] 主循环：活动核心玩法（示例占位）")

    def _stage_settle(self) -> None:
        logger.log(f"[{self.NAME}] 结算：领取奖励并回大厅（示例占位）")
