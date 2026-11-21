# -*- coding: utf-8 -*-
"""P3-A A 线探针（第二版）：纯居中窗口目标链的分项归因。

与 probe_a_chain.py 的区别：本版把选择目标按「模式」拆开
（区间内=自车 / 夹上界 / 夹下界），并分别统计
  自车估计步变、道路中心步变、粗层换道、发布 slew、执行误差符号翻转。

用法：
    .venv\\Scripts\\python.exe maaracing_master/plugins/_archive/speedrush/experiments/speedrush_agility/probe_a_attrib.py
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

HALF = 0.17          # (0.5 − 0.27 − 0.06) 道
LW = 3.5
SLEW = 2.0           # 道/s
MERGE_SLEW = 4.0


def load(session):
    p = os.path.join(os.path.expandvars(r"%APPDATA%\MaaRacingMaster\logs"),
                     session, "trace", "speedrush.jsonl")
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") is None and d.get("sel_dbg"):
                out.append(d)
    return out


def f(x, d=None):
    return x if isinstance(x, (int, float)) else d


def med(v):
    v = sorted(v)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def pct(v, q):
    v = sorted(v)
    if not v:
        return None
    i = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[i]


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


def row(t):
    sd = t.get("sel_dbg") or {}
    c = t.get("coarse") or {}
    ego = f(sd.get("ego"))
    c0 = f(sd.get("c0"))
    cl = f(c.get("lane"))
    tsel = f(sd.get("target"))
    model = None
    mode = "n/a"
    if ego is not None and c0 is not None:
        base = c0 + (cl or 0.0)
        if tsel is not None and abs(tsel - ego) < 2e-3:
            mode = "in"
            model = ego
        elif tsel is not None and abs(tsel - (base + HALF)) < 0.03:
            mode = "hi"
            model = base + HALF
        elif tsel is not None and abs(tsel - (base - HALF)) < 0.03:
            mode = "lo"
            model = base - HALF
    return dict(
        seq=t.get("seq"), stage=t.get("stage_id"), fid=t.get("fid"),
        why=t.get("sel_why"), dt=f(t.get("dt"), 0.05),
        ego=ego, c0=c0, cl=cl, tsel=tsel, pub=f(t.get("x_target")),
        goal=f(t.get("goal")), exe=f(t.get("executed_lane")),
        vlat=f(t.get("v_lat")), head=f(t.get("heading")),
        steer=f(t.get("steer_norm")), mode=mode, model=model,
        plan_x=f((t.get("plan") or {}).get("x")),
        plan_pc=f((t.get("plan") or {}).get("pc")),
        fd=f((t.get("plan") or {}).get("fd")),
        plan_role=t.get("plan_role"), owner=t.get("steer_owner"),
        ro=t.get("ro_source"), roc=t.get("ro_center_src"),
        ro_reject=t.get("ro_reject"), auth=t.get("authority"),
        left=f(sd.get("left")), right=f(sd.get("right")),
    )


def analyze(session):
    ticks = load(session)
    ws = windows(ticks)
    print(f"\n=== session {session}: ticks={len(ticks)} 纯居中窗口={len(ws)}"
          f" 共{sum(len(w) for w in ws)}拍")
    allr = []
    for w in ws:
        rs = [row(t) for t in w]
        allr.extend(rs)
    # --- 目标模型自检：target 能否用 ego/c0/coarse 解释
    modes = Counter(r["mode"] for r in allr)
    print("模式构成:", dict(modes))
    bad = [r for r in allr if r["mode"] == "n/a"]
    print(f"无法解释拍={len(bad)}")
    # --- 逐拍步变
    def steps(key):
        return [b[key] - a[key] for a, b in zip(allr, allr[1:])
                if a[key] is not None and b[key] is not None
                and a["stage"] == b["stage"] and a["seq"] + 1 == b["seq"]]
    d_ego, d_c0, d_tsel, d_pub, d_exe, d_vl = (steps("ego"), steps("c0"),
                                               steps("tsel"), steps("pub"),
                                               steps("exe"), steps("vlat"))
    for nm, v in (("Δego", d_ego), ("Δc0", d_c0), ("Δtsel", d_tsel),
                  ("Δpub", d_pub), ("Δexe", d_exe),
                  ("v_lat*dt", [x * 0.05 for x in d_vl])):
        a = [abs(x) for x in v]
        print(f"  |{nm}| 中位={med(a):.4f} p90={pct(a,0.9):.4f} 最大={max(a):.4f}"
              f" n={len(a)}")
    # --- c0 来源与抖动
    src_changes = sum(1 for a, b in zip(allr, allr[1:])
                      if a["stage"] == b["stage"] and a["seq"] + 1 == b["seq"]
                      and a["ro"] != b["ro"])
    print("ro_source 构成:", dict(Counter(r["ro"] for r in allr)),
          " 相邻拍换源次数:", src_changes)
    sat = sum(1 for x in d_c0 if abs(x) >= 0.28)
    print(f"|Δc0|≥0.28(接近 0.3 限速) 的拍={sat}/{len(d_c0)}"
          f"  |Δc0|≥0.1 的拍={sum(1 for x in d_c0 if abs(x)>=0.1)}")
    # 符号交替率（10 Hz 抖动的直接度量）
    def alt_rate(v):
        s = [1 if x > 0 else (-1 if x < 0 else 0) for x in v if x != 0]
        return sum(1 for a, b in zip(s, s[1:]) if a * b < 0) / max(1, len(s) - 1)
    print(f"Δc0 符号交替率={alt_rate(d_c0):.2f}  Δego 交替率={alt_rate(d_ego):.2f}"
          f"  Δpub 交替率={alt_rate(d_pub):.2f}  v_lat 交替率={alt_rate(d_vl):.2f}")
    # --- 模式翻转与粗层换道
    flips = 0
    cl_changes = 0
    jump = []
    for a, b in zip(allr, allr[1:]):
        if a["stage"] != b["stage"] or a["seq"] + 1 != b["seq"]:
            continue
        if a["mode"] != b["mode"]:
            flips += 1
            if a["tsel"] is not None and b["tsel"] is not None:
                jump.append(abs(b["tsel"] - a["tsel"]))
        if a["cl"] is not None and b["cl"] is not None \
                and abs(b["cl"] - a["cl"]) > 1e-6:
            cl_changes += 1
    print(f"模式翻转={flips}  翻转时|Δtsel|中位={med(jump):.3f} 最大={max(jump):.3f}"
          f"  粗层换道拍={cl_changes}")
    # --- 发布 slew 与滞后
    nb = 0
    lags = []
    signflip_pub_ego = 0
    signflip_pub_exe = 0
    n_cmp = 0
    prev_e = prev_x = None
    for a, b in zip(allr, allr[1:]):
        if a["stage"] != b["stage"] or a["seq"] + 1 != b["seq"]:
            continue
        if a["pub"] is not None and b["pub"] is not None \
                and b["goal"] is not None:
            dp = b["pub"] - a["pub"]
            lim = SLEW * b["dt"]
            if abs(abs(dp) - lim) < 0.15 * lim and abs(b["goal"] - b["pub"]) > 5e-3:
                nb += 1
            lags.append(abs(b["goal"] - b["pub"]))
        if b["pub"] is not None and b["ego"] is not None:
            e = b["pub"] - b["ego"]
            if prev_e is not None and e * prev_e < 0:
                signflip_pub_ego += 1
            prev_e = e
            n_cmp += 1
        if b["pub"] is not None and b["exe"] is not None:
            e = b["pub"] - b["exe"]
            if prev_x is not None and e * prev_x < 0:
                signflip_pub_exe += 1
            prev_x = e
    print(f"slew 咬合拍={nb}  发布−目标 |lag| 中位={med(lags):.3f}"
          f" p90={pct(lags,0.9):.3f} 最大={max(lags):.3f}")
    print(f"(pub−ego) 符号翻转={signflip_pub_ego}/{n_cmp}拍"
          f"  ({signflip_pub_ego/max(1,n_cmp)*10:.2f} 次/s @20Hz→×0.5?)")
    print(f"(pub−exe) 符号翻转={signflip_pub_exe}/{n_cmp}拍")
    # authority 构成（COMMIT=机动权限，GENTLE=维护权限）
    print("authority:", dict(Counter(r["auth"] for r in allr)))
    return allr


if __name__ == "__main__":
    for s in ("20261118_195555", "20261119_104138", "20261118_225251"):
        analyze(s)
