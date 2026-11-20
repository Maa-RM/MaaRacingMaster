# -*- coding: utf-8 -*-
"""P3-A A 线探针（定稿）：纯居中窗口的目标链分项归因。

目标链模型（已用 trace 自检）：
    lane_center = c0 + route.ref          # c0=road.center_at(0)/lw（选择帧），ref 见 route.ref
    t_sel = clamp(ego, lane_center ± 0.17) # selection._center_target；区间内即为 ego
    t_pub = slew(t_sel, 2.0 道/s)          # decision 输出 slew
执行侧误差（planner._pd）= t_pub − (executed + v_lat·0.3)

用法：.venv\\Scripts\\python.exe maaracing_master/plugins/_archive/speedrush/experiments/speedrush_agility/probe_a_final.py
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter

HALF = 0.17
LW = 3.5
SLEW = 2.0
TAU = 0.3


def load(session):
    p = os.path.join(os.path.expandvars(r"%APPDATA%\MaaRacingMaster\logs"),
                     session, "trace", "speedrush.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") is None and d.get("sel_dbg"):
                out.append(d)
    return out


def f(x, d=None):
    return x if isinstance(x, (int, float)) else d


def stat(v):
    v = [x for x in v if x is not None]
    if not v:
        return "n=0"
    v2 = sorted(abs(x) for x in v)
    n = len(v2)
    q = lambda p: v2[min(n - 1, int(round(p * (n - 1))))]
    mean = sum(v2) / n
    return f"n={n} 中位={q(.5):.4f} p90={q(.9):.4f} 最大={v2[-1]:.4f} 均值={mean:.4f}"


def row(t):
    sd = t.get("sel_dbg") or {}
    rt = t.get("route") or {}
    co = t.get("coarse") or {}
    pl = t.get("plan") or {}
    ego, c0, tsel = f(sd.get("ego")), f(sd.get("c0")), f(sd.get("target"))
    ref = f(rt.get("ref"))
    if ref is None:
        ref = f(co.get("lane"), 0.0)
    base = None if c0 is None else c0 + (ref or 0.0)
    mode = "n/a"
    if ego is not None and base is not None and tsel is not None:
        if abs(tsel - ego) < 2e-3:
            mode = "in"
        elif abs(tsel - (base + HALF)) < 0.012:
            mode = "hi"
        elif abs(tsel - (base - HALF)) < 0.012:
            mode = "lo"
    return dict(
        seq=t.get("seq"), stage=t.get("stage_id"), why=t.get("sel_why"),
        dt=f(t.get("dt"), 0.05), ego=ego, c0=c0, ref=ref, base=base,
        tsel=tsel, pub=f(t.get("x_target")), goal=f(t.get("goal")),
        exe=f(t.get("executed_lane")), vlat=f(t.get("v_lat")),
        head=f(t.get("heading")), steer=f(t.get("steer_norm")),
        mode=mode, plan_x=f(pl.get("x")), plan_pc=f(pl.get("pc")),
        fd=f(pl.get("fd")), owner=t.get("steer_owner"),
        role=t.get("plan_role"), auth=t.get("authority"),
        ro=t.get("ro_source"), route_ph=rt.get("ph"),
        plan_gate=(t.get("plan_gate") or {}),
    )


def windows(ticks, whys=("center", "center:coarse"), min_len=20):
    out, cur = [], []
    for t in ticks:
        ok = (t.get("state") == "CRUISE" and t.get("sel_layer") == 4
              and t.get("sel_why") in whys)
        if not ok:
            if cur:
                out.append(cur)
            cur = []
            continue
        if cur:
            pv = cur[-1]
            if (t.get("stage_id") != pv.get("stage_id")
                    or t.get("seq") != pv.get("seq") + 1
                    or f(t.get("dt"), 0) > 0.12):
                out.append(cur)
                cur = []
        cur.append(t)
    if cur:
        out.append(cur)
    return [w for w in out if len(w) >= min_len]


def run(session):
    ticks = load(session)
    if not ticks:
        print("missing", session)
        return
    ws = windows(ticks)
    rows = [row(t) for w in ws for t in w]
    print(f"\n=== {session}: 纯居中窗口 {len(ws)} 个 / {len(rows)} 拍")
    print("  模式:", dict(Counter(r["mode"] for r in rows)))
    steps = {"ego": [], "c0": [], "tsel": [], "pub": [], "exe": [],
             "vlat": [], "ref": [], "goal": []}
    trans = Counter()
    jumps = []
    attrib = Counter()
    for a, b in zip(rows, rows[1:]):
        if a["stage"] != b["stage"] or a["seq"] + 1 != b["seq"]:
            continue
        for k in ("ego", "c0", "tsel", "pub", "exe", "vlat", "ref", "goal"):
            if a[k] is not None and b[k] is not None:
                steps[k].append(b[k] - a[k])
        d = None if a["tsel"] is None or b["tsel"] is None \
            else b["tsel"] - a["tsel"]
        cat = None
        if a["ref"] is not None and b["ref"] is not None \
                and abs(b["ref"] - a["ref"]) > 1e-6:
            cat = "路线参考道 ±1 道"
            trans["ref 换道"] += 1
        elif a["mode"] == "in" and b["mode"] == "in":
            cat = "自车估计移动"
            trans["in→in"] += 1
        elif a["mode"] in ("hi", "lo") and a["mode"] == b["mode"]:
            cat = "道路中心估计移动"
            trans[a["mode"] + "→" + b["mode"]] += 1
        elif a["mode"] != b["mode"] and "n/a" not in (a["mode"], b["mode"]):
            cat = "区间模式翻转"
            trans["翻转 " + a["mode"] + "→" + b["mode"]] += 1
        else:
            trans["无法解释"] += 1
        if cat and d is not None:
            attrib[cat] += abs(d)
            jumps.append((abs(d), cat, b["seq"], b["stage"]))
    print("  逐拍|Δ|:", {k: stat(v) for k, v in steps.items() if k != "ref"})
    print("  过渡分类:", dict(trans))
    tot = sum(attrib.values()) or 1.0
    print("  目标位移归属（按 |Δtsel| 求和占比）:",
          {k: f"{v:.1f} ({v/tot*100:.0f}%)" for k, v in attrib.items()})
    jumps.sort(reverse=True)
    print("  最大目标跳变:", [(round(a, 3), c, f"s{st}", sq)
                             for a, c, sq, st in jumps[:6]])
    # c0 来源与交替
    sc = sum(1 for a, b in zip(rows, rows[1:])
             if a["stage"] == b["stage"] and a["seq"] + 1 == b["seq"]
             and a["ro"] != b["ro"])
    n = len(rows) - 1
    def alt(v):
        s = [1 if x > 0 else -1 for x in v if x]
        return sum(1 for a, b in zip(s, s[1:]) if a * b < 0) / max(1, len(s) - 1)
    print(f"  ro_source 换源 {sc}/{n} 拍 ({sc/max(1,n)*100:.0f}%)  构成",
          dict(Counter(r["ro"] for r in rows)))
    print(f"  Δc0 符号交替率={alt(steps['c0']):.2f}  Δego={alt(steps['ego']):.2f}"
          f"  Δpub={alt(steps['pub']):.2f}  v_lat={alt(steps['vlat']):.2f}")
    # 发布滞后 / slew 咬合
    nb = lag_in = 0
    lag_vals = []
    for a, b in zip(rows, rows[1:]):
        if a["stage"] != b["stage"] or a["seq"] + 1 != b["seq"]:
            continue
        if None not in (a["pub"], b["pub"], b["goal"]):
            dp = b["pub"] - a["pub"]
            lim = SLEW * b["dt"]
            if abs(abs(dp) - lim) < 0.15 * lim and abs(b["goal"] - b["pub"]) > 5e-3:
                nb += 1
        if b["mode"] == "in" and b["pub"] is not None and b["ego"] is not None:
            lag_vals.append(b["ego"] - b["pub"])
            if abs(b["ego"] - b["pub"]) > 0.02:
                lag_in += 1
    print(f"  slew 咬合拍={nb}/{n}  区间内拍={sum(1 for r in rows if r['mode']=='in')}"
          f"  其中发布落后>0.02道={lag_in}")
    print("  区间内 (ego−pub) 分布:", stat(lag_vals))
    # 执行侧实际误差（planner 口径）
    errs = []
    for r in rows:
        if None not in (r["pub"], r["exe"]):
            xpred = r["exe"] + (r["vlat"] or 0.0) * TAU
            errs.append(r["pub"] - xpred)
    print("  planner 口径误差 (pub−(exe+v_lat·0.3)):", stat(errs))
    print("  超过死区的拍: 维护0.15道",
          sum(1 for e in errs if abs(e) > 0.15),
          " 机动0.05道", sum(1 for e in errs if abs(e) > 0.05))
    print("  authority:", dict(Counter(r["auth"] for r in rows)),
          " owner:", dict(Counter(r["owner"] for r in rows)))
    return rows, ws


if __name__ == "__main__":
    for s in ("20261118_195555", "20261119_104138", "20261118_225251"):
        run(s)
