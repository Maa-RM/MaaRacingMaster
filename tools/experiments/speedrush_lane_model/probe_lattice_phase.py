"""车道格相位探针：候选「道中心」到底落在分隔线上还是道中心上。

背景
----
RULES §3.8：本玩法固定 4 车道、3 条白色分隔线。所以路的几何中心（走廊
中心）**本身就是一条分隔线**，车道中心在它 ±0.5、±1.5 道。

`coarse.choose_lane` 的候选道是 ``x_rel = k * lane_w_m``——从走廊中心起
算整数道。若走廊中心是分隔线，候选道中心就全落在分隔线上，即恒定的半道
偏差。

本探针不改任何生产代码，只用已落盘的 trace（每拍带车道线 L/k、走廊
road_offset）和 raw 帧复算两件事：

1. **真实车道中心**：相邻两条已接受分隔线（k 差 1）的中点。这是与走廊
   完全独立的见证——线画在路面上，中点必然是道中心。
2. **两套候选格**：旧格 ``-ro + k``、新格 ``-ro + k + 0.5``（自车系，
   道，右正），分别量它们到最近真实道中心的距离。

判据：新格若把「候选道中心 → 真实道中心」的距离从 ~0.35 道压到 ~0.1
道以内，且「保持车道」目标横移量分布合理，则半道偏差成立。

用法::

    .venv/Scripts/python.exe tools/experiments/speedrush_lane_model/probe_lattice_phase.py
    ... --session 20261112_224317 --stage 2 --show 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_APPDATA = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
LOG_ROOT = os.path.join(_APPDATA, "MaaRacingMaster", "logs")
DEBUG_ROOT = os.path.join(_APPDATA, "MaaRacingMaster", "debug")
SESSIONS = ("20261112_224317", "20261112_220717", "20261112_203641")


def _wrap(v: float) -> float:
    """把道差折到 [-0.5, 0.5)。"""
    return (v + 0.5) % 1.0 - 0.5


def load_ticks(session: str) -> list[dict]:
    path = os.path.join(LOG_ROOT, session, "trace", "speedrush.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") is None:
                out.append(d)
    return out


def lane_centers(line_list: list[dict]) -> list[float]:
    """相邻两条分隔线的中点（自车系，道，右正）。"""
    good = []
    for ln in line_list:
        if not ln.get("accepted"):
            continue
        if ln.get("k") is None or ln.get("L") is None:
            continue
        if ln.get("rms") is not None and float(ln["rms"]) > 3.0:
            continue
        good.append((int(ln["k"]), float(ln["L"])))
    out = []
    for i in range(len(good)):
        for j in range(i + 1, len(good)):
            if abs(good[i][0] - good[j][0]) != 1:
                continue
            out.append(0.5 * (good[i][1] + good[j][1]))
    return out


def nearest(vals: list[float], x: float) -> float:
    return min(vals, key=lambda c: abs(c - x))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--stage", type=int, default=None)
    ap.add_argument("--show", type=int, default=8, help="打印逐拍样例条数")
    args = ap.parse_args()

    sessions = [args.session] if args.session else list(SESSIONS)
    for ses in sessions:
        ticks = load_ticks(ses)
        if not ticks:
            print(f"{ses}: 无 trace，跳过")
            continue
        for st in sorted({t["stage_id"] for t in ticks}):
            if args.stage is not None and st != args.stage:
                continue
            old_d, new_d, ph_d, keep_shift, phase_shift = [], [], [], [], []
            old_on_div = 0
            n_use = 0
            samples = []
            for t in ticks:
                if t["stage_id"] != st:
                    continue
                lo = t.get("lane_obs") or {}
                ro = t.get("ro_off")
                if lo.get("x") is None or ro is None:
                    continue
                cents = lane_centers(lo.get("lines") or [])
                if not cents:
                    continue
                n_use += 1
                # 旧格：候选 k 在自车系的位置 = -ro + k（k 取离自车最近的几个）
                k0 = round(ro)
                old_cands = [-ro + k for k in range(k0 - 2, k0 + 3)]
                # B：固定半道
                fix_cands = [c + 0.5 for c in old_cands]
                # C：按本拍相位（走廊中心在线网格里的位置 c = x − ro）折算
                c = float(lo["x"]) - float(ro)
                shift = round(c - 0.5) + 0.5 - c
                phase_cands = [c2 + shift for c2 in old_cands]
                d_old = min(abs(nearest(cents, c2) - c2) for c2 in old_cands)
                d_new = min(abs(nearest(cents, c2) - c2) for c2 in fix_cands)
                d_ph = min(abs(nearest(cents, c2) - c2) for c2 in phase_cands)
                old_d.append(d_old)
                new_d.append(d_new)
                ph_d.append(d_ph)
                if d_old < 0.15:
                    old_on_div += 1
                # 保持车道目标：三套格各自离自车（0）最近的候选中心
                t_old = min(old_cands, key=abs)
                t_new = min(fix_cands, key=abs)
                t_ph = min(phase_cands, key=abs)
                keep_shift.append(t_new - t_old)
                phase_shift.append(t_ph - t_old)
                if len(samples) < args.show:
                    samples.append((t["fid"], ro, cents, t_old, t_new, t_ph,
                                    d_old, d_new, d_ph))
            if not n_use:
                print(f"{ses} stage{st}: 无可用拍（线点不足）")
                continue
            import numpy as np
            o = np.array(old_d)
            n = np.array(new_d)
            ph = np.array(ph_d)
            s = np.array(keep_shift)
            sp = np.array(phase_shift)
            print(f"\n=== {ses} stage{st}  n={n_use}")
            print(f"  候选道中心→最近真实道中心（道）："
                  f"旧格 p50={np.median(o):.2f} p90={np.percentile(o, 90):.2f} | "
                  f"固定+0.5 p50={np.median(n):.2f} p90={np.percentile(n, 90):.2f} | "
                  f"按相位 p50={np.median(ph):.2f} p90={np.percentile(ph, 90):.2f}")
            print(f"  落在 0.15 道内占比：旧格 {float(np.mean(o < 0.15)):.0%}"
                  f"  固定+0.5 {float(np.mean(n < 0.15)):.0%}"
                  f"  按相位 {float(np.mean(ph < 0.15)):.0%}")
            print(f"  保持车道目标横移（相对旧格）：固定+0.5 p50={np.median(s):+.2f} "
                  f"|shift|>0.3 占 {float(np.mean(np.abs(s) > 0.3)):.0%} | "
                  f"按相位 p50={np.median(sp):+.2f} p5={np.percentile(sp, 5):+.2f} "
                  f"p95={np.percentile(sp, 95):+.2f}")
            for (fid, ro, cents, t_old, t_new, t_ph,
                 d_old, d_new, d_ph) in samples[: args.show]:
                cs = ",".join(f"{c:+.2f}" for c in sorted(cents))
                print(f"    fid{fid} ro={ro:+.2f} 真道中心[{cs}] "
                      f"旧={t_old:+.2f}({d_old:.2f}) 固定={t_new:+.2f}({d_new:.2f}) "
                      f"相位={t_ph:+.2f}({d_ph:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
