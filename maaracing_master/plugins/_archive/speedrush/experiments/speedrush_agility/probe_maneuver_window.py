#!/usr/bin/env python
"""机动时间尺度 × 机会窗离线复算（speedrush）。

**问什么**：驾驶侧"动作慢两倍"是不是机会窗的主要压缩因素，以及"挡道即躲避"
与"到站时间才躲避"两种触发语义的覆盖差。

**输入**：控制局 trace（每个会话一个 speedrush.jsonl）。只读；不 import
业务代码，运动学与选择层几何都在本文件内最小自建。

**两档运动学**（标尺独立于待验证实现）：
- slow = 现役闭环律（planner.lane_move_time 同式：k_lat=1.2 / tau=0.25 /
  heading_max=0.15 / v=41 m/s），与生产逐位同口径，用来复现今天的行为边界。
- fast = 游戏官方紧急档（RULES §4.3.1：横向加速度上限 10 m/s²、紧急横速
  5 m/s）。这不是调参，是用权威口径给"玩家能做多快"定上界。

**几何**：贴边目标与拒绝次序照抄 selection._pass_x / _pick_pass 的现行口径
（gap 0.68 道、缘余量 0.35 道、min_gap 0.6 道、半宽和 0.54 道），保证两档
比的是同一批候选、只差执行时间。

用法：``.venv/Scripts/python.exe -X utf8 probe_maneuver_window.py <trace.jsonl ...>``
"""

from __future__ import annotations

import argparse
import collections
import json
import math

LANE_W = 3.5
REACTION_S = 0.13
MARGIN_S = 0.15
NEED_PAD_S = REACTION_S + MARGIN_S
HALF_LANE = 0.54          # 两车半宽和（0.27 + 0.27）
REACH_LANE = 1.3          # 邻道候选上限
HORIZON_S = 4.5           # 擦边视界
HOLD_S = 0.5              # 并排保持窗（t >= -0.5）
LEAN_MIN_SLOW = 2.0       # 现役邻道到站门
LEAN_MIN_FAST = 1.0       # 快档下的等效门（k=3 到位 0.64s + 0.28 余量 ≈ 0.92s）
FAST_K_LAT = 3.0          # 快档带宽（sim_agility.py 扫描结果）
GRAZE_GAP = 0.68
WALL_MARGIN = 0.35
MIN_GAP = 0.6
NEAR_Z_M = 30.0           # "近距挡道"的米制口径
NEAR_CY = 600             # 近距挡道的像素口径（720 − near_cy_margin 120）


def t_closed_loop(dx_lane: float, k: float = 1.2, tau: float = 0.25,
                  hmax: float = 0.15, settle: float = 0.1,
                  v: float = 41.0, dt: float = 0.02) -> float:
    """现役闭环律的到位时间（与 planner.lane_move_time 同式）。"""
    e = abs(dx_lane)
    if e <= settle:
        return 0.0
    a = 1.0 - math.exp(-dt / tau)
    psi, t = 0.0, 0.0
    for _ in range(int(10.0 / dt)):
        psi_des = math.atan(k * e * LANE_W / v)
        psi_des = max(-hmax, min(hmax, psi_des))
        psi += a * (psi_des - psi)
        e -= (v / LANE_W) * math.tan(psi) * dt
        t += dt
        if e <= settle:
            return t
    return math.inf


def t_emergency(dx_lane: float, a: float = 10.0, vmax: float = 5.0,
                settle: float = 0.1) -> float:
    """快档到位时间：与实现同式（闭环律 k_lat=FAST_K_LAT），保持决策/执行同一模型。"""
    return t_closed_loop(dx_lane, k=FAST_K_LAT)


def pass_target(cx: float, left: float | None, right: float | None) -> float | None:
    """贴边目标横位（selection._pass_x 同式：选空间大的一侧 + 缘夹取 + min_gap）。"""
    if left is None or right is None:
        return None
    room_l = cx - left
    room_r = right - cx
    side = 1.0 if room_r >= room_l else -1.0
    lo, hi = left + WALL_MARGIN, right - WALL_MARGIN
    x = max(lo, min(hi, cx + side * GRAZE_GAP))
    if abs(x - cx) < MIN_GAP:
        x = max(lo, min(hi, cx - side * MIN_GAP))
        if abs(x - cx) < MIN_GAP:
            return None
    return x


def _car_rows(dbg: dict, otab: dict | None,
              car_pos: list | None) -> list[dict]:
    """把 sel_dbg.car（x, t）与 otab.t 的 (safe, z)、car_pos 的 cy 按横位配对。

    otab.t 行是 14 元组：[kind, id, x_lane, x_m, z_m, age_ms, seen,
    safety_ok, safety_why, src, src_age_ms, v_close, v_lat, line]。
    sel_dbg.car 的 x 是道路帧坐标（x_lane + 自车横位），故按
    ``x_lane + ego ≈ car.x`` 配对；car_pos 的 x 同源。
    """
    cars = dbg.get("car") or []
    ego = dbg.get("ego") or 0.0
    pool = []
    for row in (otab or {}).get("t") or []:
        try:
            if row[0] != "car" or row[2] is None:
                continue
            pool.append({"x": float(row[2]), "z": row[4], "safe": bool(row[7])})
        except (TypeError, ValueError, IndexError):
            continue
    pos = []
    for row in car_pos or []:
        try:
            pos.append({"x": float(row["x"]), "cy": row.get("cy")})
        except (TypeError, ValueError, KeyError):
            continue
    out = []
    for c in cars:
        x, t = c[0], c[1]
        match = None
        for i, o in enumerate(pool):
            if abs(o["x"] + ego - x) < 0.02:
                match = pool.pop(i)
                break
        cy = None
        for i, o in enumerate(pos):
            if abs(o["x"] + ego - x) < 0.02:
                cy = pos.pop(i)["cy"]
                break
        out.append({"x": x, "t": t,
                    "z": None if match is None else match["z"],
                    "cy": cy,
                    "safe": True if match is None else match["safe"]})
    return out


def analyze(path: str) -> dict:
    n = collections.Counter()
    slack_slow, slack_fast = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            dbg = r.get("sel_dbg")
            if not dbg:
                continue
            ego = dbg.get("ego")
            if ego is None:
                continue
            left, right = dbg.get("left"), dbg.get("right")
            cars = _car_rows(dbg, r.get("otab"), r.get("car_pos"))
            n["ticks"] += 1
            ok = {"s2": False, "s1": False, "f2": False, "f1": False}
            for c in cars:
                dx = abs(c["x"] - ego)
                if dx > REACH_LANE or not c["safe"]:
                    continue
                t = c["t"]
                if t is None or not (-HOLD_S <= t <= HORIZON_S):
                    continue
                adjacent = dx > HALF_LANE
                px = pass_target(c["x"], left, right)
                if px is None or abs(px - c["x"]) < HALF_LANE:
                    n["rej_edge"] += 1
                    continue
                need_slow = (0.0 if abs(px - ego) <= 0.1
                             else t_closed_loop(px - ego)) + NEED_PAD_S
                need_fast = (0.0 if abs(px - ego) <= 0.1
                             else t_emergency(px - ego)) + NEED_PAD_S
                # 四个组合：{slow, fast} × {邻道门 2.0s, 1.0s}
                for key, need, gate in (
                        ("s2", need_slow, LEAN_MIN_SLOW),
                        ("s1", need_slow, LEAN_MIN_FAST),
                        ("f2", need_fast, LEAN_MIN_SLOW),
                        ("f1", need_fast, LEAN_MIN_FAST)):
                    if adjacent and t < gate:
                        continue
                    if t > 0.0 and need > t:
                        continue
                    ok[key] = True
                if adjacent and t < LEAN_MIN_SLOW:
                    n["lane_blocked_slow"] += 1
                if adjacent and LEAN_MIN_FAST <= t < LEAN_MIN_SLOW:
                    n["lane_window_fast"] += 1
                slack_slow.append(t - need_slow)
                slack_fast.append(t - need_fast)
                if ok["f1"] and not ok["s2"]:
                    n["cars_only_fast"] += 1
            for key in ("s2", "s1", "f2", "f1"):
                if ok[key]:
                    n["ticks_" + key + "_ok"] += 1
            if ok["f1"] and not ok["s2"]:
                n["ticks_only_fast"] += 1
            if ok["f2"] and not ok["s2"]:
                n["ticks_fast_kin_only"] += 1
            if ok["s1"] and not ok["s2"]:
                n["ticks_gate_only"] += 1
            # H2：挡道即躲避（同道 + 近距；t 只作分级）。
            # 近距证据 = 米制 z（锚/框高）或框底行 cy（像素近场口径 near_cy）。
            same = [c for c in cars if abs(c["x"] - ego) <= HALF_LANE]
            if same:
                n["ticks_with_same"] += 1
                cur = any(c["t"] is not None and -HOLD_S <= c["t"] <= HORIZON_S
                          for c in same)
                new = any((c["z"] is not None and c["z"] <= NEAR_Z_M)
                          or (c["cy"] is not None and c["cy"] >= NEAR_CY)
                          or (c["t"] is not None and c["t"] <= HORIZON_S)
                          for c in same)
                near = [c for c in same
                        if (c["z"] is not None and c["z"] <= NEAR_Z_M)
                        or (c["cy"] is not None and c["cy"] >= NEAR_CY)]
                if near:
                    n["ticks_same_near"] += 1
                    if all(c["t"] is None for c in near):
                        n["ticks_same_near_all_t_none"] += 1
                n["same_covered_cur"] += int(cur)
                n["same_covered_new"] += int(new)
                if new and not cur:
                    n["same_only_new"] += 1
                    if any(c["t"] is None for c in near):
                        n["same_only_new_t_none"] += 1
    return {"ticks": n["ticks"], "counters": dict(n),
            "slack_slow": slack_slow, "slack_fast": slack_fast}


def q(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--out")
    args = ap.parse_args()
    total = collections.Counter()
    sl_all, sl_fast_all = [], []
    for path in args.traces:
        res = analyze(path)
        c = collections.Counter(res["counters"])
        total.update(c)
        sl_all += res["slack_slow"]
        sl_fast_all += res["slack_fast"]
        t = c["ticks"] or 1
        print(f"== {path}")
        print(f"   拍数 {c['ticks']}  有候选拍：slow+2s {c['ticks_s2_ok']} "
              f"({100*c['ticks_s2_ok']/t:.1f}%) / slow+1s {c['ticks_s1_ok']} "
              f"({100*c['ticks_s1_ok']/t:.1f}%) / fast+2s {c['ticks_f2_ok']} "
              f"({100*c['ticks_f2_ok']/t:.1f}%) / fast+1s {c['ticks_f1_ok']} "
              f"({100*c['ticks_f1_ok']/t:.1f}%)")
        print(f"   新增拍：仅快运动学 {c['ticks_fast_kin_only']} / 仅门放宽 "
              f"{c['ticks_gate_only']} / 组合 {c['ticks_only_fast']}")
        print(f"   邻道被 2s 门拦 {c['lane_blocked_slow']} 车次；"
              f"邻道在 1~2s 窗内的车次 {c['lane_window_fast']}")
        print(f"   挡道覆盖：现规则 {c['same_covered_cur']} / 新规则 "
              f"{c['same_covered_new']}（新增 {c['same_only_new']} 拍，"
              f"其中 t=None 车 {c['same_only_new_t_none']}）")
        print(f"   同道近距拍 {c['ticks_same_near']}，其中全部车算不出到站时间 "
              f"{c['ticks_same_near_all_t_none']}")
    t = total["ticks"] or 1
    print("== 合计")
    print(f"   拍数 {total['ticks']}")
    print(f"   有候选拍：slow+2s {total['ticks_s2_ok']} ({100*total['ticks_s2_ok']/t:.1f}%)"
          f" / slow+1s {total['ticks_s1_ok']} ({100*total['ticks_s1_ok']/t:.1f}%)"
          f" / fast+2s {total['ticks_f2_ok']} ({100*total['ticks_f2_ok']/t:.1f}%)"
          f" / fast+1s {total['ticks_f1_ok']} ({100*total['ticks_f1_ok']/t:.1f}%)")
    print(f"   新增拍：仅快运动学 {total['ticks_fast_kin_only']} / 仅门放宽 "
          f"{total['ticks_gate_only']} / 组合 {total['ticks_only_fast']}")
    print(f"   邻道 2s 门拦截车次 {total['lane_blocked_slow']}；1~2s 窗内车次 "
          f"{total['lane_window_fast']}")
    print(f"   挡道覆盖：现 {total['same_covered_cur']} / 新 {total['same_covered_new']} / "
          f"新增拍 {total['same_only_new']}（t=None 车 {total['same_only_new_t_none']}）")
    print(f"   同道近距拍 {total['ticks_same_near']}，其中全部车算不出到站时间 "
          f"{total['ticks_same_near_all_t_none']}")
    print(f"   候选余量 t-need（负=来不及）：慢档 P50 {q(sl_all,.5)} P25 {q(sl_all,.25)}；"
          f"快档 P50 {q(sl_fast_all,.5)} P25 {q(sl_fast_all,.25)}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"counters": dict(total),
                       "slack_slow": {"p50": q(sl_all, .5), "p25": q(sl_all, .25)},
                       "slack_fast": {"p50": q(sl_fast_all, .5), "p25": q(sl_fast_all, .25)}},
                      f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
