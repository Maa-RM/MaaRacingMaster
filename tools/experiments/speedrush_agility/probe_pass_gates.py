"""Pass/graze 逐拍门位归因（speedrush，离线）。

**问什么**：生产 trace 里 `sel_why` 在 pass/graze 与 center/commit 之间闪断时，
每一拍到底是哪道门把候选拒了。用生产同一套函数（`_pick_pass` 的逐门重述）
对每拍车表逐车逐门记账，并与录制的 `sel_why` 对照——复现率低的拍单独列出
（输入重建缺口：safe/v_close/请求/承诺道不在 sel_dbg 里）。

输入口径：
- 车 x/t 取 `sel_dbg.car`（道，选择帧），身份取同序 `sel_dbg.opp.car`；
- `safe` / `near` / `v_close` 从 `scene.objs` 按 oid 对齐（grid 行 near 按
  z_mid≤16 重算，metric 行 near 按 z≤motion.near_z_m；v_close≈z/t 近似，
  桥接/稳健斜率修正不可见，alongside 窗因此可能偏小）；
- 承诺道中心取 `intent_lane`（路线目标道）；keep 态的 commit_x 未知，
  跨度门只在 committed 拍生效，keep 拍按无跨度记（与生产差异列入缺口）。

用法::

    .venv/Scripts/python.exe -X utf8 probe_pass_gates.py <trace.jsonl ...> \
        [--head 12 --tail 10] [--csv out.csv]
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from maaracing_master.plugins.speedrush import selection as S  # noqa: E402
from maaracing_master.plugins.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins.speedrush.world_model import load_calib  # noqa: E402


def _params():
    cfg = load_decision()
    s = cfg.selection
    pl = cfg.planner
    return S.SelectionParams(
        lane_w_m=load_calib().lane_w_m,
        horizon_s=s.horizon_s, max_waypoints=s.max_waypoints,
        graze_gap_lane=s.graze_gap_lane,
        center_band_lane=s.center_band_lane,
        opportunity_tol_lane=s.opportunity_tol_lane,
        k_lat_lane_s=pl.k_lat_lane_s,
        tau_heading_s=pl.tau_heading_s,
        heading_max_rad=pl.heading_max_rad,
        reaction_s=s.reaction_s, margin_s=s.margin_s,
        wall_margin_lane=s.wall_margin_lane,
        center_lookahead_m=s.center_lookahead_m,
        switch_dwell_s=s.switch_dwell_s,
        layer_dwell=s.layer_dwell, event_replan=s.event_replan,
        request_hold=cfg.route.request_hold,
        car_half_lane=s.car_half_lane, ego_half_lane=s.ego_half_lane,
        lane_clearance_lane=s.lane_clearance_lane,
        preposition_enabled=s.preposition_enabled,
        preposition_horizon_s=s.preposition_horizon_s,
        preposition_max_lane=s.preposition_max_lane,
        preposition_bias_lane=s.preposition_bias_lane,
        pass_preposition_enabled=s.pass_preposition_enabled,
        pass_hold_enabled=s.pass_hold_enabled,
        pass_hold_s=s.pass_hold_s,
        pass_min_gap_lane=s.pass_min_gap_lane,
        pass_in_commit_enabled=s.pass_in_commit_enabled,
        pass_horizon_s=s.pass_horizon_s,
        pass_graze_adjacent_enabled=s.pass_graze_adjacent_enabled,
        pass_no_surrender_enabled=s.pass_no_surrender_enabled,
        arrival_guard_enabled=s.arrival_guard_enabled,
        pass_reach_lane=s.pass_reach_lane,
        pass_lean_min_s=s.pass_lean_min_s,
        pass_clear_len_m=s.pass_clear_len_m,
        pass_min_closing_mps=s.pass_min_closing_mps,
        pass_alongside_cap_s=s.pass_alongside_cap_s,
        pass_latch_enabled=s.pass_latch_enabled)


def _road_from_dbg(d: dict, lw: float):
    left, right = d.get("left"), d.get("right")
    c0 = d.get("c0")
    if left is None or right is None or c0 is None:
        return None
    # 选择帧：走廊缘在 ego 平移后的坐标系；中心线按 c0 平带近似
    # （真实分带中心在弯道有偏移，列入重建缺口）。
    c = c0 * lw
    band = S.RoadBand(0.0, 30.0, c - 0.1, c + 0.1, "measured", 0.0, 1.0)
    return S.RoadFrame((band,), (right - left) * lw,
                       left * lw, right * lw, True)


def _cars_from_rec(rec: dict, d: dict, lw: float, near_z: float):
    cars = []
    rows = d.get("car") or []
    opp = (d.get("opp") or {}).get("car") or []
    meta = {}
    for o in (rec.get("scene") or {}).get("objs") or []:
        # [kind, x_m, t, conf, half_w?, z?, src, z_mid?, seen?, safe?, ...]
        if not o or o[0] != "car" or len(o) < 15:
            continue
        oid = o[14]
        z = o[5] if isinstance(o[5], (int, float)) else None
        zmid = o[7] if isinstance(o[7], (int, float)) else None
        safe = bool(o[9])
        if o[6] == "grid":
            near = zmid is not None and zmid <= 16.0
        else:
            near = z is not None and z <= near_z
        meta[oid] = (safe, near, z)
    for i, row in enumerate(rows):
        if i >= len(opp) or not opp[i]:
            continue
        oid = opp[i][0]
        x = row[0] * lw
        t = row[1]
        safe, near, z = meta.get(oid, (True, False, None))
        v_close = None
        if t is not None and t > 0.05 and z is not None:
            v_close = z / t
        cars.append(S.Obj("car", x, t, oid=oid, safe=safe, near=near,
                          v_close=v_close))
    return cars


def _gate_pass(c, others, road, ego, p, *, allow_adjacent: bool):
    """逐门重述 _pick_pass 单车判定；返回 (ok, why, x, gate)。"""
    half = (p.car_half_lane + p.ego_half_lane) * p.lane_w_m
    hold = p.pass_hold_s if p.pass_hold_enabled else 0.0
    horizon = p.pass_horizon_s if p.pass_horizon_s > 0 else p.horizon_s
    reach = p.pass_reach_lane * p.lane_w_m
    if not c.safe:
        return False, None, None, "unsafe"
    dx = abs(c.x - ego)
    if dx > (half if not p.pass_graze_adjacent_enabled
             else max(half, reach)):
        return False, None, None, "out_of_reach"
    if not ((c.t is not None and -hold <= c.t <= horizon)
            or (c.near and dx <= half)):
        return False, None, None, "no_time"
    t_eff = 0.0 if c.t is None else c.t
    adjacent = dx > half
    why = "graze" if adjacent else "pass"
    if adjacent and not (allow_adjacent and p.pass_graze_adjacent_enabled):
        return False, why, None, "adjacent_off"
    if adjacent and t_eff < p.pass_lean_min_s:
        return False, why, None, "lean_gate"
    alongside = S._pass_alongside_s(c.v_close, p)
    inflight = c.t is None or t_eff > 0.0 or not p.pass_hold_enabled
    if adjacent:
        one = S._pass_x_side(c, road, p, 1.0 if ego >= c.x else -1.0)
        xs = [] if one is None else [one]
    elif t_eff > 0.0:
        xs = S._pass_x_candidates(c, road, p)
    else:
        xs = [x for x in [S._pass_x(c, road, p)] if x is not None]
    if not xs:
        return False, why, None, "edge_geom"
    last = "edge_geom"
    for x in xs:
        if abs(x - c.x) < half:
            last = "edge_gap"
            continue
        if c.t is not None and inflight \
                and not S._reachable(x, ego, t_eff, p):
            if adjacent or not p.pass_no_surrender_enabled:
                last = "slow_margin"
                continue
        if (alongside > 0.0 or inflight) \
                and S._pass_window_blocked(others, x, t_eff, alongside, p):
            last = "window_blocked"
            continue
        if abs(x - ego) > p.aligned_lane * p.lane_w_m \
                and S._pass_path_blocked(
                    others, ego, x, S._t_move(x - ego, p) + p.reaction_s, p):
            last = "path_blocked"
            continue
        return True, why, x, "ok"
    return False, why, None, last


def analyze(path: str, head: float, tail: float):
    p = _params()
    lw = p.lane_w_m
    near_z = load_decision().motion.near_z_m
    # 阶段边界：掐头去尾
    stages = []  # (start_mono, end_mono)
    cur = None
    ticks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ty = rec.get("type")
            if ty == "stage_begin":
                cur = [rec["ts_mono_ns"], None]
                stages.append(cur)
            elif ty == "stage_end" and cur is not None:
                cur[1] = rec["ts_mono_ns"]
                cur = None
            elif rec.get("sel_why"):
                ticks.append(rec)
    keep = []
    for rec in ticks:
        m = rec.get("ts_mono_ns")
        ok = False
        for st in stages:
            if st[1] is None or m is None:
                continue
            t0 = (m - st[0]) / 1e9
            t1 = (st[1] - m) / 1e9
            if t0 >= head and t1 >= tail:
                ok = True
                break
        if ok:
            keep.append(rec)

    n = collections.Counter()
    gate_ct = collections.Counter()
    rows_out = []
    mismatch = []
    held_n = collections.Counter()
    for rec in keep:
        d = rec.get("sel_dbg") or {}
        ego = d.get("ego")
        if ego is None:
            continue
        ego *= lw
        road = _road_from_dbg(d, lw)
        if road is None:
            continue
        cars = _cars_from_rec(rec, d, lw, near_z)
        committed = rec.get("intent_phase") == "merging"
        commit_x = (rec.get("intent_lane") or 0.0) * lw if committed else None
        n["ticks"] += 1
        why_rec = rec.get("sel_why")
        n[f"why:{why_rec}"] += 1
        gh = d.get("graze_hold") or [None, 0.0]
        latch_armed = gh[0] is not None

        # 重放 _pick_pass 的调用序：非承诺先同道紧急再邻道；承诺同道紧急+跨度内邻道
        best = None
        per_car = []
        for c in sorted(cars, key=S._pass_key):
            others = [o for o in cars if o is not c]
            if not committed:
                ok, why, x, gate = _gate_pass(c, others, road, ego, p,
                                              allow_adjacent=False)
                if not ok and gate in ("adjacent_off",):
                    ok, why, x, gate = _gate_pass(c, others, road, ego, p,
                                                  allow_adjacent=True)
            else:
                ok, why, x, gate = _gate_pass(c, others, road, ego, p,
                                              allow_adjacent=False)
                if not ok and gate == "adjacent_off" \
                        and p.pass_graze_adjacent_enabled:
                    ok2, why2, x2, gate2 = _gate_pass(
                        c, others, road, ego, p, allow_adjacent=True)
                    if ok2:
                        sp = (S._lane_span(commit_x, p, road)
                              if commit_x is not None else None)
                        if sp is None or S._in_interval(x2, sp):
                            ok, why, x, gate = ok2, why2, x2, gate2
                        else:
                            gate = "commit_span"
                    else:
                        gate = gate2
            per_car.append((c.oid, ok, why, gate,
                            None if x is None else round(x / lw, 3)))
            gate_ct[gate] += 1
            if ok and best is None:
                best = (c.oid, why, x)
        if best is not None:
            n["replay_pass"] += 1
            if why_rec in ("pass", "graze"):
                n["replay_and_record"] += 1
            else:
                n["replay_not_record"] += 1
                mismatch.append((rec.get("seq"), why_rec, "pass_candidate",
                                 best[0], best[1]))
        else:
            n["replay_none"] += 1
            if why_rec in ("pass", "graze"):
                n["record_not_replay"] += 1
                mismatch.append((rec.get("seq"), why_rec, "no_candidate",
                                 d.get("hit_oid"), None))
        # 闩状态账：武装中但本拍没在贴边
        if latch_armed:
            held_n["armed_ticks"] += 1
            if why_rec in ("pass", "graze"):
                held_n["armed_delivering"] += 1
            elif why_rec == "commit":
                held_n["armed_commit"] += 1
            elif why_rec == "reward":
                held_n["armed_reward"] += 1
            elif why_rec.startswith("coin"):
                held_n["armed_coin"] += 1
            else:
                held_n["armed_center"] += 1
            # 闩目标车本拍的门位
            for oid, ok, why, gate, x in per_car:
                if oid == gh[0]:
                    held_n[f"heldcar:{gate}"] += 1
                    break
            else:
                held_n["heldcar:missing"] += 1
        rows_out.append({
            "seq": rec.get("seq"), "fid": rec.get("fid"),
            "why": why_rec, "committed": int(committed),
            "ego": round(ego / lw, 3),
            "target": d.get("target"),
            "hold_oid": gh[0], "hold_t": gh[1],
            "replay": None if best is None else best[1],
            "replay_oid": None if best is None else best[0],
            "gates": ";".join(f"{o}:{g}" for o, ok, w, g, x in per_car
                              if not ok),
        })
    return n, gate_ct, held_n, rows_out, mismatch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--head", type=float, default=12.0)
    ap.add_argument("--tail", type=float, default=10.0)
    ap.add_argument("--csv")
    args = ap.parse_args()
    all_rows = []
    for path in args.traces:
        n, gates, held, rows, mism = analyze(path, args.head, args.tail)
        all_rows.extend(rows)
        print(f"== {os.path.basename(os.path.dirname(os.path.dirname(path)))}"
              f"/{os.path.basename(path)}")
        print(f"   拍 {n['ticks']}：重放有候选 {n['replay_pass']}"
              f"（录制也是贴边 {n['replay_and_record']}、"
              f"录制非贴边 {n['replay_not_record']}）；"
              f"重放无候选 {n['replay_none']}"
              f"（录制是贴边 {n['record_not_replay']}）")
        tot = n["replay_pass"] + n["record_not_replay"]
        rec_pass = n["replay_and_record"] + n["record_not_replay"]
        if tot:
            print(f"   贴边拍复现率 {n['replay_and_record']}/{rec_pass}"
                  f" = {n['replay_and_record'] / max(rec_pass, 1):.1%}")
        print("   门位:", dict(gates.most_common()))
        print("   闩:", dict(held.most_common()))
        if mism[:10]:
            print("   分歧样例:", mism[:10])
    if args.csv and all_rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"csv: {args.csv} ({len(all_rows)} 行)")


if __name__ == "__main__":
    main()