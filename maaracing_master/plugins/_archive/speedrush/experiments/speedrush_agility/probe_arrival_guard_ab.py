"""来车守卫侧别记忆的 A/B 影子复算（speedrush，离线）。

**问什么**：给来车守卫加「同一辆来车在窗内定侧、不逐拍重推」之后，目标序列
的抖动是否下降、代价是否出现。两个策略吃同一批录制输入，各自跑一遍选择层：

- ``off``：今天的行为（每拍按 ``ego >= car.x`` 重推侧别）。
- ``on``：``selection.arrival_guard_side_pin`` 打开。

口径与边界：

- 开环影子：自车横位、车表、走廊全部取录制值；目标不回灌运动，所以只能
  比较「同一批观测下的目标序列」，不能预测分数或闭环稳定性。
- 重建缺口：车表的 ``x_unknown/half_w/near`` 取自 trace 的 ``carsrc``；
  ``v_close`` 由纵深/到站时间近似；道路用 ``scene.road.bands``。这些字段
  不是选择层的全部内部状态，**先把 off 与录制目标的重合率当作保真度**，
  重合率低时只报差异、不下结论。
- 每段重置选择层状态；两臂从同一初始状态出发，第一次目标不同之后状态各自演化。

产出：每段的拍数、off/on 的大跳次数（>0.5 道）、总变差（Σ|Δtarget|）、
方向反转次数、最大单拍跳幅，以及 off 对录制目标的重合率。

用法::

    .venv/Scripts/python.exe -X utf8 probe_arrival_guard_ab.py <会话 ...>
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from maaracing_master.plugins._archive.speedrush import selection as S  # noqa: E402
from maaracing_master.plugins._archive.speedrush.config import load_decision  # noqa: E402
from maaracing_master.plugins._archive.speedrush.world_model import load_calib  # noqa: E402

LANE_W = 3.5
JUMP_LANE = 0.5
GAP_S = 0.2
HERE = Path(__file__).resolve().parent
OUT = HERE / "_out"


def _params():
    cfg = load_decision()
    s = cfg.selection
    pl = cfg.planner
    lw = load_calib().lane_w_m
    return S.SelectionParams(
        lane_w_m=lw,
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
        arrival_guard_side_pin=False,
        pass_reach_lane=s.pass_reach_lane,
        pass_lean_min_s=s.pass_lean_min_s,
        pass_x_unknown_enabled=s.pass_x_unknown_enabled,
        pass_side_pin_enabled=s.pass_side_pin_enabled,
        pass_latch_enabled=s.pass_latch_enabled)


def _road(rec):
    r = (rec.get("scene") or {}).get("road") or {}
    bands = []
    for b in r.get("bands") or []:
        if not b or len(b) < 7:
            continue
        bands.append(S.RoadBand(b[0], b[1], b[2], b[3], b[4], b[5], b[6]))
    if not bands:
        return None
    return S.RoadFrame(tuple(bands), r.get("w"), r.get("l"), r.get("r"), True)


def _objs(rec, near_z: float):
    sd = rec.get("sel_dbg") or {}
    srcs = rec.get("carsrc") or []
    cars = []
    for i, row in enumerate(sd.get("car") or []):
        if not row or len(row) < 2:
            continue
        meta = srcs[i] if i < len(srcs) else {}
        x, t = row[0] * LANE_W, row[1]
        z = meta.get("z") if isinstance(meta.get("z"), (int, float)) else None
        src = meta.get("src") or "pixel"
        near = (src == "grid" and isinstance(meta.get("cz"), (int, float))
                and meta["cz"] <= 16.0) or (z is not None and z <= near_z)
        v_close = (z / t) if (z is not None and t is not None
                              and t > 0.05) else None
        cars.append(S.Obj(
            "car", x, t, oid=meta.get("id", -1),
            safe=bool(meta.get("safe", True)), near=near, src=src,
            v_close=v_close, seen=bool(meta.get("seen", True)), z=z,
            x_unknown=bool(meta.get("x_unknown", False)),
            half_w_m=meta.get("half_w")))
    def rows(key, kind):
        out = []
        for row in sd.get(key) or []:
            if not row or len(row) < 2:
                continue
            out.append(S.Obj(kind, row[0] * LANE_W, row[1],
                             count=row[2] if len(row) > 2 else 1.0,
                             conf=row[3] if len(row) > 3 else 1.0))
        return out
    return cars, rows("rw", "reward"), rows("coin", "coin")


def _inputs(rec, near_z: float):
    road = _road(rec)
    if road is None:
        return None
    cars, rewards, coins = _objs(rec, near_z)
    sd = rec.get("sel_dbg") or {}
    ego = (rec.get("scene") or {}).get("ego_m")
    if ego is None and sd.get("ego") is not None:
        ego = sd["ego"] * LANE_W
    if ego is None:
        return None
    route = rec.get("route") or {}
    committed = route.get("ph") == "merging"
    commit_x = None
    if committed and isinstance(route.get("ref"), (int, float)) \
            and sd.get("c0") is not None:
        commit_x = (route["ref"] + sd["c0"]) * LANE_W
    coarse = (rec.get("coarse") or {}).get("lane")
    return road, rewards, cars, coins, float(ego), committed, commit_x, coarse


def _stats(targets: list[float | None]) -> dict:
    jumps = tv = 0
    mx = 0.0
    prev = None
    for v in targets:
        if v is None:
            prev = None
            continue
        if prev is not None:
            d = v - prev
            tv += abs(d)
            mx = max(mx, abs(d))
            if abs(d) > JUMP_LANE:
                jumps += 1
        prev = v
    return {"jumps": jumps, "tv": round(tv, 2), "max": round(mx, 2)}


def _reversals(targets: list[float | None]) -> int:
    n = 0
    last = 0.0
    prev = None
    for v in targets:
        if v is None:
            prev = None
            continue
        if prev is not None:
            d = v - prev
            if abs(d) > 1e-3:
                if last and (d > 0) != (last > 0):
                    n += 1
                last = d
        prev = v
    return n


def _segments(rows: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    prev_ts = None
    for r in rows:
        ts = r.get("ts_ns")
        if not out or ts is None or prev_ts is None \
                or (ts - prev_ts) / 1e9 > GAP_S:
            out.append([])
        out[-1].append(r)
        prev_ts = ts
    return out


def main() -> None:
    sessions = sys.argv[1:]
    if not sessions:
        raise SystemExit("用法：probe_arrival_guard_ab.py <会话 ...>")
    p_off = _params()
    p_on = replace(p_off, arrival_guard_side_pin=True)
    near_z = load_decision().motion.near_z_m
    lines = ["# 来车守卫侧别记忆 A/B（影子复算）\n"]
    total = {"off": [0, 0.0, 0, 0.0], "on": [0, 0.0, 0, 0.0], "n": 0, "close": 0}
    for sess in sessions:
        path = Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" \
            / "logs" / sess / "trace" / "speedrush.jsonl"
        if not path.is_file():
            lines.append(f"- {sess}: trace 不存在，跳过")
            continue
        rows = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                sd = r.get("sel_dbg") or {}
                if sd.get("target") is None:
                    continue
                rows.append(r)
        rows.sort(key=lambda r: r.get("ts_ns") or 0)
        lines.append(f"## {sess}（{len(rows)} 拍）\n")
        lines.append("### 无状态选择（每拍直接跑 select，隔离守卫决策）\n")
        lines.append("| 段 | 拍数 | off 大跳 | on 大跳 | off 总变差 | on 总变差 |"
                     " off 反转 | on 反转 | off 最大跳 | on 最大跳 | off 重合率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        why_all: dict[str, list[int]] = {}
        diff_n = 0
        total_seg_ticks = 0
        mags: list[float] = []
        for si, seg in enumerate(_segments(rows)):
            guard: dict[int, float] = {}
            rec_t, off_t, on_t = [], [], []
            for rec in seg:
                inp = _inputs(rec, near_z)
                if inp is None:
                    continue
                road, rewards, cars, coins, ego, committed, cx, coarse = inp
                kw = dict(coarse_lane=coarse, committed=committed,
                          commit_x_m=cx)
                ro = S.select(road, rewards, cars, coins, ego, p_off, **kw)
                rn = S.select(road, rewards, cars, coins, ego, p_on,
                              guard_side=guard, **kw)
                rec_t.append(rec["sel_dbg"]["target"])
                off_t.append(ro.target_x_m / LANE_W)
                on_t.append(rn.target_x_m / LANE_W)
                total_seg_ticks += 1
                d = abs(off_t[-1] - on_t[-1])
                if d > 1e-6:
                    diff_n += 1
                    mags.append(d)
                why = str(rec.get("sel_why"))
                cnt = why_all.setdefault(why, [0, 0])
                cnt[0] += 1
                if abs(off_t[-1] - rec_t[-1]) <= 0.1:
                    cnt[1] += 1
            so, sn = _stats(off_t), _stats(on_t)
            ro, rn = _reversals(off_t), _reversals(on_t)
            close = sum(1 for a, b in zip(off_t, rec_t) if abs(a - b) <= 0.1)
            rate = close / len(rec_t) if rec_t else 0.0
            lines.append(
                f"| s{si + 1} | {len(rec_t)} | {so['jumps']} | {sn['jumps']} | "
                f"{so['tv']} | {sn['tv']} | {ro} | {rn} | {so['max']} | "
                f"{sn['max']} | {rate:.0%} |")
            total["off"][0] += so["jumps"]
            total["off"][1] += so["tv"]
            total["off"][2] += ro
            total["on"][0] += sn["jumps"]
            total["on"][1] += sn["tv"]
            total["on"][2] += rn
            total["n"] += len(rec_t)
            total["close"] += close
        lines.append("")
        mags.sort()
        p50 = mags[len(mags) // 2] if mags else 0.0
        p90 = mags[int(len(mags) * 0.9)] if mags else 0.0
        lines.append(f"两臂目标不同的拍：{diff_n}/{total_seg_ticks}，"
                     f"差异 p50 {p50:.2f} 道 / p90 {p90:.2f} 道"
                     f"（1.36=整侧翻转）")
        lines.append("按选择理由看无状态 off 的重合率（低重合的理由说明该理由"
                     "依赖状态，不能用来评判）：")
        for why, (n, ok) in sorted(why_all.items(), key=lambda kv: -kv[1][0]):
            lines.append(f"- {why}: {ok}/{n}（{ok / n:.0%}）")
        lines.append("")
        lines.append("### 有状态选择层（含保持闩/驻留；状态会随分歧演化）\n")
        lines.append("| 段 | 拍数 | off 大跳 | on 大跳 | off 总变差 |"
                     " on 总变差 | off 反转 | on 反转 | off 重合率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for si, seg in enumerate(_segments(rows)):
            sel_off = S.LayeredSelector(p_off)
            sel_on = S.LayeredSelector(p_on)
            rec_t, off_t, on_t = [], [], []
            prev_ts = None
            for rec in seg:
                inp = _inputs(rec, near_z)
                if inp is None:
                    continue
                road, rewards, cars, coins, ego, committed, cx, coarse = inp
                ts = rec.get("ts_ns")
                dt = 0.05 if prev_ts is None or ts is None else \
                    max(0.0, min(0.2, (ts - prev_ts) / 1e9))
                prev_ts = ts
                kw = dict(coarse_lane=coarse, committed=committed,
                          commit_x_m=cx)
                ro = sel_off.update(road, rewards, cars, coins, ego, dt, **kw)
                rn = sel_on.update(road, rewards, cars, coins, ego, dt, **kw)
                rec_t.append(rec["sel_dbg"]["target"])
                off_t.append(ro.target_x_m / LANE_W)
                on_t.append(rn.target_x_m / LANE_W)
            so, sn = _stats(off_t), _stats(on_t)
            ro, rn = _reversals(off_t), _reversals(on_t)
            close = sum(1 for a, b in zip(off_t, rec_t) if abs(a - b) <= 0.1)
            rate = close / len(rec_t) if rec_t else 0.0
            lines.append(
                f"| s{si + 1} | {len(rec_t)} | {so['jumps']} | {sn['jumps']} | "
                f"{so['tv']} | {sn['tv']} | {ro} | {rn} | {rate:.0%} |")
        lines.append("")
    if total["n"]:
        lines.append("## 汇总\n")
        lines.append(f"- 拍数 {total['n']}，off 对录制目标重合率 "
                     f"{total['close'] / total['n']:.0%}（保真度门槛：先看它）")
        lines.append(f"- 大跳：off {total['off'][0]} → on {total['on'][0]}"
                     f"（{(total['on'][0] / total['off'][0] - 1):+.0%}）"
                     if total["off"][0] else "- 大跳：off 0")
        lines.append(f"- 总变差：off {total['off'][1]} → on {total['on'][1]}")
        lines.append(f"- 方向反转：off {total['off'][2]} → on {total['on'][2]}")
        lines.append("")
    lines.append("> 开环影子：自车轨迹与车表取录制值，目标不回灌；"
                 "只比较同一批观测下的目标序列，不预测分数或闭环稳定性。"
                 "off 重合率低时（<80%）先查重建缺口，不当结论用。\n")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "arrival_guard_ab.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
