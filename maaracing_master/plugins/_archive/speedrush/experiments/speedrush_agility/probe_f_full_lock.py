"""满杆事件的横向后果：连续转向还是离散换道（speedrush，离线）。

**问什么**：RULES §1 记载「|杆|>0.99 且对应技能生效时，约 0.2~0.5 秒切到相邻
车道」。本探针只在实录里定位满杆事件、量事件之后的横向后果，不评价控制律、
不做释放策略、不核对自动回正。

口径：

- **满杆**：``|steer_x| / 32767 > 0.99``。程序局的 ``steer_x`` 是下发摇杆量；
  人工局另有手柄流 ``pads.jsonl`` 的 ``lx``，两者是否同一条输入通道需调用方判断
  （本探针分开报，不合并）。
- **可信横位**：``lane_obs.x`` 的可用拍（``usable`` 且 ``state=TRACK``）。
  ``x`` 与 ``executed_lane`` 都会在重锚时整道跳变，只有可用拍和「整组线是否
  真的平移了一道」能区分「车动了」与「锚点换了人」。
- **整组线平移**：相邻两拍把上一拍的每条线 ``lines[].L``（道）配到本拍最近的
  线，取配对位移集合的中位。车真的横移一道，整组线必须在一拍内平移约一道；
  重锚只改道号、不改线在画面里的位置，整组线位移仍接近 0。
- 1 道 = 3.5 m；``v_lat`` 单位是道/秒（``v·tanψ/LANE_W``，ψ 截幅后的导出值）。

用法::

    .venv/Scripts/python.exe -X utf8 probe_f_full_lock.py [会话 ...]

不给会话时跑默认三局。
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
DEBUG = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/debug/speedrush")
LANE_W = 3.5
FULL = 0.99
WIN_AFTER_S = 0.6
TRUST_RATE_DT_S = 0.2      # 可信横位差分的最大间隔


def load(sess: str):
    p = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    ticks = [json.loads(line) for line in open(p, encoding="utf-8")]
    ticks = [r for r in ticks if "steer_x" in r]
    ticks.sort(key=lambda r: r["ts_ns"])
    return ticks


def frame(r, t):
    lo = r.get("lane_obs") or {}
    return {
        "t": t,
        "u": r["steer_x"] / 32767.0,
        "x": lo.get("x"),
        "usable": bool(lo.get("usable")),
        "state": lo.get("state"),
        "lines": [l["L"] for l in (lo.get("lines") or [])
                  if l.get("L") is not None],
        "exl": r.get("executed_lane"),
        "vlat": r.get("v_lat") or 0.0,
        "psi": (r.get("lat_ctrl") or {}).get("psi"),
        "owner": r.get("steer_owner"),
        "role": r.get("plan_role"),
        "intent": r.get("intent_lane"),
        "vego": r.get("v_ego") or 0.0,
    }


def build(ticks):
    t0 = ticks[0]["ts_ns"]
    return [frame(r, (r["ts_ns"] - t0) / 1e9) for r in ticks]


def trusted(f):
    return f["x"] is not None and f["usable"] and f["state"] == "TRACK"


def line_shift(prev, cur):
    """整组线在相邻两拍的中位位移（道）；配对不上返回 None。"""
    if not prev["lines"] or not cur["lines"]:
        return None
    used, ds = set(), []
    for v in prev["lines"]:
        best = None
        for j, w in enumerate(cur["lines"]):
            if j in used:
                continue
            if best is None or abs(w - v) < abs(cur["lines"][best] - v):
                best = j
        if best is not None and abs(cur["lines"][best] - v) < 0.30:
            used.add(best)
            ds.append(cur["lines"][best] - v)
    return st.median(ds) if len(ds) >= 2 else None


def runs_of(s, flags):
    runs, cur = [], []
    for i, f in enumerate(s):
        if flags[i]:
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def nearest_trusted(s, t, lo, hi):
    cand = [f for f in s if lo <= f["t"] <= hi and trusted(f)]
    if not cand:
        return None
    return min(cand, key=lambda f: abs(f["t"] - t))


def event_metrics(s, i0, i1):
    """满杆段 [i0, i1] 之后 WIN_AFTER_S 内的横向后果。"""
    t_start = s[i0]["t"]
    t_end = s[i1 + 1]["t"] if i1 + 1 < len(s) else s[i1]["t"]
    win = [f for f in s[i0:] if f["t"] <= t_start + WIN_AFTER_S]
    # 只认事件前后紧邻的可信拍：隔得远的 x 会跨过重锚，读数不可比
    before = [f for f in s if t_start - 0.25 <= f["t"] <= t_start
              and trusted(f)]
    after = [f for f in win if f["t"] >= t_start and trusted(f)]
    x0 = before[-1] if before else None
    x1 = after[-1] if after else None
    shifts = []
    for a, b in zip(s[max(0, i0 - 1):], s[max(0, i0):]):
        m = line_shift(a, b)
        if m is not None and a["t"] >= t_start - 0.05 \
                and b["t"] <= t_start + WIN_AFTER_S:
            shifts.append((abs(m), b["t"], m))
    return {
        "t": t_start,
        "dur": t_end - t_start,
        "n": i1 - i0 + 1,
        "u0": s[i0]["u"],
        "vlat0": s[i0]["vlat"],
        "vlat_end": win[-1]["vlat"],
        "psi0": s[i0]["psi"],
        "psi_end": win[-1]["psi"],
        "role": s[i0]["role"],
        "intent": s[i0]["intent"],
        "x0": None if x0 is None else x0["x"],
        "x1": None if x1 is None else x1["x"],
        "dx": (None if x0 is None or x1 is None
               else x1["x"] - x0["x"]),
        "crossed": (None if x0 is None or x1 is None
                    else int(x0["x"] + 0.5) != int(x1["x"] + 0.5)),
        "max_line_shift": max((v for v, _, _ in shifts), default=None),
        "straddle": min((abs(v) for f in win for v in f["lines"]),
                        default=None),
        "rate": None if x0 is None or x1 is None or x1["t"] == x0["t"]
        else (x1["x"] - x0["x"]) / (x1["t"] - x0["t"]),
    }


def trace_stats(s):
    """全jú：可信横位横速、整组线单拍跳变、满杆拍占比。"""
    rates, shifts = [], []
    for a, b in zip(s, s[1:]):
        dt = b["t"] - a["t"]
        if 0 < dt <= TRUST_RATE_DT_S and trusted(a) and trusted(b):
            rates.append((b["x"] - a["x"]) / dt)
        if abs(b["u"]) > FULL or abs(a["u"]) > FULL:
            m = line_shift(a, b)
            if m is not None:
                shifts.append(abs(m))
    ar = sorted(abs(v) for v in rates)
    return {
        "rate_n": len(rates),
        "rate_p50": st.median(ar) if ar else None,
        "rate_p90": ar[int(len(ar) * 0.9)] if ar else None,
        "rate_p99": ar[int(len(ar) * 0.99)] if ar else None,
        "rate_max": ar[-1] if ar else None,
        "full_line_shift_max": max(shifts) if shifts else None,
        "full_line_shift_n": len(shifts),
    }, rates, shifts


def pad_runs(ticks, s):
    """人工局：手柄侧的满杆段。只取与 trace 时间范围重叠的 pads 文件。

    手柄的 ``lx`` 是玩家输入，和 trace 的 ``steer_x``（程序自己的量）不是一回事，
    两者是否同一条输入通道由调用方判断，这里只报段与车侧响应。
    """
    out = []
    if not os.path.isdir(DEBUG):
        return out
    t_lo, t_hi = ticks[0]["ts_ns"], ticks[-1]["ts_ns"]
    for name in sorted(os.listdir(DEBUG)):
        pp = os.path.join(DEBUG, name, "pads.jsonl")
        if "manual" not in name or not os.path.exists(pp):
            continue
        pads = [json.loads(line) for line in open(pp, encoding="utf-8")]
        pads.sort(key=lambda d: d["ts_ns"])
        if pads[-1]["ts_ns"] < t_lo or pads[0]["ts_ns"] > t_hi:
            continue
        for run in runs_of(pads, [abs(d["lx"]) > FULL * 32767 for d in pads]):
            j = min(run[-1] + 1, len(pads) - 1)
            a = pads[run[0]]
            t = (a["ts_ns"] - t_lo) / 1e9
            dur = (pads[j]["ts_ns"] - a["ts_ns"]) / 1e9
            i0 = next((k for k, f in enumerate(s) if f["t"] >= t), len(s) - 1)
            i1 = next((k for k, f in enumerate(s) if f["t"] >= t + dur),
                      len(s) - 1)
            m = event_metrics(s, i0, i1)
            out.append({"file": name, "lx": a["lx"], "n": len(run),
                        "dur": dur, "t": t, "dx": m["dx"],
                        "crossed": m["crossed"]})
    return out


def main(argv):
    sessions = argv[1:] or ["20261119_104138", "20261118_195555",
                            "20261118_225251"]
    report = {}
    for sess in sessions:
        s = build(load(sess))
        flags = [abs(f["u"]) > FULL for f in s]
        runs = runs_of(s, flags)
        glob, rates, shifts = trace_stats(s)
        print(f"\n== {sess}  {s[-1]['t']:.1f}s  {len(s)} 拍  "
              f"满杆 {sum(flags)} 拍 / {len(runs)} 段")
        print(f"   可信横位横速 道/s：p50 {fmt(glob['rate_p50'])} "
              f"p90 {fmt(glob['rate_p90'])} p99 {fmt(glob['rate_p99'])} "
              f"max {fmt(glob['rate_max'])}（×{LANE_W} = m/s）")
        print(f"   满杆相邻拍的整组线位移：n={glob['full_line_shift_n']} "
              f"max={fmt(glob['full_line_shift_max'], 3)} 道")
        evs = []
        for run in runs:
            m = event_metrics(s, run[0], run[-1])
            evs.append(m)
            print(f"   t={m['t']:7.3f} u={m['u0']:+.3f} n={m['n']:2d} "
                  f"dur={m['dur']:.3f}s role={m['role']} intent={m['intent']} "
                  f"vlat {m['vlat0']:+.2f}->{m['vlat_end']:+.2f} "
                  f"x {fmt(m['x0'])}->{fmt(m['x1'])} Δx={fmt(m['dx'])} "
                  f"cross={m['crossed']} lineShift={fmt(m['max_line_shift'], 3)} "
                  f"straddle={fmt(m['straddle'])}")
        durs = sorted(e["dur"] for e in evs)
        slide = sum(1 for e in evs if abs(e["vlat0"]) >= 0.8)
        dxs = [e["dx"] for e in evs if e["dx"] is not None]
        rate = [abs(e["rate"]) for e in evs if e["rate"] is not None]
        summ = {
            "runs": len(evs),
            "frames": sum(flags),
            "dur_min": durs[0] if durs else None,
            "dur_med": st.median(durs) if durs else None,
            "dur_max": durs[-1] if durs else None,
            "vlat_slide": slide,
            "dx_n": len(dxs),
            "dx_min": min(dxs) if dxs else None,
            "dx_max": max(dxs) if dxs else None,
            "cross": sum(1 for e in evs if e["crossed"]),
            "rate_med": st.median(rate) if rate else None,
            "rate_max": max(rate) if rate else None,
        }
        print(f"   汇总：{summ['runs']} 段 / {summ['frames']} 拍；"
              f"时长 min {fmt(summ['dur_min'], 3)} 中位 {fmt(summ['dur_med'], 3)} "
              f"max {fmt(summ['dur_max'], 3)} s；起手 |v_lat|≥0.8 的 {slide} 段；"
              f"有 Δx 的 {summ['dx_n']} 段，"
              f"Δx∈[{fmt(summ['dx_min'])}, {fmt(summ['dx_max'])}] 道，"
              f"跨整道 {summ['cross']} 段；窗内横速中位 {fmt(summ['rate_med'])} "
              f"max {fmt(summ['rate_max'])} 道/s")
        report[sess] = {"global": glob, "summary": summ, "events": evs}
        pads = pad_runs(load(sess), s)
        if pads:
            pdur = sorted(p["dur"] for p in pads)
            dxs = [p["dx"] for p in pads if p["dx"] is not None]
            print(f"   手柄侧满杆 {len(pads)} 段：时长 min {fmt(pdur[0], 3)} "
                  f"中位 {fmt(st.median(pdur), 3)} max {fmt(pdur[-1], 3)} s；"
                  f"有 Δx 的 {len(dxs)} 段，Δx∈[{fmt(min(dxs)) if dxs else 'None'}, "
                  f"{fmt(max(dxs)) if dxs else 'None'}] 道；"
                  f"跨整道 {sum(1 for p in pads if p['crossed'])} 段")
            report[sess]["pads"] = pads
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out",
                       "f_full_lock.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(report, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n落盘 {out}")
    return 0


def fmt(v, nd=2):
    return "None" if v is None else f"{v:+.{nd}f}"


if __name__ == "__main__":
    sys.exit(main(sys.argv))
