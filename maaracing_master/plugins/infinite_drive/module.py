# -*- coding: utf-8 -*-
"""无限驾驶活动模块骨架：感知/几何/控制基建已自 speedrush 归档搬迁，操作流程未接线。

v2 玩法的模式结构、商店与阶段节奏整体重建（见 RULES.md 的核对状态），v1 的
流程与策略层留档在 _archive/speedrush/。本模块当前只承载注册与资源声明：
start() 显式失败并说明未接线，不假装能跑。接线时从归档的流程实现取结构、
按 v2 事实重写，不在本骨架上预支阶段清单。
"""

from __future__ import annotations

from maaracing_master.core.base import ActivityContext, ActivityModule
from maaracing_master.core.logger import logger


class InfiniteDriveModule(ActivityModule):
    """极速狂飙 v2 活动模块（施工形态：基建在位，流程未接线）。"""

    ID = "infinite_drive"
    NAME = "无限驾驶"
    # 流程未接线：阶段清单只占位，GUI 断点下拉随之只有占位项
    STAGE_ORDER = ["待接线"]
    # 驾驶阶段全程占用手柄（油门常踩 + 横向连续转向）——接线后启用
    REQUIRES_GAMEPAD_EXCLUSIVE = True
    REQUIRES: frozenset[str] = frozenset({"capture", "gamepad"})
    # 权重是可选模型包（发布包默认不带）：模块级资产门禁不要求，接线后按驾驶模式校验
    REQUIRED_ASSETS: tuple[str, ...] = ()
    COVER = "cover.jpg"
    DEBUG_SAVE = "self"

    def __init__(self, ctx: ActivityContext):
        super().__init__(ctx)

    @property
    def current_stage(self) -> str | None:
        return None

    def start(self, start_from: str | None = None) -> bool:
        logger.log("[无限驾驶] 操作流程未接线：感知/几何基建已就位，v2 流程施工中", "ERROR")
        return False

    def stop(self) -> None:
        pass

    def cleanup(self) -> None:
        pass