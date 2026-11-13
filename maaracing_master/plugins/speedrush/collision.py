# -*- coding: utf-8 -*-
"""正面撞车事件记录：YOLO 街车锚点纵深贴车头且横向体积重叠。

**它是什么**：只读观测器，每拍吃对象表（``object_table.ObjectTable``）的轨道，
对 **kind==车、纵深来自锚点（metric）、且在自车正前方** 的轨道做一次边沿判定：
纵深首次落进 ``[0, z_max_m]`` 米即记一条事件。事件按轨道 id 去重，同一辆车
在阶段内只记一次——这是"撞了哪几辆"，不是逐拍近距流水。

**为什么是纵深 + 类别 + 正面，不是真碰撞**：游戏日志与 HUD 都没有碰撞事件
（RULES §4.3 只规定碰撞损失时间、不扣分），能程序化拿到的只有观测量。
「YOLO 标成街车 + 锚点纵深贴到车头」是撞车位置的上界代理；再加一条横向体积门
把「车当成点」的误记挡掉：自车与目标各自按半宽展开（目标优先用框宽实测半宽，
缺失回落 ``car_half_m``），再加 ``margin_m``，只有横向包络重叠才算正面。
**框高粗距（``metric_coarse``）不进本层**：近场框被自车车身/画面截断，粗距正是
最不可靠的一档；宁可漏记，也不拿不可信的纵深造事件。

**纵深口径**：``TrackObj.z_m`` 来自米制车链。锚点纵深是框底行（车尾接地）几何，
2 米以下因几何失效被拒——所以 ``z_max_m`` 是「最后一次可信锚点」的上界窗口，
不是物理间隙；真实接触到 0 米时仪器已看不见。负纵深=已过自车车头，按掠过不记。
"""
from __future__ import annotations

from dataclasses import dataclass

from maaracing_master.plugins.speedrush.object_table import TrackObj
from maaracing_master.plugins.speedrush.world_model import KIND_CAR

SRC_ANCHOR = "metric"     # 锚点纵深（ObjectTable：非 BOX_SIZE 的米制源）


@dataclass(frozen=True)
class CollisionEvent:
    """一条正面撞车事件（一次性；按轨道 id 去重）。"""

    track_id: int
    fid: int                 # 首次落入近距带的拍号
    z_m: float               # 该拍纵深（米，正=车头前方）
    x_m: float               # 米制横位（相对自车中心）
    x_clear_m: float         # 横向包络余量（≤0=两车横向已重叠）
    half_w_m: float | None   # 目标半宽（实测；None=用标称半宽）
    src: str                 # 纵深来源（metric）
    conf: float              # 该轨道本拍置信
    age_ms: float            # 该轨道观测新鲜度（距最后一次真实读数）


class CollisionObserver:
    """正面撞车事件观察器（有状态：按轨道 id 去重，阶段切换重建）。

    ``z_max_m`` 是近距带上限（米）。判据：kind==车、纵深来自锚点、``0 <= z <=
    z_max_m``、有米制横位，且 ``|x_m| <= ego_half_m + car_half_m + margin_m``
    （目标半宽优先用 ``TrackObj.half_w_m``，缺失回落 ``car_half_m``）。无米制
    横位的轨道（多为框高粗距条目）无法证明在正面，不记。
    """

    def __init__(self, *, z_max_m: float, ego_half_m: float,
                 car_half_m: float, margin_m: float) -> None:
        if z_max_m <= 0:
            raise ValueError(f"z_max_m 必须 > 0：{z_max_m}")
        if ego_half_m < 0 or car_half_m < 0 or margin_m < 0:
            raise ValueError(
                f"半宽/余量不得为负：{ego_half_m}/{car_half_m}/{margin_m}")
        self.z_max_m = float(z_max_m)
        self.ego_half_m = float(ego_half_m)
        self.car_half_m = float(car_half_m)
        self.margin_m = float(margin_m)
        self._fired: set[int] = set()

    def reset(self) -> None:
        self._fired.clear()

    @property
    def fired_ids(self) -> frozenset[int]:
        """本阶段已记录过事件的轨道 id（观测面/测试用）。"""
        return frozenset(self._fired)

    def update(self, tracks: "tuple[TrackObj, ...] | list[TrackObj]",
               fid: int) -> tuple[CollisionEvent, ...]:
        """推进一拍，返回本拍**新触发**的事件（同 id 只触发一次）。"""
        out: list[CollisionEvent] = []
        for t in tracks:
            if t.kind != KIND_CAR or t.id in self._fired:
                continue
            z = t.z_m
            if t.src != SRC_ANCHOR or z is None \
                    or not (0.0 <= z <= self.z_max_m):
                continue
            x = t.x_m
            if x is None:
                continue                  # 无米制横位：证明不了在正面
            half_w = t.half_w_m
            car_half = self.car_half_m if half_w is None else half_w
            x_clear = abs(x) - (self.ego_half_m + car_half + self.margin_m)
            if x_clear > 0.0:
                continue                  # 横向体积不重叠：不是正面
            self._fired.add(t.id)
            out.append(CollisionEvent(
                track_id=t.id, fid=fid, z_m=z, x_m=x, x_clear_m=x_clear,
                half_w_m=half_w, src=t.src, conf=t.conf, age_ms=t.age_ms))
        return tuple(out)
