# -*- coding: utf-8 -*-
"""无限驾驶 v2 固定数值真源（写死进程序，不 OCR）。

来源：对局录像实测 + 源码配置表核对（inf_drive_stage_dat）。变动量（本场得分、
关卡进度、本轮总得分）走 OCR / 识别，不在此列；此处只放每关/每阶段恒定的量。
"""

from __future__ import annotations

# 每关目标分（过关条件）。结算页「过关条件」即此值，固定不 OCR。
# 真源：inf_drive_stage_dat RoundCfg.PveTargetScore（源码核对）。
PVE_STAGE_TARGETS: dict[int, int] = {
    1: 6000,
    2: 12000,
    3: 18000,
    4: 28000,
    5: 40000,
}

# 阶段时长（秒）：PVE 阶段1 / 阶段2。总限时 = 两者之和（准备页按此显示）。
STAGE1_DURATION_S = 25
STAGE2_DURATION_S = 65
ROUND_TOTAL_S = STAGE1_DURATION_S + STAGE2_DURATION_S
# PVP 阶段2 与 PVE 不同（300s），且无目标分、比血量；本骨架只跑 PVE。
PVP_STAGE2_DURATION_S = 300

# 阶段2特殊事件时长（秒）按关变化：追猎 / 缆绳卡车（服务端二选一，客户端回退追猎）。
EVENT_DURATION_S: dict[int, dict[str, int]] = {
    1: {"chase": 50, "truck": 50},
    2: {"chase": 50, "truck": 60},
    3: {"chase": 65, "truck": 60},
    4: {"chase": 65, "truck": 65},
    5: {"chase": 65, "truck": 65},
}

# 驾驶阶段等待结束信号的余量（秒）：计时归零到结算/选卡画面落地有转场延迟。
DRIVE_END_MARGIN_S = 8

# 选卡槽位（归一化中心）：三槽位置固定，点击不需要 OCR。
CARD_SLOT_CENTERS: tuple[tuple[float, float], ...] = (
    (0.3125, 0.6611),
    (0.5, 0.6611),
    (0.6875, 0.6611),
)
# 槽内能力名字 ROI（归一化）：OCR 读名字用于排优先级；点击仍走固定槽位坐标。
CARD_NAME_ROIS: tuple[tuple[float, float, float, float], ...] = (
    (0.2656, 0.3972, 0.3594, 0.4250),
    (0.4531, 0.3972, 0.5469, 0.4250),
    (0.6406, 0.3972, 0.7344, 0.4250),
)
# 选卡优先级（名字→秩，越小越优先）。名字真源 = 源码能力库 Skill.Name；
# 秩为骨架期起步排序（偏超车/得分向），可按实测调整；未收录的名字落默认秩。
CARD_PRIORITY: dict[str, int] = {
    "冲刺": 0,
    "飞行": 1,
    "金币掉落": 2,
    "弹射": 3,
    "幽灵化": 4,
    "闪现": 5,
    "浮空装置": 6,
    "羽翼": 7,
    "复原": 8,
}
CARD_PRIORITY_DEFAULT = 99
# OCR 全失败时的回退槽位。
CARD_PICK_SLOT = 0
# 阶段2选卡次数上限（1/2 + 2/2 两轮）。
CARD2_MAX_PICKS = 2

# 结算页变动量 OCR 区域（归一化）：本场得分数字 / 底部进度与累计分条。
# 固定量（过关条件=目标分、进度分母5）不 OCR，见 PVE_STAGE_TARGETS。
RESULT_SCORE_ROI: tuple[float, float, float, float] = (0.3234, 0.4958, 0.6883, 0.5986)
RESULT_PROGRESS_ROI: tuple[float, float, float, float] = (0.2656, 0.7431, 0.7344, 0.7917)