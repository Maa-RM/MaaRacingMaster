# -*- coding: utf-8 -*-
"""正面撞车事件记录：YOLO 街车锚点纵深贴车头、横向体积重叠且仍在接近。

**它是什么**：只读观测器，每拍吃对象表（``object_table.ObjectTable``）的轨道，
对 **kind==车、纵深来自锚点（metric）、在自车正前方、且相对纵深仍在接近** 的
轨道做一次边沿判定：纵深首次落进 ``[0, z_max_m]`` 米时先挂一条候审事件，
经过 ``pass_window_s`` 秒观察窗后才落定。落定规则：
- 同一 track 在窗内产生 **pass**（超越完成，含推定超越）→ 这次是擦过/超车，
  不记（选项 3：按 pass 事后否决）；
- 窗口内纵深明显回升（``recede_m``）或接近证据消失（闭速 ≤0 且相对速率 ≤0）→
  判为「在拉远」，不记（只收靠近的）；
- 其余落定为一次正面撞车事件；track 在窗口内直接消失（无 pass）也立即落定。

**为什么是纵深 + 类别 + 正面 + 接近**：游戏日志与 HUD 都没有碰撞事件
（RULES §4.3 只规定碰撞损失时间、不扣分），能程序化拿到的只有观测量。
「YOLO 街车 + 锚点纵深贴车头 + 横向体积重叠」是撞车位置的上界代理；再加
「仍在接近」把定速并行/被甩开的那一档挡掉。**框高粗距（``metric_coarse``）不进
本层**：近场框被自车车身/画面截断，粗距正是最不可靠的一档；宁可漏记，也不拿
不可信的纵深造事件。

**纵深口径**：``TrackObj.z_m`` 来自米制车链。锚点纵深是框底行（车尾接地）几何，
2 米以下因几何失效被拒——所以 ``z_max_m`` 是「最后一次可信锚点」的上界窗口，
不是物理间隙；真实接触到 0 米时仪器已看不见。负纵深=已过自车车头，按掠过不记。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from maaracing_master.plugins.speedrush.object_table import TrackObj
from maaracing_master.plugins.speedrush.world_model import KIND_CAR

SRC_ANCHOR = "metric"     # 锚点纵深（ObjectTable：非 BOX_SIZE 的米制源）


@dataclass(frozen=True)
class CollisionEvent:
    """一条落定的正面撞车事件（一次性；按轨道 id 去重）。"""

    track_id: int
    fid: int                 # 首次落入近距带的拍号（候选触发拍）
    settle_fid: int          # 观察窗落定拍号（pass 否决/拉远否决在此判定）
    z_m: float               # 触发拍纵深（米，正=车头前方）
    x_m: float               # 触发拍米制横位（相对自车中心）
    x_clear_m: float         # 横向包络余量（≤0=两车横向已重叠）
    half_w_m: float | None   # 目标半宽（实测；None=用标称半宽）
    src: str                 # 纵深来源（metric）
    conf: float              # 触发拍置信
    age_ms: float            # 触发拍观测新鲜度
    v_close_mps: float | None  # 触发拍闭速（正=接近；None=估计窗未开）
    rel_approach: float      # 触发拍相对接近率（px/fid，正=接近）


@dataclass
class _Pending:
    """候审事件（窗口内可被 pass / 拉远否决）。"""

    event: CollisionEvent
    ts_ns: int


class CollisionObserver:
    """正面撞车事件观察器（有状态：挂起窗 + 按轨道 id 去重，阶段切换重建）。

    判据：kind==车、纵深来自锚点、``0 <= z <= z_max_m``、有米制横位、
    ``|x_m| <= ego_half_m + car_half_m + margin_m``（目标半宽优先用
    ``TrackObj.half_w_m``，缺失回落 ``car_half_m``）、且仍在接近
    （有闭速看闭速 >0，否则看 ``rel_approach`` >0）。
    ``pass_window_s`` 是候审窗：窗内同 track 产生 pass 即否决；``recede_m``
    是判「拉远」的纵深回升量。
    """

    def __init__(self, *, z_max_m: float, ego_half_m: float,
                 car_half_m: float, margin_m: float,
                 pass_window_s: float, recede_m: float) -> None:
        if z_max_m <= 0:
            raise ValueError(f"z_max_m 必须 > 0：{z_max_m}")
        if ego_half_m < 0 or car_half_m < 0 or margin_m < 0:
            raise ValueError(
                f"半宽/余量不得为负：{ego_half_m}/{car_half_m}/{margin_m}")
        if pass_window_s < 0 or recede_m < 0:
            raise ValueError(
                f"pass_window_s/recede_m 不得为负：{pass_window_s}/{recede_m}")
        self.z_max_m = float(z_max_m)
        self.ego_half_m = float(ego_half_m)
        self.car_half_m = float(car_half_m)
        self.margin_m = float(margin_m)
        self.pass_window_s = float(pass_window_s)
        self.recede_m = float(recede_m)
        self._seen: set[int] = set()          # 已触发过（含被否决）的轨道 id
        self._pending: dict[int, _Pending] = {}

    def reset(self) -> None:
        self._seen.clear()
        self._pending.clear()

    @property
    def fired_ids(self) -> frozenset[int]:
        """本阶段触发过事件的轨道 id（含被 pass/拉远否决的）。"""
        return frozenset(self._seen)

    @property
    def pending_ids(self) -> frozenset[int]:
        """仍在候审窗内的轨道 id（观测面/测试用）。"""
        return frozenset(self._pending)

    def update(self, tracks: "tuple[TrackObj, ...] | list[TrackObj]",
               fid: int, *, ts_ns: int,
               pass_ids: "set[int] | frozenset[int] | tuple[int, ...]" = ()
               ) -> tuple[CollisionEvent, ...]:
        """推进一拍，返回本拍**落定**的事件（同 id 只落一条）。"""
        by_id = {t.id: t for t in tracks if t.kind == KIND_CAR}
        out: list[CollisionEvent] = []
        # 1) pass 否决：同 track 在窗内产生 pass = 擦过/超车，不是撞车
        for tid in set(pass_ids):
            if tid in self._pending:
                self._pending.pop(tid, None)
        # 2) 已有候审：消失/拉远/接近证据消失/窗满，逐条落定
        for tid, p in list(self._pending.items()):
            t = by_id.get(tid)
            if t is None:
                out.append(replace(p.event, settle_fid=fid))
                self._pending.pop(tid)
                continue
            z = t.z_m
            if (z is not None and z > p.event.z_m + self.recede_m) \
                    or not self._approaching(t):
                self._pending.pop(tid)      # 拉远/不再接近：否决
                continue
            if (ts_ns - p.ts_ns) / 1e9 >= self.pass_window_s:
                out.append(replace(p.event, settle_fid=fid))
                self._pending.pop(tid)
        # 3) 新触发：挂起，等观察窗落定
        for t in by_id.values():
            if t.id in self._seen:
                continue
            if not self._trigger_ok(t):
                continue
            z_m, x_m = t.z_m, t.x_m
            if z_m is None or x_m is None:
                continue            # 观测门缺数不挂起（与 _trigger_ok 同口径，这里只做收窄）
            self._seen.add(t.id)
            self._pending[t.id] = _Pending(
                event=CollisionEvent(
                    track_id=t.id, fid=fid, settle_fid=fid, z_m=z_m,
                    x_m=x_m, x_clear_m=self._x_clear(x_m, t.half_w_m),
                    half_w_m=t.half_w_m, src=t.src, conf=t.conf,
                    age_ms=t.age_ms, v_close_mps=t.v_close_mps,
                    rel_approach=t.rel_approach),
                ts_ns=ts_ns)
        return tuple(out)

    def _x_clear(self, x_m: float, half_w_m: float | None) -> float:
        car_half = self.car_half_m if half_w_m is None else half_w_m
        return abs(x_m) - (self.ego_half_m + car_half + self.margin_m)

    def _trigger_ok(self, t: TrackObj) -> bool:
        if t.src != SRC_ANCHOR:
            return False
        z = t.z_m
        if z is None or not (0.0 <= z <= self.z_max_m):
            return False
        if t.x_m is None:
            return False
        if self._x_clear(t.x_m, t.half_w_m) > 0.0:
            return False
        return self._approaching(t)

    @staticmethod
    def _approaching(t: TrackObj) -> bool:
        """仍在接近：闭速优先，缺闭速看跟踪层相对接近率。"""
        if t.v_close_mps is not None:
            return t.v_close_mps > 0.0
        return t.rel_approach > 0.0
