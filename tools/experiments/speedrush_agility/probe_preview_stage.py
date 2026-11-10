"""整段回放台架：在生产预瞄上看满杆占空比与脉冲间隔。

**做什么**：取手动局整段 trace（09:58 / 10:48 各两阶段），逐拍把 trace 里的
规划器横向状态（exec / v_lat / psi / r / v）灌进生产 `LateralPlanner`，在
pass/graze 交付拍上跑 `_preview_steer`，得到「如果由预瞄接管会发什么杆」；
同时用同一段时间轴对齐手柄 `pads.jsonl`（16 位原始轴，归一化 /32767），
量人工的满杆占空比、脉冲时长与间隔。

**口径**：
- 满杆 = |u| ≥ 0.9（归一化）。手柄 `lx` 是 16 位有符号原始值，必须 /32767；
  旧记录直接拿 `|lx|≥0.9` 读到的是「非零杆」占比，不是满杆。
- 口径 `base` 是旧生产门（维护层 + pass/graze 交付）；`all_nocc` 是
  `preview_coverage_enabled` 开启后的生产门（所有计划层与意图、含并线，
  仍排除有碰撞代价的计划避让与全部安全链）。其余口径只用于对照。
- 安全链照生产：CRUISE、朝向来源 model、非盲窗、计划 reason 非 stale/
  frame_shift/infeasible、|exec| ≤ 1.5 道；进入驻留 0.2s。
- 脉冲 = 连续满杆拍；间隔 = 两段满杆之间的非满杆拍时长（同 20Hz 采样口径）。
- 反事实声明：状态来自人工录像、目标是影子计划，满杆占空比只用于量覆盖与
  形状，不能当作闭环结果。

**依赖**：本探针故意 import 生产 planner——标尺就是被测的预瞄，不是独立
运动学台架；独立台架见 `sim_agility.py`。

用法::

    .venv/Scripts/python.exe -X utf8 probe_preview_stage.py
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import glob
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from maaracing_master.plugins.speedrush import DECISION_FILE
from maaracing_master.plugins.speedrush.config import Planner, _read_decision
from maaracing_master.plugins.speedrush.planner import LateralPlanner

FULL = 32767
FULL_STICK = 0.9
WALL_ESCAPE_LANE = 1.5
DWELL_S = 0.2


def _planner() -> LateralPlanner:
    cfg = _read_decision(DECISION_FILE)
    base = {k: getattr(cfg.planner, k) for k in cfg.planner.__dataclass_fields__}
    return LateralPlanner(Planner(**base))


def _manual_stages(root: Path, sessions: set[str] | None = None) -> list[dict]:
    out = []
    for meta_path in sorted(root.glob("debug/speedrush/*_manual_p*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        token = meta.get("session")
        if not token:
            continue
        if sessions and not any(token == s or token.endswith(s)
                                for s in sessions):
            continue
        out.append({"meta_path": meta_path, "dir": meta_path.parent, **meta})
    return out


def _pads(stage: dict) -> list[tuple[float, float]]:
    start = dt.datetime.fromisoformat(stage["started_at"])
    base_ns = start.timestamp() * 1e9
    mono0 = stage["monotonic_start_ns"]
    path = stage["dir"] / "pads.jsonl"
    out = []
    if not path.exists():
        return out
    for line in open(path, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        lx = rec.get("lx")
        if lx is None:
            continue
        out.append((base_ns + (rec["ts_ns"] - mono0), lx / FULL))
    out.sort()
    return out


def _nearest(pads: list[tuple[float, float]], epoch: float) -> float | None:
    if not pads:
        return None
    keys = [p[0] for p in pads]
    i = bisect.bisect_left(keys, epoch)
    cand = [pads[j] for j in (i - 1, i) if 0 <= j < len(pads)]
    if not cand:
        return None
    return min(cand, key=lambda p: abs(p[0] - epoch))[1]


def _runs(vals: list[float | None], thr: float = FULL_STICK,
          step_s: float = 0.05) -> tuple[int, list[float], list[float]]:
    """满杆段数、段长（s）、段间隔（s）；step_s 是采样周期（trace 0.05、
    手柄 200Hz 0.005——混用会把段长放大 10 倍）。"""
    dur, gap = [], []
    cur = gap_n = 0
    for v in vals:
        on = v is not None and abs(v) >= thr
        if on:
            if gap_n and cur == 0 and dur:
                gap.append(gap_n * step_s)
            gap_n = 0
            cur += 1
        else:
            if cur:
                dur.append(cur * step_s)
                cur = 0
            gap_n += 1
    if cur:
        dur.append(cur * step_s)
    return len(dur), dur, gap


VARIANTS = ("base", "nonmerge_nocc", "nonmerge_any", "all_nocc", "all_any")


def _gate_kind(variant: str, *, safety_ok: bool, merging: bool,
               plan_valid: bool, layer, cc: float, why) -> str | None:
    """返回本变体下本拍的接管通道名（None=不接管）。

    base=生产现状（维护层 + pass/graze 交付）；nonmerge_*=去掉计划层/意图
    限制但保留并线排除；all_*=连并线也交给预瞄。*_nocc 保留碰撞代价排除，
    *_any 连有碰撞代价的计划避让也接管。安全链（状态/盲窗/陈旧/贴边逃生/
    朝向来源）所有变体一致。"""
    if not safety_ok:
        return None
    if variant == "base":
        if merging:
            return None
        if why in ("pass", "graze") and layer == 2 and cc <= 0.0:
            return "pass"
        if (not plan_valid) or (layer == 4 and cc <= 0.0):
            return "center"
        return None
    if variant.startswith("nonmerge") and merging:
        return None
    if variant.endswith("_nocc"):
        if cc > 0.0:
            return None
    return "all"


def analyze(stage: dict, variant: str = "base") -> dict:
    token = stage["session"]
    trace_path = (Path(os.environ.get("APPDATA", "")) / "MaaRacingMaster" /
                  "logs" / token / "trace" / "speedrush.jsonl")
    if not trace_path.exists():
        return {}
    pads = _pads(stage)
    start = dt.datetime.fromisoformat(stage["started_at"]).timestamp() * 1e9
    end = dt.datetime.fromisoformat(stage["ended_at"]).timestamp() * 1e9
    pl = _planner()
    precise_t = 0.0
    center_t = 0.0
    dwell_t = 0.0
    active = False
    u_seq: list[float | None] = []
    h_seq: list[float | None] = []
    active_flags: list[bool] = []
    why_off: collections.Counter = collections.Counter()
    why_on: collections.Counter = collections.Counter()
    labels: list[str] = []
    for line in open(trace_path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") is not None or not r.get("sel_dbg"):
            continue
        ts = r.get("ts_wall_ns") or 0
        if not (start <= ts <= end):
            continue
        lc = r.get("lat_ctrl") or {}
        plan = r.get("plan") or {}
        target = r.get("x_target")
        plan_valid = bool(plan.get("valid"))
        layer = plan.get("layer")
        cc = float(plan.get("collision_cost") or plan.get("cc") or 0.0)
        merging = (r.get("intent_phase") or "keep") == "merging"
        safety_ok = (
            r.get("state") == "CRUISE"
            and lc.get("csrc") == "model"
            and not lc.get("unver")
            and plan.get("reason") not in (
                "plan:stale", "plan:frame_shift", "plan:infeasible_corridor")
            and abs(lc.get("exec") or 0.0) <= WALL_ESCAPE_LANE
            and target is not None)
        if variant == "base":
            base_ok = safety_ok and not merging
            precise = (base_ok and r.get("sel_why") in ("pass", "graze")
                       and layer == 2 and cc <= 0.0)
            # 生产预瞄另外接管维护态（计划 layer=4 或无有效计划），同样 0.2s 驻留。
            center = base_ok and (not plan_valid or (layer == 4 and cc <= 0.0))
            precise_t = precise_t + 0.05 if precise else 0.0
            center_t = center_t + 0.05 if (center and plan_valid) else 0.0
            p_g = bool(precise and (active or precise_t + 1e-9 >= DWELL_S))
            c_g = bool(center and ((not plan_valid) or active
                                   or center_t + 1e-9 >= DWELL_S))
            gated = p_g or c_g
            kind = "pass" if p_g else "center" if c_g else None
        else:
            kind = _gate_kind(variant, safety_ok=safety_ok, merging=merging,
                              plan_valid=plan_valid, layer=layer, cc=cc,
                              why=r.get("sel_why"))
            dwell_t = dwell_t + 0.05 if kind is not None else 0.0
            gated = bool(kind is not None
                         and (active or dwell_t + 1e-9 >= DWELL_S))
            p_g = bool(gated and kind == "pass")
        u = None
        if r.get("state") != "CRUISE":
            label = "state:" + str(r.get("state"))
        elif lc.get("csrc") != "model":
            label = "csrc:" + str(lc.get("csrc"))
        elif lc.get("unver"):
            label = "unverified"
        elif plan.get("reason") in ("plan:stale", "plan:frame_shift",
                                    "plan:infeasible_corridor"):
            label = "plan_reason:" + str(plan.get("reason"))
        elif abs(lc.get("exec") or 0.0) > WALL_ESCAPE_LANE:
            label = "wall_escape"
        elif target is None:
            label = "no_target"
        elif gated:
            label = "on"
            why_on[str(r.get("sel_why"))] += 1
        elif variant == "base":
            if merging:
                label = "phase:merging"
            elif plan_valid and layer == 2 and cc > 0.0:
                label = "layer2_collision"
            elif plan_valid and layer == 2:
                label = "layer2_other:" + str(r.get("sel_why"))
            elif plan_valid and layer == 4 and cc > 0.0:
                label = "layer4_collision"
            elif plan_valid:
                label = "layer" + str(layer)
            else:
                label = "dwell_or_none"
        elif kind is None:
            if merging:
                label = "phase:merging"
            elif variant.endswith("_nocc"):
                label = "collision_cost"
            else:
                label = "policy_off"
        else:
            label = "dwell"
        if label != "on":
            why_off[label] += 1
        if gated and target is not None:
            pl.state.executed_lane = float(lc.get("exec") or 0.0)
            pl.state.v_lat = float(r.get("v_lat") or 0.0)
            pl.state.heading = float(r.get("heading") or 0.0)
            pl.state.yaw_rate = float(lc.get("r") or lc.get("rr") or 0.0)
            pl.state.v_mps = float(r.get("v_ego") or lc.get("vmp") or 41.0)
            # 覆盖口径下计划判「要跨道」（layer=2、无碰撞代价）的拍同样用
            # 机动态惩罚缩放（生产 preview_ok 同口径）。
            m = p_g or (variant.startswith("all") and plan_valid
                        and layer == 2 and cc <= 0.0)
            u = pl._preview_steer(
                0.05, float(target),
                float(r.get("steer_norm") or 0.0), maneuver=m)
        active = u is not None
        if not active:
            pl._preview_prev_u = None
        u_seq.append(u)
        h_seq.append(_nearest(pads, ts))
        active_flags.append(active)
        labels.append(label)
    n = len(u_seq)
    act = sum(active_flags)
    u_full = sum(1 for u in u_seq if u is not None and abs(u) >= FULL_STICK)
    h_full = sum(1 for h in h_seq if h is not None and abs(h) >= FULL_STICK)
    h_act = [(u, h) for u, h, a in zip(u_seq, h_seq, active_flags) if a]
    h_act_full = sum(1 for _, h in h_act if h is not None and abs(h) >= FULL_STICK)
    p_cnt, p_dur, p_gap = _runs(u_seq)
    h_cnt, h_dur, h_gap = _runs(h_seq)
    pad_dur, pad_gap = [], []
    if pads:
        pv = [p[1] for p in pads]
        _, pad_dur, pad_gap = _runs(pv, step_s=0.005)
    return {"stage": stage["dir"].name, "ticks": n, "active": act,
            "u_full": u_full, "h_full": h_full,
            "h_act": len(h_act), "h_act_full": h_act_full,
            "p_cnt": p_cnt, "p_dur": p_dur, "p_gap": p_gap,
            "h_cnt": h_cnt, "h_dur": h_dur, "h_gap": h_gap,
            "pad_dur": pad_dur, "pad_gap": pad_gap,
            "why_off": why_off, "why_on": why_on, "labels": labels,
            "h_seq": h_seq,
            "u_max": max((abs(u) for u in u_seq if u is not None), default=None),
            "h_max": max((abs(h) for h in h_seq if h is not None), default=None)}


def _med(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    ap.add_argument("--session", action="append",
                    help="只跑指定会话；可重复")
    ap.add_argument("--variant", action="append", choices=VARIANTS,
                    help="只跑指定口径；可重复；缺省全跑")
    args = ap.parse_args()
    sessions = set(args.session) if args.session else None
    stages = _manual_stages(Path(args.root), sessions)
    if not stages:
        raise SystemExit("没找到手动录制 meta.json")
    cov = _read_decision(DECISION_FILE).planner.preview_coverage_enabled
    prod = "all_nocc" if cov else "base"
    print(f"生产预瞄口径 = {prod}（preview_coverage_enabled={cov}）")
    variants = args.variant or list(VARIANTS)
    for variant in variants:
        t = collections.Counter()
        lab_n: collections.Counter = collections.Counter()
        lab_full: collections.Counter = collections.Counter()
        for st in stages:
            res = analyze(st, variant)
            if not res:
                continue
            if len(variants) == 1:
                print(f"== {res['stage']}")
                print(f"   拍 {res['ticks']}  预瞄接管 {res['active']} "
                      f"({res['active']/max(res['ticks'],1):.0%})  "
                      f"预瞄满杆 {res['u_full']} "
                      f"({res['u_full']/max(res['ticks'],1):.1%})")
                print(f"   人工满杆(同段全拍) {res['h_full']} "
                      f"({res['h_full']/max(res['ticks'],1):.1%})  "
                      f"人工(接管窗内) {res['h_act_full']}/{res['h_act']} "
                      f"({res['h_act_full']/max(res['h_act'],1):.1%})")
                print("   未接管原因 " + "  ".join(
                    f"{k}:{v}" for k, v in res["why_off"].most_common(8)))
            t.update({k: res[k] for k in ("ticks", "active", "u_full", "h_full",
                                          "h_act", "h_act_full")})
            for lab, h in zip(res["labels"], res["h_seq"]):
                lab_n[lab] += 1
                if h is not None and abs(h) >= FULL_STICK:
                    lab_full[lab] += 1
            for k, v in (("p_dur", res["p_dur"]), ("p_gap", res["p_gap"])):
                t[k + "_sum"] += sum(v)
                t[k + "_n"] += len(v)
        if not t["ticks"]:
            continue
        print(f"== 口径 {variant}")
        print(f"   预瞄接管 {t['active']/t['ticks']:.0%}  "
              f"预瞄满杆 {t['u_full']/t['ticks']:.1%}  "
              f"人工满杆 {t['h_full']/t['ticks']:.1%}  "
              f"接管窗内人工满杆 {t['h_act_full']/max(t['h_act'],1):.1%}")
        print(f"   预瞄脉冲时长 P50={t['p_dur_sum']/max(t['p_dur_n'],1):.2f}s "
              f"间隔 P50={t['p_gap_sum']/max(t['p_gap_n'],1):.2f}s")
        print("   未接管桶 " + "  ".join(
            f"{k}:{v/t['ticks']:.0%}" for k, v in lab_n.most_common(6)
            if k != "on"))


if __name__ == "__main__":
    main()
