"""Pass/graze 优先级离线校验（speedrush）。

**问什么**：在程序自己的局里，选择层处在 center/commit 的拍上，是否已经存在
一个按现役几何与慢档运动学都可行的贴边候选？如果有，把 pass/graze 提到
center/commit 之前会不会有得分收益、目标道是否被占（碰撞代价）。

**几何/运动学**：直接复用 ``probe_maneuver_window`` 的现行口径
（0.68 道贴边、缘余量 0.35、min_gap 0.6、半宽和 0.54、慢档闭环），
只多两件事：

1. 按当前选择 ``sel_why`` 把可行拍分成「已选 pass/graze」与「仍选
   center/commit」；
2. 对「未选」的候选检查贴边目标道上有无第二辆车（横距 < 半宽和，且它的
   到站时间落在本次横移完成前 +0.5s），作为该优先级改动的碰撞风险上界。

用法::

    .venv/Scripts/python.exe -X utf8 probe_pass_priority.py <trace.jsonl ...>
"""

from __future__ import annotations

import argparse
import collections
import json

import probe_maneuver_window as pm

GATE_M = 1.2          # 现役邻道到站门（decision.json pass_lean_min_s）
OCC_PAD_S = 0.5       # 目标道占用检查的额外时间余量


def pass_target_side(cx: float, side: float,
                     left: float | None, right: float | None) -> float | None:
    """邻道贴边目标：生产 `_pass_x_side` 同式，取靠自车一侧。"""
    if left is None or right is None:
        return None
    lo, hi = left + pm.WALL_MARGIN, right - pm.WALL_MARGIN
    x = max(lo, min(hi, cx + side * pm.GRAZE_GAP))
    if abs(x - cx) < pm.MIN_GAP:
        x = max(lo, min(hi, cx - side * pm.MIN_GAP))
        if abs(x - cx) < pm.MIN_GAP:
            return None
    return x


def analyze(path: str, gate: float) -> collections.Counter:
    n: collections.Counter = collections.Counter()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            dbg = rec.get("sel_dbg")
            if not dbg:
                continue
            ego = dbg.get("ego")
            left, right = dbg.get("left"), dbg.get("right")
            if ego is None or left is None or right is None:
                continue
            cars = pm._car_rows(dbg, rec.get("otab"), rec.get("car_pos"))
            best = None
            for car in cars:
                dx = abs(car["x"] - ego)
                if dx > pm.REACH_LANE or not car["safe"]:
                    continue
                if dx <= pm.HALF_LANE:
                    continue
                t = car["t"]
                if t is None or not (-pm.HOLD_S <= t <= pm.HORIZON_S):
                    continue
                if t < gate:
                    n["rejected_inside_gate"] += 1
                    continue
                side = 1.0 if ego >= car["x"] else -1.0
                px = pass_target_side(car["x"], side, left, right)
                if px is None or abs(px - car["x"]) < pm.HALF_LANE:
                    n["rejected_edge"] += 1
                    continue
                need = (0.0 if abs(px - ego) <= 0.1
                        else pm.t_closed_loop(px - ego)) + pm.NEED_PAD_S
                if t > 0 and need > t:
                    n["rejected_slow_margin"] += 1
                    continue
                if any(o is not car and abs(o["x"] - px) < pm.HALF_LANE
                       and (o["t"] is None or o["t"] <= need + OCC_PAD_S)
                       for o in cars):
                    n["rejected_blocked"] += 1
                    continue
                slack = t - need
                if best is None or slack < best["slack"]:
                    best = {"car": car, "px": px, "need": need,
                            "slack": slack, "t": t}

            if best is None:
                continue
            n["feasible_ticks"] += 1
            sel = rec.get("sel_why") or ""
            if sel in ("pass", "graze"):
                n["chosen_pass"] += 1
            elif sel.startswith("center") or sel == "commit":
                n["missed_center"] += 1
            else:
                n["missed_other"] += 1
            n["slack_sum_ticks"] += 1
            n["slack_sum"] += best["slack"]
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--gate", type=float, default=GATE_M)
    args = ap.parse_args()
    total: collections.Counter = collections.Counter()
    for path in args.traces:
        n = analyze(path, args.gate)
        total.update(n)
        print(f"== {path}")
        print(f"   可行拍 {n['feasible_ticks']}：已选 pass/graze {n['chosen_pass']}"
              f" / 仍选 center/commit {n['missed_center']}"
              f" / 其他 {n['missed_other']}")
        print(f"   门内拒 {n['rejected_inside_gate']} / 贴边几何拒 "
              f"{n['rejected_edge']} / 慢档余量拒 {n['rejected_slow_margin']}"
              f" / 目标道占用拒 {n['rejected_blocked']}")
    n = total
    if n["feasible_ticks"]:
        print("== 合计")
        print(f"   可行拍 {n['feasible_ticks']}：已选 {n['chosen_pass']}"
              f" / 仍选 center/commit {n['missed_center']}"
              f" / 其他 {n['missed_other']}")
        print(f"   若把 pass/graze 提级，上限新增 {n['missed_center']} 拍；"
              f"目标道占用 {n['rejected_blocked']} 车次由安全门保留")
        print(f"   平均候选余量 {n['slack_sum'] / n['slack_sum_ticks']:.2f}s")


if __name__ == "__main__":
    main()
