"""路线起意门复算探针（speedrush，离线）。

**问什么**：路线层在「该提前并线」的拍上为什么没并？把 `_tick_keep` 里
提前超车分支与紧急逃生分支的可行性过滤，按生产同序在 trace 上复算一遍，
统计第一失败原因。属于观测工具：不接控制，结论只用来看方向。

口径（与生产逐条同序，2026-11-10 核对 route.py）：

- 候选集 `alts` = 非承诺道 + 粗层合格（`space`/`unknown_car` 不算合格）。
- 提前超车 feas = `_clear(c)` ∧ 横穿路径空 ∧ 穿越清障时间够——**不查目标
  道到站时间**（生产的提前分支没有这一道门，与紧急分支不一致）。
- 紧急逃生 feas = 目标道「空闲或净空 ≥ 本道 + switch_gain_m」∧ 路径空 ∧
  目标道到站时间 ≥ merge_safe_ttc_s ∧ 清障时间够。
- 时间表 `lane_ttc` 按 `_lane_ttc_map` 的口径从 scene.objs 重建：
  `round((x-c0)/lane_w)` 归道、只收 0<t≤视界的车。

近似项：`blocked_s`（粗层占用连续时长）没进 trace，紧急分支只按时间表
`ttc_s`（`cur_ttc ≤ overtake_need` 连续 ≥ urgent_confirm_s）判是否已确认；
`passing` 取上一拍选择层 why∈{pass,graze}；`drifted_free` 按「实测道为空且
偏离承诺道 ≥ anchor_follow_lane」近似。这些只影响「哪些拍算进入分支」，
不影响分支内五道门的顺序。

用法::

    .venv/Scripts/python.exe -X utf8 probe_route_reject.py <会话 ...>
"""

from __future__ import annotations

import collections
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402

from maaracing_master.plugins.speedrush import DECISION_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.config import _read_decision  # noqa: E402
from maaracing_master.plugins.speedrush.route import (  # noqa: E402
    RouteParams, lane_move_time, overtake_need_s, urgent_need_s)


def _params():
    cfg = _read_decision(DECISION_FILE)
    c, s, r, pl = cfg.coarse, cfg.selection, cfg.route, cfg.planner
    p = RouteParams(
        switch_gain_m=c.switch_margin_m,
        proxy_speed_mps=c.range_speed_mps,
        urgent_confirm_s=r.urgent_confirm_s,
        opportunity_confirm_s=r.opportunity_confirm_s,
        cancel_unsafe_s=r.cancel_unsafe_s,
        cancel_cooldown_s=r.cancel_cooldown_s,
        finish_tol_lane=s.center_band_lane,
        finish_v_tol_lane_s=r.finish_v_tol_lane_s,
        merge_max_s=r.merge_max_s,
        soft_car=r.soft_car,
        reaction_s=s.reaction_s,
        move_margin_s=s.margin_s,
        urgent_blind_m=r.urgent_blind_m,
        merge_safe_ttc_s=r.merge_safe_ttc_s,
        pass_hold=r.pass_hold,
        overtake=r.overtake,
        request_hold=r.request_hold,
        request_gap_s=r.request_gap_s,
        request_intent_age_s=r.request_intent_age_s,
        anchor_follow=r.anchor_follow,
        anchor_follow_s=r.anchor_follow_s,
        anchor_follow_lane=r.anchor_follow_lane,
        car_clear_lane=s.car_half_lane + s.ego_half_lane,
        k_lat_lane_s=pl.k_lat_lane_s,
        tau_heading_s=pl.tau_heading_s,
        heading_max_rad=pl.heading_max_rad)
    return p, pl, urgent_need_s(p), overtake_need_s(p)


def _lane_ttc(rec: dict) -> dict[int, float]:
    sd = rec.get("sel_dbg") or {}
    c0 = (sd.get("c0") or 0.0) * 3.5
    out: dict[int, float] = {}
    for o in (rec.get("scene") or {}).get("objs") or []:
        if not o or o[0] != "car" or len(o) < 16:
            continue
        if not o[8] or o[2] is None:
            continue
        t = o[2]
        if not 0.0 < t <= 7.0:
            continue
        k = int(round((o[1] - c0) / 3.5))
        if k not in out or t < out[k]:
            out[k] = t
    return out


def _lanes(rec: dict) -> dict[float, dict]:
    out = {}
    for cc in (rec.get("coarse") or {}).get("cand") or []:
        out[float(cc[0])] = {"lane": float(cc[0]), "free": cc[1],
                             "blocker": cc[3], "bx": cc[5]}
    return out


def _path_clear(by: dict, v_ref: float, lane: float) -> bool:
    lo, hi = sorted((int(round(v_ref)), int(lane)))
    for k in range(lo + 1, hi):
        c = by.get(float(k))
        if c is None or c["blocker"] is not None:
            return False
    return True


def _cross_ok(cur: dict, v_ref: float, lane: float, ttc_cur, p, pl) -> bool:
    if cur["bx"] is None or ttc_cur is None:
        return True
    d = 1.0 if lane > v_ref else -1.0
    goal = cur["bx"] + d * p.car_clear_lane
    if (goal - v_ref) * d <= 0.0:
        return True
    need = lane_move_time(abs(goal - v_ref), k_lat_lane_s=pl.k_lat_lane_s,
                          tau_heading_s=pl.tau_heading_s,
                          heading_max_rad=pl.heading_max_rad)
    return need <= ttc_cur


def analyze(sess: str) -> None:
    p, pl, urg, ovt = _params()
    ticks, stage_s = P.load(sess)
    early = collections.Counter()
    urg_c = collections.Counter()
    n_early = n_urg = n_early_old = 0
    run = run_old = gap = 0.0
    last_ts = None
    prev_pass = False
    for rec in ticks:
        rt = rec.get("route") or {}
        now = rec["ts_mono_ns"]
        dt = 0.0 if last_ts is None else max(0.0, (now - last_ts) / 1e9)
        last_ts = now
        if rt.get("ph") != "keep" or rt.get("ref") is None:
            run = run_old = gap = 0.0
            prev_pass = rec.get("sel_why") in ("pass", "graze")
            continue
        by = _lanes(rec)
        if not by:
            run = run_old = gap = 0.0
            prev_pass = rec.get("sel_why") in ("pass", "graze")
            continue
        ref = float(rt["ref"])
        v_ref = float((rec.get("coarse") or {}).get("ego") or ref)
        ttc = _lane_ttc(rec)
        ttc_cur = None
        for k in {int(round(ref)), int(round(v_ref))}:
            if k in ttc:
                ttc_cur = ttc[k] if ttc_cur is None else min(ttc_cur, ttc[k])
        hot = ttc_cur is not None and 0.0 < ttc_cur <= ovt
        run_old = run_old + dt if hot else 0.0
        # 生产现行规则（route.ttc_hold_s）：缺样不清零，超过宽限才作废
        if hot:
            run, gap = run + dt, 0.0
        elif ttc_cur is not None:
            run, gap = 0.0, 0.0
        else:
            gap += dt
            run = run if gap <= p.ttc_hold_s else 0.0
        prev_pass = rec.get("sel_why") in ("pass", "graze")
        cur = by.get(round(ref))
        k = float(round(v_ref))
        drifted = (abs(v_ref - ref) >= p.anchor_follow_lane
                   and by.get(k) is not None and by[k]["blocker"] is None)
        if not hot or run + 1e-9 < p.urgent_confirm_s \
                or prev_pass or drifted or cur is None or ttc_cur is None:
            continue
        if urg < ttc_cur <= ovt and run_old + 1e-9 >= p.urgent_confirm_s:
            n_early_old += 1
        alts = [x for x in by.values()
                if x["lane"] != ref and x["blocker"] not in ("space", "unknown_car")]
        if urg < ttc_cur <= ovt:
            n_early += 1

            def why(x) -> str | None:
                if abs(v_ref - x["lane"]) <= 0.3:
                    return "already_there"
                if x["blocker"] is not None:
                    return "not_clear"
                if not _path_clear(by, v_ref, x["lane"]):
                    return "path_blocked"
                if not _cross_ok(cur, v_ref, x["lane"], ttc_cur, p, pl):
                    return "cross_not_clear"
                return None

            if any(why(x) is None for x in alts):
                early["COMMIT"] += 1
            else:
                cands = [x for x in alts if abs(x["lane"] - v_ref) == 1.0] \
                    or list(alts)
                if not cands:
                    early["no_candidate"] += 1
                else:
                    c = min(cands,
                            key=lambda x: (abs(x["lane"] - v_ref), x["lane"]))
                    early[why(c) or "COMMIT"] += 1
        if ttc_cur <= urg:
            n_urg += 1
            feas = []
            for x in alts:
                if not (x["blocker"] is None
                        or (cur["free"] is not None
                            and x["free"] >= cur["free"] + p.switch_gain_m)):
                    continue
                if not _path_clear(by, v_ref, x["lane"]):
                    continue
                tx = ttc.get(int(round(x["lane"])))
                if tx is not None and tx < p.merge_safe_ttc_s:
                    continue
                if not _cross_ok(cur, v_ref, x["lane"], ttc_cur, p, pl):
                    continue
                feas.append(x)
            if not feas:
                cands = [x for x in alts if abs(x["lane"] - v_ref) == 1.0] \
                    or list(alts)
                if not cands:
                    urg_c["no_candidate"] += 1
                else:
                    c = min(cands, key=lambda x: (abs(x["lane"] - v_ref), x["lane"]))
                    if c["blocker"] is not None and not (
                            cur["free"] is not None
                            and c["free"] >= cur["free"] + p.switch_gain_m):
                        urg_c["not_wider"] += 1
                    elif not _path_clear(by, v_ref, c["lane"]):
                        urg_c["path_blocked"] += 1
                    else:
                        tx = ttc.get(int(round(c["lane"])))
                        if tx is not None and tx < p.merge_safe_ttc_s:
                            urg_c["too_soon"] += 1
                        else:
                            urg_c["cross"] += 1
    print(f"== {sess}  stage {stage_s:.0f}s  ticks {len(ticks)}")
    print(f"   提前带拍 {n_early}（旧规则缺样清零只到 {n_early_old}）  "
          f"{dict(early)}")
    print(f"   紧急带拍 {n_urg}  无可行 {sum(urg_c.values())}  {dict(urg_c)}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
