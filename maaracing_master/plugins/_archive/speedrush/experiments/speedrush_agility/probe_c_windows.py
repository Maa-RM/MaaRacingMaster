"""C 线：反向窗ロ的逐拍归因表（读 probe_c_control_ownership 的 dump）。

对每个「杆值反向」事件给出：路径、参考量、状态时刻、整形前后杆值，
以及同一条朝向律内部的两项分解——
    u = k_psi·(ψ_des − ψ) − k_psi·lead·r − k_r·r
         └── 参考/位置项 A ──┘   └──── 阻尼/前瞻项 B ────┘
用于区分「参考翻面」「阻尼反打」与「路径切换」。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parents[6]))
from maaracing_master.plugins._archive.speedrush.config import load_decision  # noqa: E402


def terms(tick, p):
    st = tick["stick"]
    if not st:
        return None
    psi_des = st["psi_des"]
    psi = st["psi"]
    r = st["r"] or 0.0
    lim = p.heading_hold_rad if st["gentle"] else p.heading_max_rad
    des = max(-lim, min(lim, psi_des))
    a = p.k_psi * (des - psi)
    b = -p.k_psi * st["lead"] * r - p.k_r * r
    return {"psi_des": st["psi_des"], "psi": psi, "r": r,
            "A_ref": round(a, 4), "B_damp": round(b, 4),
            "u_raw": st["u_raw"], "u_lim": st["u"], "lim": st["lim"],
            "dom": "A" if abs(a) > abs(b) else "B"}


def find_reversals(ticks, thresh=0.25):
    revs = []
    prev = None
    for i, t in enumerate(ticks):
        v = t["emit"] / 32767.0
        if abs(v) <= thresh:
            continue
        if prev is not None and v * ticks[prev]["emit"] < 0:
            revs.append((prev, i))
        prev = i
    return revs


def row(tick, p):
    t = terms(tick, p)
    plan = tick["plan"] or {}
    xt = tick["xt"]
    x = tick["x"]
    return {
        "t": tick["t"], "seq": tick["seq"], "path": tick["path"],
        "why": tick["why"], "phase": tick["phase"],
        "chain": [c[0] for c in tick["chain"]],
        "vals": [c[1] for c in tick["chain"]],
        "pre_lpf": tick["raw"], "final_raw": tick.get("final_raw"),
        "norm": tick["norm"], "emit": tick["emit"],
        "x": x, "xt": xt,
        "err_pos": None if xt is None else round(xt - x, 3),
        "err_lead": None if xt is None else
        round(xt - (x + tick["v_lat"] * p.lookahead_tau_s), 3),
        "vlat": tick["v_lat"], "psi": tick["psi"], "r": tick["rr"],
        "plan_pc": plan.get("pc"), "plan_layer": plan.get("layer"),
        "plan_chosen": plan.get("chosen"), "plan_valid": plan.get("valid"),
        "preview": tick["preview"], "osc_a": tick["osc_a"],
        "branch": tick["branch"],
        "terms": t,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", nargs="+", type=Path, required=True)
    ap.add_argument("--gap", type=float, default=1.5)
    ap.add_argument("--rev-thres", type=float, default=0.25,
                    help="显著拍阈值：|steer_x/32767| 超过它才算显著拍")
    ap.add_argument("--center-only", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    p = load_decision().planner
    for path in args.dump:
        ticks = json.loads(path.read_text(encoding="utf-8"))
        revs = find_reversals(ticks, args.rev_thres)
        windows = []
        for pair in revs:
            if windows and ticks[pair[1]]["t"] - ticks[windows[-1][-1][1]]["t"] \
                    <= args.gap:
                windows[-1].append(pair)
            else:
                windows.append([pair])
        print(f"# {path.name} ticks={len(ticks)} reversals={len(revs)} "
              f"windows={len(windows)}")
        agg = Counter()
        for wi, w in enumerate(windows, 1):
            a0, b0 = w[0][0], w[-1][1]
            span = ticks[a0:b0 + 1]
            center = all((s["why"] or "").startswith("center")
                         for s in span)
            homo = [len({s["path"] for s in ticks[a:b + 1]}) == 1
                    for a, b in w]
            agg["same_path_rev"] += sum(homo)
            agg["switch_rev"] += len(homo) - sum(homo)
            agg["center_window"] += int(center)
            if args.center_only and not center:
                continue
            rec = {
                "window": wi, "t": [span[0]["t"], span[-1]["t"]],
                "revs": len(w), "center_only": center,
                "paths": dict(Counter(s["path"] for s in span)),
                "sub": [],
            }
            for a, b in w:
                ra, rb = row(ticks[a], p), row(ticks[b], p)
                flip = {
                    "same_path": len({s["path"] for s in ticks[a:b + 1]}) == 1,
                    "err_pos_sign_flip": (ra["err_pos"] is not None
                                          and rb["err_pos"] is not None
                                          and ra["err_pos"] * rb["err_pos"] < 0),
                    "pc_sign_flip": (ra["plan_pc"] is not None
                                     and rb["plan_pc"] is not None
                                     and ra["plan_pc"] * rb["plan_pc"] < 0),
                    "chosen_changed": ra["plan_chosen"] != rb["plan_chosen"],
                    "preview_on_at_b": rb["preview"],
                    "dom_a": None if not ra["terms"] else ra["terms"]["dom"],
                    "dom_b": None if not rb["terms"] else rb["terms"]["dom"],
                    "crossing_tick": None if b - a <= 1 else
                    [ticks[k]["t"] for k in range(a + 1, b)],
                }
                rec["sub"].append({"a": ra, "b": rb, "flip": flip})
            print(json.dumps(rec, ensure_ascii=False))
        if not args.quiet:
            print(json.dumps({"agg": dict(agg)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
