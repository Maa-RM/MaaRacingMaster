# -*- coding: utf-8 -*-
"""P3-A A 线探针：纯居中连续窗口里的目标链对账（选择 → 发布 → 执行误差）。

只读 trace，不改任何生产代码。用法：

    .venv\\Scripts\\python.exe maaracing_master/plugins/_archive/speedrush/experiments/speedrush_agility/probe_a_chain.py \
        --session 20261119_104138

输出：
  1) 纯居中连续窗口清单（stage / seq 区间 / 时长 / 目标模式构成）
  2) 逐拍差分归因：Δego(选择帧) / Δc0(道路几何) / 区间模式翻转 / 发布 slew
  3) 假追赶候选：区间内 target=ego 且发布目标落后（slew 咬合）的那些拍
  4) 实际横移：v_lat 积分 vs executed 差分 vs 选择帧 ego 差分
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict

LANE_W_M = 3.5          # cal.lane_w_m（速度类读数的道/秒换算，仅用于米制展示）
HALF_LANE = 0.17        # (0.5 − ego_half_lane 0.27 − clearance 0.06)，selection._lane_interval
SLEW_LANE_PER_S = 2.0   # hysteresis.target_slew_lane_s
MERGE_SLEW = 4.0


def load(session: str) -> list[dict]:
    root = os.path.expandvars(r"%APPDATA%\MaaRacingMaster\logs")
    p = os.path.join(root, session, "trace", "speedrush.jsonl")
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") is None:
                out.append(d)
    return out


def fnum(x):
    return x if isinstance(x, (int, float)) else None


def windows(ticks: list[dict], whys=("center", "center:coarse"),
            min_len: int = 20):
    """同 stage、CRUISE、sel_layer=4 且 why 在集合内、seq 连续、dt<0.12 的窗口。"""
    out, cur = [], []
    for t in ticks:
        ok = (t.get("state") == "CRUISE" and t.get("sel_layer") == 4
              and t.get("sel_why") in whys and t.get("sel_dbg"))
        if not ok:
            if cur:
                out.append(cur)
            cur = []
            continue
        if cur:
            prev = cur[-1]
            if (t.get("stage_id") != prev.get("stage_id")
                    or t.get("seq") != prev.get("seq") + 1
                    or (t.get("dt") or 0) > 0.12):
                out.append(cur)
                cur = []
        cur.append(t)
    if cur:
        out.append(cur)
    return [w for w in out if len(w) >= min_len]


def rows(win: list[dict]) -> list[dict]:
    """把窗口展开为逐拍记录（全部横位量换算到「道」）。"""
    rs = []
    prev = None
    for t in win:
        sd = t.get("sel_dbg") or {}
        pl = t.get("plan") or {}
        ego = fnum(sd.get("ego"))
        tsel = fnum(sd.get("target"))
        c0 = fnum(sd.get("c0"))
        c8 = fnum(sd.get("c8"))
        pub = fnum(t.get("x_target"))
        exe = fnum(t.get("executed_lane"))
        dt = fnum(t.get("dt")) or 0.05
        r = dict(
            seq=t.get("seq"), fid=t.get("fid"), stage=t.get("stage_id"),
            why=t.get("sel_why"), reason=t.get("reason"),
            ego=ego, tsel=tsel, c0=c0, c8=c8, pub=pub, goal=fnum(t.get("goal")),
            exe=exe, v_lat=fnum(t.get("v_lat")), head=fnum(t.get("heading")),
            steer=fnum(t.get("steer_norm")), dt=dt,
            origin=fnum(sd.get("origin")), shift=fnum(sd.get("shift")),
            left=fnum(sd.get("left")), right=fnum(sd.get("right")),
            act=t.get("authority"), br=t.get("exec_branch"),
            plan_x=fnum(pl.get("x")), plan_v=fnum(pl.get("v")),
            plan_pc=fnum(pl.get("pc")), plan_fd=fnum(pl.get("fd")),
            plan_role=t.get("plan_role"), owner=t.get("steer_owner"),
            ro_source=t.get("ro_source"), ro_center=t.get("ro_center_src"),
            ro_reject=t.get("ro_reject"),
        )
        # 区间内（target 取 ego）判定：selection._center_target 区间内直接返回 ego
        r["in_iv"] = (ego is not None and tsel is not None
                      and abs(tsel - ego) < 2e-3)
        # 区间边界位置（用 c0 近似 center_at(0)，lookahead=0）
        if c0 is not None:
            r["hi"] = c0 + HALF_LANE
            r["lo"] = c0 - HALF_LANE
        # 发布滞后：本拍 slew 是否咬合（|Δpub| ≈ slew*dt 且 |pub−goal|>1e-4）
        r["slew_bound"] = False
        r["lag"] = None
        if prev is not None and pub is not None and prev["pub"] is not None:
            dpub = pub - prev["pub"]
            lim = SLEW_LANE_PER_S * dt
            r["dpub"] = dpub
            if r["goal"] is not None:
                rem = r["goal"] - pub
                r["lag"] = rem
                if abs(abs(dpub) - lim) < 0.02 * lim / 0.1 and abs(rem) > 5e-3:
                    r["slew_bound"] = True
        rs.append(r)
        prev = r
    return rs


def summarize(win, rs):
    sd = win[0].get("sel_dbg") or {}
    n_in = sum(1 for r in rs if r["in_iv"])
    print(f"-- 窗口 stage={win[0].get('stage_id')} seq {rs[0]['seq']}..{rs[-1]['seq']}"
          f" n={len(rs)} t={len(rs)*0.05:.1f}s why={Counter(r['why'] for r in rs)}"
          f" in_iv={n_in}/{len(rs)}")
    print(f"   origin={sd.get('origin')} shift={sd.get('shift')} "
          f"left={sd.get('left')} right={sd.get('right')} c0={sd.get('c0')}")
    egos = [r["ego"] for r in rs if r["ego"] is not None]
    pubs = [r["pub"] for r in rs if r["pub"] is not None]
    if egos:
        print(f"   ego∈[{min(egos):+.3f},{max(egos):+.3f}] "
              f"pub∈[{min(pubs):+.3f},{max(pubs):+.3f}]")
    # 逐拍差分中位数（道）
    def med(vals):
        vals = sorted(vals)
        return None if not vals else vals[len(vals) // 2]
    d_ego = [abs(b["ego"] - a["ego"]) for a, b in zip(rs, rs[1:])
             if a["ego"] is not None and b["ego"] is not None]
    d_c0 = [abs(b["c0"] - a["c0"]) for a, b in zip(rs, rs[1:])
            if a["c0"] is not None and b["c0"] is not None]
    d_sel = [abs(b["tsel"] - a["tsel"]) for a, b in zip(rs, rs[1:])
             if a["tsel"] is not None and b["tsel"] is not None]
    d_pub = [abs(b["pub"] - a["pub"]) for a, b in zip(rs, rs[1:])
             if a["pub"] is not None and b["pub"] is not None]
    d_exe = [abs(b["exe"] - a["exe"]) for a, b in zip(rs, rs[1:])
             if a["exe"] is not None and b["exe"] is not None]
    print(f"   逐拍|差|中位: ego={med(d_ego)} c0={med(d_c0)} sel={med(d_sel)}"
          f" pub={med(d_pub)} exe={med(d_exe)}")
    nb = sum(1 for r in rs if r["slew_bound"])
    print(f"   slew 咬合拍={nb}/{len(rs)}  发布−选择 最大|差|="
          f"{max((abs((r['pub'] or 0)-(r['goal'] or 0)) for r in rs), default=0):.3f}")
    # 归因：选择目标差分拆成 c0 变化 / ego 变化 / 模式
    attr = Counter()
    for a, b in zip(rs, rs[1:]):
        if a["tsel"] is None or b["tsel"] is None:
            continue
        if a["in_iv"] and b["in_iv"]:
            attr["ego 直通(in→in)"] += 1
        elif not a["in_iv"] and not b["in_iv"]:
            attr["夹边界(out→out)"] += 1
        elif a["in_iv"] and not b["in_iv"]:
            attr["模式翻转 in→out"] += 1
        else:
            attr["模式翻转 out→in"] += 1
    print("   逐拍归因:", dict(attr))
    return dict(n=len(rs), in_iv=n_in, slew=nb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="20261119_104138")
    ap.add_argument("--min-len", type=int, default=20)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--detail", type=int, default=0)
    a = ap.parse_args()
    ticks = load(a.session)
    ws = windows(ticks, min_len=a.min_len)
    print(f"session={a.session} ticks={len(ticks)} 纯居中窗口={len(ws)}")
    ranked = sorted(ws, key=len, reverse=True)
    allrs = []
    for w in ranked[:a.top]:
        rs = rows(w)
        allrs.append((w, rs))
        summarize(w, rs)
    # 全窗口合并统计
    allr = [r for _, rs in allrs for r in rs]
    print("\n== 合并统计（前 %d 个窗口） ==" % len(allrs))
    n = len(allr)
    print(f"拍数={n} 区间内={sum(1 for r in allr if r['in_iv'])} "
          f"slew咬合={sum(1 for r in allr if r['slew_bound'])}")
    print("ro_source:", Counter(r["ro_source"] for r in allr).most_common())
    # 假追赶候选：区间内 target=ego，发布滞后同号持续
    runs = []
    cur = []
    for r in allr:
        if r["in_iv"] and r["slew_bound"]:
            cur.append(r)
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
    if len(cur) >= 2:
        runs.append(cur)
    print(f"\n区间内且 slew 咬合的连续段（≥2 拍）={len(runs)}")
    for run in runs[:12]:
        print("  seq", run[0]["seq"], "..", run[-1]["seq"], "n=", len(run),
              "lag", round(run[0]["lag"], 3), "->", round(run[-1]["lag"], 3),
              "ego Δ", round(run[-1]["ego"] - run[0]["ego"], 3),
              "exe Δ", round((run[-1]["exe"] or 0) - (run[0]["exe"] or 0), 3))
    if a.detail:
        k = a.detail
        for w, rs in allrs:
            if len(rs) >= k:
                print("\n== 明细 seq", rs[0]["seq"], "..", rs[-1]["seq"])
                hdr = ("seq  ego    tsel   pub    goal   c0    in  d_ego  d_sel"
                       "  d_pub  lag   exe    vlat   steer  plan_x")
                print(hdr)
                prev = None
                for r in rs:
                    print(f"{r['seq']:4d} {r['ego']:+.3f} {r['tsel']:+.3f} "
                          f"{r['pub']:+.3f} {r['goal']:+.3f} {r['c0']:+.3f} "
                          f"{int(r['in_iv'])} "
                          f"{(r['ego']-(prev['ego'] if prev else r['ego'])):+.4f} "
                          f"{(r['tsel']-(prev['tsel'] if prev else r['tsel'])):+.4f} "
                          f"{r.get('dpub',0):+.4f} "
                          f"{(r['lag'] or 0):+.3f} {(r['exe'] or 0):+.3f} "
                          f"{(r['v_lat'] or 0):+.3f} {(r['steer'] or 0):+.2f} "
                          f"{(r['plan_x'] or 0):+.3f}")
                    prev = r
                break


if __name__ == "__main__":
    main()
