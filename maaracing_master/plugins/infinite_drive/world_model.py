# -*- coding: utf-8 -*-
"""几何标定真源与地面投影常数。

**几何真源**：标定常数全部住 `resources/calibration/gate0.json`（三源分立：
游戏事实 RULES / 几何标定 gate0 / 行为参数 decision），经 `load_calib()`
fail-loud 读取，本模块不写标定字面量——写了就是第二份真相。口径与升级路径
注记随文件 `_notes` 走。

**归一纪律**：未经按行归一的像素横向差**禁止**当距离用——远处框分母过小，
像素差与车道量之间没有线性关系；横向读数一律来自车道线拟合（lane_obs）或
经 gate0 尺子归一，距离用框底行对地平线行的落差按地面投影换算。

**捕获几何**（WGC 固定 1280×720）见 gate0 与各消费模块的口径注记。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from maaracing_master.plugins.infinite_drive import GATE0_FILE

# 保险杠视角的地面纵深投影尺（距离 = GROUND_FOCAL_PX × 相机高 ÷ 框底行落差）。
# 线角的全幅焦距独立从标定文件读取。焦距取横向视场口径的实测值，
# 相机高见 RULES §8.1。
GROUND_FOCAL_PX = 470.0
BUMPER_CAM_H_M = 1.25


@dataclass(frozen=True)
class Calib:
    """几何标定常数（真源 = resources/calibration/gate0.json，三源分立裁定：
    游戏事实 RULES / 几何标定 gate0 / 行为参数 decision——本类只是文件载体的形状，
    禁止在此写字面量默认值：写了就是第二份真相）。

    字段口径见 gate0.json 的 _notes；来源与误差注记见本模块 docstring 与 CODE_WIKI。
    """

    vpx: float        # 消失点 x
    y_h: float        # 地平线行（2D 尺子口径）
    focal_px: float   # 线角几何的全幅相机焦距（≠ 地面投影焦距 GROUND_FOCAL_PX）
    a_x: float        # 横向尺子：归一量 → 车道单位（保险杠机位重标）
    min_denom: float  # 归一适用域下限：分母 (cy − y_h) 低于此 → 横向置空
    lane_w_m: float   # 一条车道的米制宽（车道量 ↔ 米制换算锚；出处见 _notes）


def _require(d: dict, key: str) -> float:
    v = d.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"gate0.json 键「{key}」缺失或非数值：{v!r}")
    return float(v)


def _read_gate0(path) -> Calib:
    """gate0.json 的实际解析（fail-loud：缺键/非数值/依赖矛盾即拒载并写明原因）。
    与 load_calib 拆开只为测试可注入临时文件；进程读法只有带缓存的那一个。"""
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("schema_version") != 1:
        raise ValueError(f"gate0.json schema_version 非 1：{d.get('schema_version')!r}")
    cal = Calib(
        vpx=_require(d, "vpx"), y_h=_require(d, "y_h"),
        focal_px=_require(d, "focal_px"), a_x=_require(d, "a_x"),
        min_denom=_require(d, "min_denom"), lane_w_m=_require(d, "lane_w_m"))
    # 依赖矛盾检查：尺子分母与换算锚都不能退化
    if not (cal.a_x > 0 and cal.min_denom > 0 and cal.vpx > 0
            and cal.lane_w_m > 0):
        raise ValueError(f"gate0.json 数值非法：{cal}")
    return cal


@lru_cache(maxsize=1)
def load_calib() -> Calib:
    return _read_gate0(GATE0_FILE)
