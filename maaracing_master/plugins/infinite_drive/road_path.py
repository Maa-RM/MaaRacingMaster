# -*- coding: utf-8 -*-
"""沿已观测的街车纵深选一段可达行车线。横坐标以道宽计，纵坐标以米计。"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor

from maaracing_master.plugins.infinite_drive.ranges import MID_MAX_M, NEAR_MAX_M
from maaracing_master.plugins.infinite_drive.yellow_line import ROAD_HALF_LANES

_STEP_M = 8
_HORIZON_M = int(MID_MAX_M) + _STEP_M
_ROAD_MARGIN = 0.32
_GRID_LIMIT = int(10 * (ROAD_HALF_LANES - _ROAD_MARGIN))
_CANDIDATES = tuple(i / 10 for i in range(-_GRID_LIMIT, _GRID_LIMIT + 1))
_MAX_SHIFT = 0.03 * _STEP_M  # 相邻纵深切片的横移上限，与车头角限幅同量级。
_CLEARANCE = 0.60
_PASS_EDGE = 0.85
_PASS_OFFSET = 0.20
_PASS_TOLERANCE = 0.15
_FRONT_BUFFER_M = 5.0
_REAR_BUFFER_M = 7.0
LOOKAHEAD_M = 12.0


@dataclass(frozen=True)
class RoadPath:
    points: tuple[tuple[float, float], ...]
    throttle: float

    def x_at(self, distance: float) -> float:
        """前视采样，到达路线末端后保持最后的横位。"""
        for (near, x0), (far, x1) in zip(self.points, self.points[1:]):
            if near <= distance <= far:
                return x0 + (x1 - x0) * (distance - near) / (far - near)
        return self.points[-1][1]


def _pass_reward(passing_x: float, car_x: float) -> float:
    """只奖励目标车所在道的相邻白线外侧，路缘没有对侧车道。"""
    car_lane = floor(car_x)
    our_lane = floor(passing_x)
    if abs(car_lane - our_lane) != 1 or abs(passing_x - car_x) > _PASS_EDGE + 1e-9:
        return 0.0
    boundary = max(car_lane, our_lane)
    point = boundary + (_PASS_OFFSET if our_lane > car_lane else -_PASS_OFFSET)
    # 道宽内预留跟踪误差；实际可经过的横位仍由整段车身避让裁决。
    error = abs(passing_x - point)
    return 0.65 * max(0.0, 1.0 - error / _PASS_TOLERANCE)


def _edge_cost(x0: float, x1: float, near: float, far: float,
               cars: list[tuple[float, float]]) -> float | None:
    shift = x1 - x0
    if abs(shift) > _MAX_SHIFT:
        return None
    slope = shift / (far - near)
    cost = 1.4 * shift * shift
    for car_x, car_z in cars:
        enter = max(near, car_z - _FRONT_BUFFER_M)
        leave = min(far, car_z + _REAR_BUFFER_M)
        if enter > leave:
            continue
        left, right = sorted((x0 + slope * (enter - near), x0 + slope * (leave - near)))
        # 检查整段扫过的区域，防止两个切片端点都空、连线却切进车身。
        if left - _CLEARANCE + 1e-9 < car_x < right + _CLEARANCE - 1e-9:
            return None
        if near < car_z <= far:
            passing_x = x0 + slope * (car_z - near)
            cost -= _pass_reward(passing_x, car_x)
    return cost


def plan_road_path(ego: float, home: float, cars: list[tuple[float, float]],
                   previous: float | None = None) -> RoadPath:
    """比较近场空档。先走到可达的最远处，再优先贴车且横移少的路线。"""
    traffic = [(x, z) for x, z in cars if abs(x) < ROAD_HALF_LANES and 0 < z <= _HORIZON_M + _FRONT_BUFFER_M]
    positions = set(_CANDIDATES)
    if abs(ego) <= ROAD_HALF_LANES - _ROAD_MARGIN:
        positions.add(ego)
    candidates = sorted(positions)
    states: dict[float, tuple[float, tuple[tuple[float, float], ...]]] = {ego: (0.0, ((0.0, ego),))}
    for far in range(_STEP_M, _HORIZON_M + 1, _STEP_M):
        near = far - _STEP_M
        obstacles = [(x, z) for x, z in traffic if z - _FRONT_BUFFER_M <= far and z + _REAR_BUFFER_M >= near]
        next_states: dict[float, tuple[float, tuple[tuple[float, float], ...]]] = {}
        for x1 in candidates:
            bias = 0.025 * abs(x1 - home)
            if far == _STEP_M and previous is not None:
                bias += 0.15 * abs(x1 - previous)
            for x0, (cost, points) in states.items():
                edge = _edge_cost(x0, x1, near, far, obstacles)
                if edge is None:
                    continue
                total = cost + edge + bias
                if x1 not in next_states or total < next_states[x1][0]:
                    next_states[x1] = total, (*points, (float(far), x1))
        if not next_states:
            break
        states = next_states

    _, points = min(states.values(), key=lambda state: state[0])
    throttle = min(1.0, points[-1][0] / (2.0 * NEAR_MAX_M))
    return RoadPath(points, throttle)
