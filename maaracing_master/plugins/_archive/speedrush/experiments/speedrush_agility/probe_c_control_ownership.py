"""C 线：单拍最终控制权、覆盖链与相位（只读）。

复用实录回放（probe_steering_replay.replay 的同一输入契约），在
``LateralPlanner.update`` 上挂行级追踪与律函数记录：

- 本拍最终生效的控制语句（哪条路径出的杆）；
- 同拍覆盖链（每次对 ``steer_raw`` 的赋值，按顺序与值）；
- 位置状态的消费时刻（律运行前的 executed_lane 与 ⑥b 修正后的差）；
- 死区是否参与（``|e|`` 与 db 的关系、dz 开/关两种杆值与符号）；
- 整形链（原始 → 低通 → 限速 → 死区 → 下发）逐段数值。

不改变生产行为：追踪器只在回放期间替换，且先用未跟踪回放核对逐位一致，
再与实录下发杆值比对；基线不符时拒绝解释反事实。
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import inspect
import json
import math
from pathlib import Path
import sys
import textwrap

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[5]))
sys.path.insert(0, str(HERE))

from maaracing_master.plugins._archive.speedrush import planner as P
from maaracing_master.plugins._archive.speedrush.config import load_decision
from maaracing_master.plugins._archive.speedrush.planner import (
    LANE_W, LateralPlanner, heading_for_error, stick_of, law)
from probe_steering_replay import replay

# ---------------------------------------------------------------- 源码位置映射

LABELS = {
    1107: "conserve_center",       # CONSERVE 有 ro：直连路中心
    1111: "conserve_hold_pd",      # CONSERVE 无 ro：冻结位 PD
    1113: "fault_zero",
    1116: "stale_hold",            # 过期保持（杆值不动）
    1123: "abort_hold_pd",
    1133: "unverified_law0",       # 盲窗：只把朝向拉回 0
    1146: "plan_hold",             # 计划短断供：沿用上拍计划航向
    1161: "frame_shift_pd",
    1186: "precise_dec",           # 交付意图（决策目标）
    1192: "maneuver_pd",
    1198: "gentle_pd",
    1201: "no_target_hold",        # 防御：无目标保持上拍杆值
    1224: "plan_merge_pd",
    1236: "precise_plan",          # 交付意图（计划有效拍）
    1262: "plan_heading",          # 计划接管：执行计划车头角
    1284: "plan_corridor",         # 走廊不可行：执行最快脱离
}
SLEW_LINES = {1150, 1272}


def _sites():
    lines, first = inspect.getsourcelines(LateralPlanner.update)
    root = ast.parse(textwrap.dedent("".join(lines)))
    sites = {}
    markers = {}
    for node in ast.walk(root):
        if not isinstance(node, ast.Assign):
            continue
        names = [ast.unparse(t) for t in node.targets]
        value = ast.unparse(node.value)
        line = first + node.lineno - 1
        if "steer_raw" in names and line in LABELS:
            sites[line] = LABELS[line]
        if "self.state.steer_norm" in names and value == "u_pv":
            sites[line] = "short_preview"
        if "line_pos" in names:
            markers[line] = "pos_before"
        if "plan_center" in names:
            markers[line] = "pos_after"
    if set(markers) and set(markers.values()) != {"pos_before", "pos_after"}:
        raise ValueError("生产控制流程变化：位置修正标记位缺失")
    return sites, markers


# ---------------------------------------------------------------- 逐拍记录

class Recorder:
    def __init__(self):
        self.ticks = []
        self.calls = []

    def new_tick(self):
        self.ticks.append({
            "assigns": [],          # [(label, steer_raw_before, value_after)]
            "pos_before": None, "pos_after": None,
        })
        return self.ticks[-1]

    def call(self, name, args, kwargs, result, state):
        self.calls.append((len(self.ticks) - 1, name, args, kwargs, result,
                           dict(state)))


def replay_traced(rows, params):
    sites, markers = _sites()
    original = LateralPlanner.update
    originals = {n: getattr(LateralPlanner, n) for n in
                 ("_pd", "_law", "_law_psi", "_law_precise", "_preview_steer")}
    orig_law, orig_stick = P.law, P.stick_of
    rec = Recorder()

    def state_of(pl):
        s = pl.state
        return {"x": s.executed_lane, "v_lat": s.v_lat, "psi": s.heading,
                "r": s.yaw_rate, "norm": s.steer_norm,
                "dt": pl._ctrl_dt_s}

    def wrap_method(name):
        orig = originals[name]

        def wrapped(self, *a, **k):
            out = orig(self, *a, **k)
            rec.calls.append((len(rec.ticks) - 1, name, a, k, out,
                              state_of(self)))
            return out
        return wrapped

    def wrapped_law(e, psi, r, p, gentle=False, deadband=None, cap=None,
                    klat=None):
        out = orig_law(e, psi, r, p, gentle, deadband, cap, klat)
        db = (p.hold_deadband_lane if gentle else 0.0) \
            if deadband is None else deadband
        u_nodz = orig_stick(heading_for_error(e, p, klat), psi, r, p,
                            gentle, cap)
        rec.calls.append((len(rec.ticks) - 1, "law", (e, psi, r),
                          {"gentle": gentle, "db": db, "cap": cap,
                           "klat": klat},
                          {"u": out, "u_nodz": u_nodz}, None))
        return out

    def wrapped_stick(psi_des, psi, r, p, gentle=False, cap=None):
        out = orig_stick(psi_des, psi, r, p, gentle, cap)
        lead = 0.0 if gentle else getattr(p, "heading_lead_s", 0.0)
        plim = p.heading_hold_rad if gentle else p.heading_max_rad
        lim = (p.hold_stick_max if gentle else 1.0) if cap is None else cap
        raw = p.k_psi * (max(-plim, min(plim, psi_des))
                         - (psi + lead * r)) - p.k_r * r
        rec.calls.append((len(rec.ticks) - 1, "stick", (psi_des, psi, r),
                          {"gentle": gentle, "cap": cap, "lead": lead,
                           "lim": lim}, {"u": out, "u_unclamped": raw}, None))
        return out

    for name in originals:
        setattr(LateralPlanner, name, wrap_method(name))
    P.law = wrapped_law
    P.stick_of = wrapped_stick

    def wrapped(self, *a, **k):
        tick = {"assigns": [], "pos_before": None, "pos_after": None}
        rec.ticks.append(tick)
        seq = []

        def trace(frame, event, arg):
            if frame.f_code is not original.__code__:
                return None
            if event == "line":
                seq.append((frame.f_lineno, frame.f_locals.get("steer_raw")))
                if frame.f_lineno in markers:
                    tick[markers[frame.f_lineno]] = (
                        frame.f_locals["self"].state.executed_lane)
            elif event == "return":
                tick["branch"] = frame.f_locals["self"]._branch
                tick["osc_fallback"] = bool(frame.f_locals.get("osc_fallback"))
                tick["precise_direct"] = bool(
                    frame.f_locals.get("precise_direct"))
                tick["plan_held"] = bool(frame.f_locals.get("plan_held"))
                tick["final_raw"] = frame.f_locals.get("steer_raw")
            return trace

        previous = sys.gettrace()
        if previous is not None:
            raise RuntimeError("已有调试跟踪器")
        sys.settrace(trace)
        try:
            command = original(self, *a, **k)
        finally:
            sys.settrace(previous)
        for j, (lineno, value) in enumerate(seq):
            if lineno not in sites:
                continue
            after = None
            for k2 in range(j + 1, len(seq)):
                if seq[k2][1] is not None:
                    after = seq[k2][1]
                    break
            if after is None:
                after = self.last_steer_raw
            tick["assigns"].append([sites[lineno], lineno, value, after])
        tick["final_assign"] = (tick["assigns"][-1][0]
                                if tick["assigns"] else None)
        if tick["assigns"]:
            tick["assigns"][-1][3] = tick.get("final_raw")
        tick["emitted"] = command.steer_x
        tick["final_norm"] = self.state.steer_norm
        tick["last_raw"] = self._last_raw
        return command

    LateralPlanner.update = wrapped
    try:
        values = replay(rows, params)
    finally:
        LateralPlanner.update = original
        P.law = orig_law
        P.stick_of = orig_stick
        for name, fn in originals.items():
            setattr(LateralPlanner, name, fn)
    return values, rec


# ---------------------------------------------------------------- 分析

def analyze(rows, params, values, rec):
    n = len(rows)
    calls_by_tick = {}
    for entry in rec.calls:
        calls_by_tick.setdefault(entry[0], []).append(entry[1:])
    ticks = []
    for i, row in enumerate(rows):
        tick = rec.ticks[i]
        prev_norm = 0.0 if i == 0 else rows[i - 1]["steer_pre_lpf"]
        assigns = tick["assigns"]
        calls = calls_by_tick.get(i, [])
        law_calls = [c for c in calls if c[0] == "law"]
        stick_calls = [c for c in calls if c[0] == "stick"]
        pd_calls = [c for c in calls if c[0] == "_pd"]
        psi_calls = [c for c in calls if c[0] == "_law_psi"]
        pv_calls = [c for c in calls if c[0] == "_preview_steer"]
        prec_calls = [c for c in calls if c[0] == "_law_precise"]
        raw = float(values[i])
        norm = row["steer_norm"]
        emitted = row["steer_x"]
        # 低通后的理论值（未预瞄接管时）
        alpha = 1.0 - math.exp(-row["dt"] / max(params.tau_steer_s, 1e-3))
        lpf = row.get("_prev_norm_it")
        ticks.append({
            "i": i, "seq": row["seq"], "t": None, "dt": row["dt"],
            "path": tick["final_assign"] or "none",
            "chain": [[a[0], round(a[3], 4)] for a in assigns],
            "raw": row["steer_pre_lpf"], "emit": emitted,
            "final_raw": round(tick.get("final_raw") or 0.0, 4),
            "norm": norm,
            "pos_before": tick["pos_before"], "pos_after": tick["pos_after"],
            "branch": tick.get("branch"),
            "xt": row["x_target"], "x": row["executed_lane"],
            "v_lat": row["v_lat"], "psi": row["heading"],
            "rr": (row["lat_ctrl"] or {}).get("rr"),
            "csrc": (row["lat_ctrl"] or {}).get("csrc"),
            "pv": (row["lat_ctrl"] or {}).get("pv"),
            "osc_a": (row["lat_ctrl"] or {}).get("osc_a"),
            "rebase": (row["lat_ctrl"] or {}).get("rebase"),
            "unver": (row["lat_ctrl"] or {}).get("unver"),
            "why": row.get("sel_why"),
            "phase": row.get("intent_phase"),
            "plan": None if row["plan"] is None else {
                "valid": row["plan"]["valid"], "reason": row["plan"]["reason"],
                "pc": row["plan"]["pc"], "layer": row["plan"]["layer"],
                "cc": row["plan"]["cc"], "chosen": row["plan"]["chosen"],
                "n_ok": row["plan"]["n_ok"]},
            "stick": None if not stick_calls else {
                "psi_des": round(stick_calls[-1][1][0], 5),
                "psi": round(stick_calls[-1][1][1], 5),
                "r": None if stick_calls[-1][1][2] is None
                else round(stick_calls[-1][1][2], 4),
                "u": round(stick_calls[-1][3]["u"], 4),
                "u_raw": round(stick_calls[-1][3]["u_unclamped"], 4),
                "lim": stick_calls[-1][2]["lim"],
                "gentle": stick_calls[-1][2]["gentle"],
                "lead": stick_calls[-1][2]["lead"]},
            "law": [[round(c[1][0], 5), c[2]["gentle"], c[2]["db"],
                     round(c[3]["u"], 4), round(c[3]["u_nodz"], 4)]
                    for c in law_calls],
            "pd": [[round(c[1][0], 4), c[2].get("gentle"),
                    round(c[3], 4)] for c in pd_calls],
            "law_psi": [[round(c[1][0], 5), c[2].get("cap"),
                         round(c[3], 4)] for c in psi_calls],
            "law_precise": [[round(c[1][0], 4), round(c[3], 4)]
                            for c in prec_calls],
            "preview": None if not pv_calls else
            (None if pv_calls[-1][3] is None else round(pv_calls[-1][3], 4)),
            "lpf_alpha": alpha,
            "assigns_n": len(assigns),
        })
    return ticks


def reversals(ticks, thresh=0.25):
    out = []
    prev = None
    for i, t in enumerate(ticks):
        v = t["emit"] / 32767.0
        if abs(v) <= thresh:
            continue
        if prev is not None and v * ticks[prev]["emit"] < 0:
            out.append((prev, i))
        prev = i
    return out


def windows(revs, ticks, gap_s=1.5):
    if not revs:
        return []
    out = [[revs[0]]]
    for pair in revs[1:]:
        gap = sum(ticks[k]["dt"] for k in range(out[-1][-1][1], pair[1]))
        if gap <= gap_s:
            out[-1].append(pair)
        else:
            out.append([pair])
    return out


def format_tick(tk):
    plan = tk["plan"] or {}
    law = tk["law"][-1] if tk["law"] else None
    return (
        f"t={tk['t']:7.3f} seq={tk['seq']:5d} path={tk['path']:<15s} "
        f"chain={'->'.join(c[0] for c in tk['chain']):<52s} "
        f"vals={'/'.join(str(c[1]) for c in tk['chain']):<28s} "
        f"raw={tk['raw']:7.3f} norm={tk['norm']:7.3f} emit={tk['emit']:6d} "
        f"x={tk['x']:7.3f} xt={tk['xt'] if tk['xt'] is None else round(tk['xt'],3):>7} "
        f"psi={tk['psi']:7.4f} r={tk['rr']:>7} vlat={tk['v_lat']:7.3f} "
        f"pc={plan.get('pc')} lay={plan.get('layer')} ch={plan.get('chosen')} "
        f"law={law} pv={tk['preview']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", type=Path, required=True)
    ap.add_argument("--dump", type=Path)
    ap.add_argument("--segment", type=int)
    ap.add_argument("--rev-thres", type=float, default=0.25,
                    help="显著拍阈值：|steer_x/32767| 超过它才算显著拍")
    ap.add_argument("--gap", type=float, default=1.5,
                    help="相邻反向合并为同一窗口的时间间隔（s），与 probe_c_windows 一致")
    ap.add_argument("--cmd-thres", type=float, nargs="+",
                    default=[0.02, 0.05, 0.1, 0.25],
                    help="命令级（低通前）符号翻转的幅度阈值，逐个输出")
    ap.add_argument("--show", type=int, default=0,
                    help="打印反转拍数 >= N 的窗口逐拍时间线")
    args = ap.parse_args()
    rows = [json.loads(line) for line in args.trace.read_text(
        encoding="utf-8").splitlines()]
    rows = [r for r in rows if r.get("lat_ctrl")]
    groups = []
    for row in rows:
        if (not groups or row["stage_id"] != groups[-1][-1]["stage_id"]
                or row["seq"] <= groups[-1][-1]["seq"]):
            groups.append([])
        groups[-1].append(row)
    params = load_decision().planner
    for seg_idx, seg in enumerate(groups, 1):
        if args.segment and seg_idx != args.segment:
            continue
        baseline = replay(seg, params)
        values, rec = replay_traced(seg, params)
        if not np.array_equal(baseline, values):
            raise AssertionError("跟踪改变了回放结果")
        actual = np.array([r["steer_x"] / 32767 for r in seg])
        err = values - actual
        ticks = analyze(seg, params, values, rec)
        dt = [r["dt"] for r in seg]
        t = np.cumsum(dt)
        for tick, tt in zip(ticks, t):
            tick["t"] = round(float(tt), 3)
        revs = reversals(ticks, args.rev_thres)
        wins = windows(revs, ticks, args.gap)
        summary = {
            "segment": seg_idx, "stage": seg[0]["stage_id"],
            "ticks": len(seg),
            "baseline": {"mean_abs": round(float(np.mean(abs(err))), 6),
                         "max_abs": round(float(np.max(abs(err))), 6),
                         "sign_flips": int(np.sum(values * actual < 0)),
                         "ticks_gt_1lsb": int(np.sum(
                             abs(err) > 1 / 32767 + 1e-9))},
            "paths": dict(Counter(t["path"] for t in ticks)),
            "reversals": len(revs),
            "rev_thres": args.rev_thres,
            "window_gap_s": args.gap,
            "windows": [],
            "deadband": {},
            "chain": {},
        }
        # 反向事件分类，两种口径都出：
        #   端点严格 = 反向两端显著拍的最终路径相同；
        #   区间宽松 = 两端显著拍之间（含）出现过的路径只有一条。
        same_end = switch_end = same_span = switch_span = 0
        pairs = Counter()
        for a, b in revs:
            homo_end = ticks[a]["path"] == ticks[b]["path"]
            homo_span = len({s["path"] for s in ticks[a:b + 1]}) == 1
            same_end += int(homo_end)
            switch_end += int(not homo_end)
            same_span += int(homo_span)
            switch_span += int(not homo_span)
            pairs[(ticks[a]["path"], ticks[b]["path"], homo_end)] += 1
        summary["reversal_split"] = {
            "endpoint_same_path": same_end, "endpoint_switch": switch_end,
            "span_same_path": same_span, "span_switch": switch_span,
            "pairs": [{"from": a, "to": b, "endpoint_same": h, "n": n}
                      for (a, b, h), n in pairs.most_common()]}
        # 窗口
        for w in wins:
            st, en = w[0][0], w[-1][1]
            span = ticks[st:en + 1]
            paths = Counter(s["path"] for s in span)
            pcs = [s["plan"]["pc"] for s in span if s["plan"]]
            xt = [s["xt"] for s in span if s["xt"] is not None]
            summary["windows"].append({
                "t": [ticks[st]["t"], ticks[en]["t"]], "n": len(w),
                "ticks": [st, en], "seq": [ticks[st]["seq"], ticks[en]["seq"]],
                "paths": dict(paths),
                "same_path_revs": sum(
                    1 for a, b in w if len({s["path"] for s in ticks[a:b + 1]}) == 1),
                "plan_pc_range": None if not pcs else [min(pcs), max(pcs)],
                "xt_range": None if not xt else
                [round(min(xt), 3), round(max(xt), 3)],
                "emit_range": [min(s["emit"] for s in span),
                               max(s["emit"] for s in span)],
                "why": dict(Counter(s["why"] for s in span)),
            })
        # 死区
        gentle = [t2 for t2 in ticks if t2["law"]]
        with_db = [t2 for t2 in gentle
                   if t2["law"][-1][1] and t2["law"][-1][2] > 0]
        inside = [t2 for t2 in with_db if abs(t2["law"][-1][0]) < t2["law"][-1][2]]
        inside_nonzero = [t2 for t2 in inside if abs(t2["law"][-1][3]) > 0.01]
        flip = [t2 for t2 in inside
                if t2["law"][-1][3] * t2["law"][-1][4] <= 0]
        opposite = [t2 for t2 in inside
                    if t2["law"][-1][3] * t2["law"][-1][4] < 0]
        summary["deadband"] = {
            "gentle_pd_ticks": len(with_db),
            "inside_db": len(inside),
            "inside_db_none": len(with_db) - len(inside),
            "inside_db_stick_gt_0.01": len(inside_nonzero),
            # 严格异号：关掉死区后杆值符号才真正反过来（u*u_nodz < 0）
            "inside_db_opposite_sign": len(opposite),
            # 宽松：把 u 恰为 0 也算作与 u_nodz 不同（u*u_nodz <= 0）
            "inside_db_zero_or_opposite": len(flip),
            "stick_abs_mean_inside": None if not inside else round(
                float(np.mean([abs(t2["law"][-1][3]) for t2 in inside])), 4),
            "entries": sum(1 for a, b in zip(with_db, with_db[1:])
                           if (abs(a["law"][-1][0]) < a["law"][-1][2])
                           != (abs(b["law"][-1][0]) < b["law"][-1][2])
                           and b["i"] == a["i"] + 1),
        }
        # 覆盖链与整形
        multi = [t2 for t2 in ticks if t2["assigns_n"] >= 2]
        neg_pair = [t2 for t2 in multi
                    if t2["chain"][-1][1] * t2["chain"][0][1] < 0
                    and min(abs(t2["chain"][0][1]),
                            abs(t2["chain"][-1][1])) > .05]
        dwell = [t2 for t2 in multi if t2["path"] == "plan_heading"]
        steps = np.diff(np.array([t2["emit"] for t2 in ticks])) / 32767
        step_by_switch = {}
        switch_ticks = [i for i in range(1, len(ticks))
                        if ticks[i]["path"] != ticks[i - 1]["path"]]
        keep_ticks = [i for i in range(1, len(ticks))
                      if ticks[i]["path"] == ticks[i - 1]["path"]]
        summary["chain"] = {
            "multi_assign_ticks": len(multi),
            "multi_assign_share": round(len(multi) / len(ticks), 3),
            "reverse_pair_ticks": len(neg_pair),
            "plan_heading_with_dwell_override": len(dwell),
            "chain_pairs": dict(Counter(
                "->".join([c[0] for c in t2["chain"]]) for t2 in multi)),
            "dz_zeroed_ticks": sum(1 for t2 in ticks
                                   if abs(t2["norm"]) > 0 and t2["emit"] == 0),
            "rate_clipped_ticks": None,
            "step_switch_p90": None if not switch_ticks else round(
                float(np.percentile(abs(steps[[i - 1 for i in switch_ticks]]),
                                    90)), 4),
            "step_keep_p90": None if not keep_ticks else round(
                float(np.percentile(abs(steps[[i - 1 for i in keep_ticks]]),
                                    90)), 4),
            "step_switch_mean": None if not switch_ticks else round(
                float(np.mean(abs(steps[[i - 1 for i in switch_ticks]]))), 4),
            "step_keep_mean": None if not keep_ticks else round(
                float(np.mean(abs(steps[[i - 1 for i in keep_ticks]]))), 4),
        }
        emit_steps = np.abs(steps)

        # 命令级（低通前 steer_pre_lpf）符号翻转：
        #   只认相邻两拍、且两拍幅度都 > 阈值的异号对；阈值逐个给出。
        def cmd_flips(thr):
            out = []
            for i in range(1, len(ticks)):
                a, b = ticks[i - 1]["raw"], ticks[i]["raw"]
                if a is None or b is None:
                    continue
                if abs(a) <= thr or abs(b) <= thr:
                    continue
                if a * b < 0:
                    out.append((i - 1, i))
            return out

        by_thr = {}
        for thr in args.cmd_thres:
            fl = cmd_flips(thr)
            over = [b for a, b in fl
                    if len(ticks[b]["chain"]) >= 2
                    and ticks[b]["chain"][0][1] * ticks[b]["chain"][-1][1] < 0
                    and min(abs(ticks[b]["chain"][0][1]),
                            abs(ticks[b]["chain"][-1][1])) > 0.05]
            by_thr[str(thr)] = {
                "flips": len(fl),
                "endpoint_same_path": sum(
                    1 for a, b in fl
                    if ticks[a]["path"] == ticks[b]["path"]),
                # 翻转拍当拍覆盖链首尾异号且都 >0.05（同拍内真的先给 A 再改 B）
                "override_opposite": len(over),
            }
        summary["command_flips"] = {"field": "steer_pre_lpf",
                                    "adjacent_only": True,
                                    "by_threshold": by_thr}
        # 按最终路径的翻转率（用最小阈值口径）
        base_thr = min(args.cmd_thres)
        flips_by_path = Counter(ticks[b]["path"] for a, b in cmd_flips(base_thr))
        ticks_by_path = Counter(t2["path"] for t2 in ticks)
        summary["path_flip_rate"] = {
            "threshold": base_thr,
            "by_path": {p: {"ticks": n, "flips": flips_by_path[p],
                            "rate": round(flips_by_path[p] / n, 4)}
                        for p, n in ticks_by_path.most_common()},
        }

        # 预瞄开/关切换拍的台阶：path 在 short_preview 与其它之间切换
        is_pv = [t2["path"] == "short_preview" for t2 in ticks]
        toggles = [i for i in range(1, len(ticks))
                   if is_pv[i] != is_pv[i - 1]]
        base_idx = [i for i in range(1, len(ticks))
                    if not is_pv[i] and not is_pv[i - 1]]
        pv_steps = emit_steps[[i - 1 for i in toggles]]
        base_steps = emit_steps[[i - 1 for i in base_idx]]
        causes = Counter()
        for i in toggles:
            pa, pb = ticks[i - 1]["plan"], ticks[i]["plan"]
            if (pa is None) != (pb is None):
                causes["plan_appear_disappear"] += 1
                continue
            if pa is None:
                causes["plan_absent"] += 1
                continue
            changed = [k for k in ("layer", "valid", "reason", "chosen", "cc")
                       if pa[k] != pb[k]]
            if not changed:
                causes["plan_unchanged"] += 1
            for k in changed:
                causes["chg_" + k] += 1
            if ticks[i - 1].get("csrc") != ticks[i].get("csrc"):
                causes["csrc"] += 1
        summary["preview_switch"] = {
            "toggles": len(toggles),
            # 预瞄接管拍里，脉冲方向与上一拍低通前命令相反（两拍都 >0.05）的拍数
            "pulse_opposite_prev": sum(
                1 for i in range(1, len(ticks))
                if is_pv[i] and ticks[i]["preview"] is not None
                and ticks[i - 1]["raw"] is not None
                and ticks[i - 1]["raw"] * ticks[i]["preview"] < 0
                and min(abs(ticks[i - 1]["raw"]),
                        abs(ticks[i]["preview"])) > 0.05),
            "step_mean": None if not toggles else round(
                float(np.mean(pv_steps)), 4),
            "step_p90": None if not toggles else round(
                float(np.percentile(pv_steps, 90)), 4),
            "step_max": None if not toggles else round(
                float(np.max(pv_steps)), 4),
            "baseline_ticks": len(base_idx),
            "baseline_mean": None if not base_idx else round(
                float(np.mean(base_steps)), 4),
            "baseline_p90": None if not base_idx else round(
                float(np.percentile(base_steps, 90)), 4),
            "step_mean_ratio": None if not (toggles and base_idx) else round(
                float(np.mean(pv_steps) / np.mean(base_steps)), 2),
            "step_p90_ratio": None if not (toggles and base_idx) else round(
                float(np.percentile(pv_steps, 90)
                      / np.percentile(base_steps, 90)), 2),
            "causes": dict(causes),
        }

        # 计划层级翻转（4↔2 等）与台阶，剔除同时开关预瞄的拍
        layer_of = [None if t2["plan"] is None else t2["plan"]["layer"]
                    for t2 in ticks]
        lf = [i for i in range(1, len(ticks))
              if layer_of[i] != layer_of[i - 1]]
        lf_nopv = [i for i in lf if is_pv[i] == is_pv[i - 1]]
        summary["layer_flip"] = {
            "count": len(lf),
            "count_no_preview_toggle": len(lf_nopv),
            "step_mean": None if not lf else round(
                float(np.mean(emit_steps[[i - 1 for i in lf]])), 4),
            "step_p90": None if not lf else round(
                float(np.percentile(emit_steps[[i - 1 for i in lf]], 90)), 4),
            "step_mean_no_preview_toggle": None if not lf_nopv else round(
                float(np.mean(emit_steps[[i - 1 for i in lf_nopv]])), 4),
            "step_p90_no_preview_toggle": None if not lf_nopv else round(
                float(np.percentile(emit_steps[[i - 1 for i in lf_nopv]],
                                    90)), 4),
            "baseline_mean": None if not base_idx else round(
                float(np.mean(base_steps)), 4),
            "baseline_p90": None if not base_idx else round(
                float(np.percentile(base_steps, 90)), 4),
        }
        print(json.dumps(summary, ensure_ascii=False))
        if args.dump:
            args.dump.parent.mkdir(parents=True, exist_ok=True)
            out = args.dump.with_name(
                f"{args.dump.stem}.s{seg_idx}{args.dump.suffix}")
            out.write_text(json.dumps(ticks, ensure_ascii=False),
                           encoding="utf-8")
        if args.show:
            for wi, w in enumerate(wins, 1):
                if len(w) < args.show:
                    continue
                print(f"--- window {wi} seg{seg_idx} "
                      f"t={ticks[w[0][0]]['t']}..{ticks[w[-1][1]]['t']} "
                      f"revs={len(w)}")
                for st, en in [(w[0][0], w[-1][1])]:
                    for tk in ticks[st:en + 1]:
                        print(format_tick(tk))


if __name__ == "__main__":
    main()
