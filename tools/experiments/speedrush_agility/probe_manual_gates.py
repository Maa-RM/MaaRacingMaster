"""手动局起意拍：贴边候选被哪道门挡住（speedrush）。

**问什么**：两局手动录像的 49 个极限超车事件里，判定前 1.2 s 的起意拍有
25 个还是 center/commit。同一拍上有没有一个按生产几何与运动学可行的贴边
候选？有的话被哪道门拒；没有的话是车不在窗内、还是时间/占用/路径。

**口径**：
- 事件与起意拍对齐、自车横位换算复用 ``probe_manual_events``（同一份游戏
  日志、同一批手动阶段）。
- 车表重建：``sel_dbg.car``（x, 到站秒）+ ``otab.t``（安全/纵深/收拢速度）
  + ``car_pos``，与 ``probe_maneuver_window._car_rows`` 同源；``t=None`` 的
  同道近车按纵深 ≤15 m 近似 ``near``（只影响挡道候选）。
- 贴边可行性：直接调用生产选择层的 ``_pass_x_side`` / ``_pass_x_candidates``
  / ``_reachable`` / ``_pass_window_blocked`` / ``_pass_path_blocked``，参数取
  ``decision.json`` 现役值；并线承诺期只对**邻道**目标传承诺道跨度（同道
  擦边是安全动作，生产不夹）。
- 只看车，不看奖励/金币：奖励层（1）高于贴边，但本批 center/commit 拍没有
  奖励选中；金币层（3）低于贴边，不影响「贴边为何没选」。

**判定**：
- ``chosen``：起意拍已是 pass/graze。
- ``feasible_missed``：扫描到可行贴边，但起意拍是 center/commit。
- ``no_candidate:<门>``：所有候选被同一道门拒（inside_gate/slow/blocked/
  path/edge/outside_window）。
- ``commit_span``：并线承诺期、邻道候选目标出承诺道跨度被夹掉。

用法::

    .venv/Scripts/python.exe -X utf8 probe_manual_gates.py \
        --log <g112.log> --session 094822 --session 103934 \
        --out manual_gates.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_manual_events as me  # noqa: E402

from maaracing_master.plugins.speedrush import DECISION_FILE  # noqa: E402
from maaracing_master.plugins.speedrush.config import _read_decision  # noqa: E402
from maaracing_master.plugins.speedrush.selection import (  # noqa: E402
    Obj, RoadBand, RoadFrame, SelectionParams, _in_interval, _lane_span,
    _pass_alongside_s, _pass_path_blocked, _pass_window_blocked, _pass_x,
    _pass_x_candidates, _pass_x_side, _reachable, _t_move)

CFG = _read_decision(DECISION_FILE)
S = CFG.selection
PL = CFG.planner


def _params(lane_w: float) -> SelectionParams:
    """与 decision 构造同一组参数（lane_w 取 trace 场景值）。"""
    return SelectionParams(
        lane_w_m=lane_w,
        horizon_s=S.horizon_s, max_waypoints=S.max_waypoints,
        graze_gap_lane=S.graze_gap_lane,
        center_band_lane=S.center_band_lane,
        opportunity_tol_lane=S.opportunity_tol_lane,
        k_lat_lane_s=PL.k_lat_lane_s,
        tau_heading_s=PL.tau_heading_s,
        heading_max_rad=PL.heading_max_rad,
        reaction_s=S.reaction_s, margin_s=S.margin_s,
        wall_margin_lane=S.wall_margin_lane,
        center_lookahead_m=S.center_lookahead_m,
        switch_dwell_s=S.switch_dwell_s,
        layer_dwell=S.layer_dwell,
        event_replan=S.event_replan,
        request_hold=CFG.route.request_hold,
        car_half_lane=S.car_half_lane,
        ego_half_lane=S.ego_half_lane,
        lane_clearance_lane=S.lane_clearance_lane,
        preposition_enabled=S.preposition_enabled,
        preposition_horizon_s=S.preposition_horizon_s,
        preposition_max_lane=S.preposition_max_lane,
        preposition_bias_lane=S.preposition_bias_lane,
        pass_preposition_enabled=S.pass_preposition_enabled,
        pass_hold_enabled=S.pass_hold_enabled,
        pass_hold_s=S.pass_hold_s,
        pass_min_gap_lane=S.pass_min_gap_lane,
        pass_in_commit_enabled=S.pass_in_commit_enabled,
        pass_horizon_s=S.pass_horizon_s,
        pass_graze_adjacent_enabled=S.pass_graze_adjacent_enabled,
        pass_no_surrender_enabled=S.pass_no_surrender_enabled,
        arrival_guard_enabled=S.arrival_guard_enabled,
        pass_reach_lane=S.pass_reach_lane,
        pass_lean_min_s=S.pass_lean_min_s,
        pass_clear_len_m=S.pass_clear_len_m,
        pass_min_closing_mps=S.pass_min_closing_mps,
        pass_alongside_cap_s=S.pass_alongside_cap_s)


def _road(rec: dict) -> tuple[RoadFrame, float]:
    scene = rec.get("scene") or {}
    road = scene.get("road") or {}
    bands = []
    for row in road.get("bands") or []:
        try:
            bands.append(RoadBand(float(row[0]), float(row[1]),
                                  None if row[2] is None else float(row[2]),
                                  None if row[3] is None else float(row[3]),
                                  str(row[4]), None, 0.0))
        except (TypeError, ValueError, IndexError):
            continue
    lane_w = float(scene.get("lane_w") or 3.5)
    rf = RoadFrame(tuple(bands), road.get("w"), road.get("l"),
                   road.get("r"), not bool(scene.get("stale")))
    return rf, lane_w


def _cars(rec: dict, ego: float, p: SelectionParams) -> list[Obj]:
    dbg = rec.get("sel_dbg") or {}
    half = (p.car_half_lane + p.ego_half_lane) * p.lane_w_m
    pool = []
    for row in (rec.get("otab") or {}).get("t") or []:
        try:
            if row[0] != "car" or row[2] is None:
                continue
            pool.append({"x": float(row[2]), "z": row[4], "safe": bool(row[7]),
                         "oid": int(row[1]), "v": row[11]})
        except (TypeError, ValueError, IndexError):
            continue
    out = []
    for c in dbg.get("car") or []:
        try:
            x, t = float(c[0]), c[1]
        except (TypeError, ValueError, IndexError):
            continue
        match = None
        for i, o in enumerate(pool):
            if abs(o["x"] + ego - x) < 0.02:
                match = pool.pop(i)
                break
        z = None if match is None else match["z"]
        near = (t is None and abs(x - ego) <= half
                and z is not None and float(z) <= 15.0)
        out.append(Obj("car", x, None if t is None else float(t),
                       oid=-1 if match is None else match["oid"],
                       v_close=None if match is None else match["v"],
                       safe=True if match is None else match["safe"],
                       near=near))
    return out


def _scan(cars: list[Obj], ego: float, p: SelectionParams, road: RoadFrame,
          committed: bool, commit_x_m: float | None
          ) -> tuple[list[dict], collections.Counter]:
    """扫描所有车的贴边可行性；返回可行候选与拒绝原因计数。"""
    half = (p.car_half_lane + p.ego_half_lane) * p.lane_w_m
    hold = p.pass_hold_s if p.pass_hold_enabled else 0.0
    horizon = p.pass_horizon_s if p.pass_horizon_s > 0 else p.horizon_s
    reach = p.pass_reach_lane * p.lane_w_m
    sp = (_lane_span(commit_x_m, p, road)
          if committed and commit_x_m is not None else None)
    feas: list[dict] = []
    rej: collections.Counter = collections.Counter()
    for c in cars:
        if not c.safe:
            rej["unsafe"] += 1
            continue
        if abs(c.x - ego) > max(half, reach):
            rej["too_far"] += 1
            continue
        if not ((c.t is not None and -hold <= c.t <= horizon)
                or (c.near and abs(c.x - ego) <= half)):
            rej["outside_window"] += 1
            continue
        t_eff = 0.0 if c.t is None else c.t
        adjacent = abs(c.x - ego) > half
        if adjacent and not p.pass_graze_adjacent_enabled:
            rej["adjacent_off"] += 1
            continue
        if adjacent and t_eff < p.pass_lean_min_s:
            rej["inside_gate"] += 1
            continue
        others = [o for o in cars if o is not c]
        alongside = _pass_alongside_s(c.v_close, p)
        inflight = c.t is None or t_eff > 0.0 or not p.pass_hold_enabled
        if adjacent:
            one = _pass_x_side(c, road, p, 1.0 if ego >= c.x else -1.0)
            if one is None:
                rej["edge"] += 1
                continue
            xs = [one]
        elif t_eff > 0.0:
            xs = _pass_x_candidates(c, road, p)
        else:
            x = _pass_x(c, road, p)
            xs = [] if x is None else [x]
        if sp is not None and adjacent:
            span_ok = [x for x in xs if _in_interval(x, sp)]
            if xs and not span_ok:
                rej["commit_span"] += 1
                continue
            xs = span_ok
        ok = []
        for x in xs:
            if abs(x - c.x) < half:
                rej["inside_car"] += 1
                continue
            if c.t is not None and inflight \
                    and not _reachable(x, ego, t_eff, p):
                if adjacent or not p.pass_no_surrender_enabled:
                    rej["slow"] += 1
                    continue
            if (alongside > 0.0 or inflight) and _pass_window_blocked(
                    others, x, t_eff, alongside, p):
                rej["blocked"] += 1
                continue
            if abs(x - ego) > p.aligned_lane * p.lane_w_m \
                    and _pass_path_blocked(
                        others, ego, x,
                        _t_move(x - ego, p) + p.reaction_s, p):
                rej["path"] += 1
                continue
            ok.append(x)
        if ok:
            feas.append({"car": c, "x": ok[0], "t": t_eff,
                         "slack": (c.t - _t_move(ok[0] - ego, p)
                                   if c.t is not None else 0.0)})
    return feas, rej


def _verdict(sel: str, feas: list[dict], rej: collections.Counter) -> str:
    if sel in ("pass", "graze"):
        return "chosen"
    if feas:
        return "feasible_missed"
    if rej:
        return "no_candidate:" + rej.most_common(1)[0][0]
    return "no_candidate:empty"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(
        os.environ.get("APPDATA", ""), "MaaRacingMaster"))
    ap.add_argument("--log", default=os.environ.get("MAAR_G112_LOG"))
    ap.add_argument("--session", action="append")
    ap.add_argument("--out", default="manual_gates.csv")
    args = ap.parse_args()
    if not args.log:
        raise SystemExit("缺少游戏日志：设 MAAR_G112_LOG 或传 --log")
    root = Path(args.root)
    sessions = set(args.session) if args.session else None
    stages = me.manual_stages(root, sessions)
    if not stages:
        raise SystemExit("没找到手动录制 meta.json")

    traces: dict[str, list[dict]] = {}
    rows: list[dict] = []
    for st in stages:
        token = st["session"]
        tp = root / "logs" / token / "trace" / "speedrush.jsonl"
        if not tp.exists():
            print(f"[skip] {token}: 缺 trace")
            continue
        trace = traces.setdefault(token, me.load_trace(str(tp)))
        for stamp, kind in me.parse_events(args.log, st["start"], st["end"]):
            if kind != "overtake":
                continue
            epoch = stamp.timestamp() * 1e9
            cur = me.nearest(trace, epoch)
            early = me.nearest(trace, epoch - 1.2e9)
            if cur is None or early is None:
                continue
            dbg = early.get("sel_dbg") or {}
            ego = dbg.get("ego")
            if ego is None:
                continue
            road, lane_w = _road(early)
            p = _params(lane_w)
            cars = _cars(early, float(ego), p)
            sel = str(early.get("sel_why"))
            committed = early.get("intent_phase") == "merging"
            lane = early.get("intent_lane")
            commit_x = (float(lane) * lane_w
                        if lane is not None else None)
            feas, rej = _scan(cars, float(ego), p, road, committed, commit_x)
            feas.sort(key=lambda f: f["slack"], reverse=True)
            best = feas[0] if feas else None
            # 多前导拍：看「更早的时候有没有可行贴边、当时选了什么」。
            # 起意拍提前的判据不是 −1.2s 那一拍有没有候选（那时车已经到眼前），
            # 而是候选第一次可行时程序有没有选它。
            leads = []
            for led in (3.0, 2.75, 2.5, 2.25, 2.0, 1.75, 1.5, 1.25, 1.0, 0.75):
                e_tick = me.nearest(trace, epoch - led * 1e9)
                if e_tick is None:
                    continue
                d2 = e_tick.get("sel_dbg") or {}
                eg2 = d2.get("ego")
                if eg2 is None:
                    continue
                r2, lw2 = _road(e_tick)
                p2 = _params(lw2)
                c2 = _cars(e_tick, float(eg2), p2)
                lane2 = e_tick.get("intent_lane")
                f2, _ = _scan(c2, float(eg2), p2, r2,
                              e_tick.get("intent_phase") == "merging",
                              None if lane2 is None else float(lane2) * lw2)
                leads.append((led, len(f2), str(e_tick.get("sel_why"))))
            first_feas = next((l for l in leads if l[1] > 0), None)
            first_pass = next((l for l in leads if l[2] in ("pass", "graze")),
                              None)
            ego_j = (cur.get("scene") or {}).get("ego_m")
            ego_12 = (early.get("scene") or {}).get("ego_m")
            rows.append({
                "session": token,
                "phase": st["phase"],
                "time": stamp.strftime("%H:%M:%S.%f")[:-3],
                "fid": cur.get("fid"),
                "sel_12": sel,
                "committed": committed,
                "lane": lane,
                "verdict": _verdict(sel, feas, rej),
                "feas_n": len(feas),
                "best_x": None if best is None else round(best["x"], 2),
                "best_t": None if best is None else round(best["t"], 2),
                "best_slack": None if best is None else round(best["slack"], 2),
                "sel": cur.get("sel_why"),
                "ego_12": ego_12,
                "ego_off_12": me.lane_off(ego_12),
                "ego_m": ego_j,
                "ego_off": me.lane_off(ego_j),
                "plan_12": (early.get("plan") or {}).get("x"),
                "plan": (cur.get("plan") or {}).get("x"),
                "rej": " ".join(f"{k}:{v}" for k, v in rej.most_common()),
                "cars": "; ".join(
                    f"{c.x:.2f}/{c.t if c.t is None else round(c.t, 2)}"
                    for c in cars[:8]),
                "first_feas": None if first_feas is None else first_feas[0],
                "first_feas_sel": None if first_feas is None else first_feas[2],
                "first_pass": None if first_pass is None else first_pass[0],
                "lead_trace": " ".join(f"{l:.2f}:{n}:{s}"
                                       for l, n, s in leads),
            })

    rows.sort(key=lambda r: (r["session"], r["time"]))
    if rows:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} rows -> {args.out}")
    for r in rows:
        print(f"{r['session'][-6:]} {r['time']} {r['sel_12']:12s} "
              f"{'M' if r['committed'] else ' '} {r['verdict']:22s} "
              f"feas={r['feas_n']} best={r['best_x']}/{r['best_t']} "
              f"早可行={r['first_feas']}({r['first_feas_sel']}) "
              f"早pass={r['first_pass']} {r['rej']}")
    cnt = collections.Counter(r["verdict"] for r in rows)
    print("== 判定", dict(cnt))
    missed = [r for r in rows if r["verdict"] != "chosen"]
    if missed:
        print(f"== 未选 pass 的 {len(missed)} 个，按起意层：",
              dict(collections.Counter(r["sel_12"] for r in missed)))
        print(f"   其中并线承诺期 {sum(1 for r in missed if r['committed'])}")
        fe = [r for r in missed if r["verdict"] == "feasible_missed"]
        if fe:
            print(f"   可行但没选 {len(fe)}：起意层 "
                  f"{dict(collections.Counter(r['sel_12'] for r in fe))}")
        earlier = [r for r in missed
                   if r["first_feas"] is not None and r["first_feas"] > 1.2]
        print(f"   更早（>1.2s）就有可行贴边：{len(earlier)}/{len(missed)}"
              f"；当时选择 "
              f"{dict(collections.Counter(r['first_feas_sel'] for r in earlier))}")
        print(f"   出现过 pass/graze 选中的："
              f"{sum(1 for r in missed if r['first_pass'] is not None)}")


if __name__ == "__main__":
    main()
