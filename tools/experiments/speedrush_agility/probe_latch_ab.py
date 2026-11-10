"""贴边意图连续性 A/B（speedrush，离线）：保持闩开/关两批对账。

四项验收（对应 plan「起意拍提前」真机 A/B）：
a. pass/graze 选中段长度与占空比（闩的目标：段长从 2 拍明显变长）；
b. 游戏 120 分事件判定前 1.2s 是否仍 pass/graze（事件按游戏日志墙钟
   对到 trace 拍，±0.3s 容差）；
c. 判定拍自车距最近安全车横距是否落 [0.54, 0.77] 道（近似：事件时刻
   最近车系被超车的假设，远场车系污染时读数偏大）；
d. 横向运动是否仍与人工同档（v_lat 过零 31~33/分、转向换向 ~63/分、
   均值杆 ~0.15），以及撞车代理（分速跌落/血量下降，若 trace 有 HUD）。

用法::

    .venv/Scripts/python.exe -X utf8 probe_latch_ab.py \
        --trace <trace.jsonl> --label latch [--game-log <g112.log>] ...
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import statistics


def _median(xs):
    xs = sorted(xs)
    return statistics.median(xs) if xs else float("nan")


def _load(path, head, tail):
    stages = []
    cur = None
    ticks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ty = rec.get("type")
            if ty == "stage_begin":
                cur = [rec["ts_mono_ns"], None, rec.get("stage_id")]
                stages.append(cur)
            elif ty == "stage_end" and cur is not None:
                cur[1] = rec["ts_mono_ns"]
                cur = None
            elif rec.get("fid") is not None:
                ticks.append(rec)
    keep = []
    for rec in ticks:
        m = rec.get("ts_mono_ns")
        if m is None:
            continue
        for st in stages:
            if st[1] is None:
                continue
            if (m - st[0]) / 1e9 >= head and (st[1] - m) / 1e9 >= tail:
                rec["_stage"] = st[2]
                keep.append(rec)
                break
    return keep, stages


def _runs(ticks, pred):
    """连续段长度（秒），按 trace 内连续拍（断档 >0.15s 切段）。"""
    out = []
    start = None
    prev_m = None
    for rec in ticks:
        m = rec["ts_mono_ns"] / 1e9
        hit = pred(rec)
        if hit and start is None:
            start = m
        if not hit and start is not None:
            out.append(prev_m - start + 0.05)
            start = None
        if hit and prev_m is not None and m - prev_m > 0.15 and start is not None:
            out.append(prev_m - start + 0.05)
            start = m
        if hit:
            prev_m = m
    if start is not None:
        out.append(prev_m - start + 0.05)
    return out


def _motion(ticks):
    """口径（2026-11-10 定）：符号变化、零值桥接（a*b<0），不掐幅度——
    零点噪声两侧都计，跨会话同口径即可比；v_lat 用规划器状态估计。
    另报「真打舵」（两侧 |steer|≥0.05）作噪声对照。"""
    n = len(ticks)
    if n < 10:
        return {}
    dur = (ticks[-1]["ts_mono_ns"] - ticks[0]["ts_mono_ns"]) / 1e9
    vlat = [r.get("v_lat") or 0.0 for r in ticks]
    steer = [r.get("steer_norm") or 0.0 for r in ticks]
    zc = sum(1 for a, b in zip(vlat, vlat[1:]) if a * b < 0)
    rev = sum(1 for a, b in zip(steer, steer[1:]) if a * b < 0)
    rev_hi = sum(1 for a, b in zip(steer, steer[1:])
                 if a * b < 0 and abs(a) >= 0.05 and abs(b) >= 0.05)
    full = sum(1 for s in steer if abs(s) >= 0.9)
    return {
        "dur_s": round(dur, 1),
        "vlat_cross_per_min": round(zc / dur * 60, 1),
        "steer_rev_per_min": round(rev / dur * 60, 1),
        "steer_rev_hi_per_min": round(rev_hi / dur * 60, 1),
        "mean_abs_steer": round(sum(abs(s) for s in steer) / n, 3),
        "full_stick_pct": round(full / n * 100, 1),
    }


def _crashes(ticks):
    """撞车代理：hud_rate 从 ≥60 跌到 ≤35（分速短窗，减速即里程档跌），
    与 hud_score 掉分 >30（扣分事件）。两次事件间隔 <1.5s 并一次。"""
    drops = []
    last_t = None
    prev = None
    for r in ticks:
        rate = r.get("hud_rate")
        t = r["ts_mono_ns"] / 1e9
        if isinstance(rate, (int, float)):
            if prev is not None and prev >= 60 and rate <= 35:
                if last_t is None or t - last_t > 1.5:
                    drops.append(t)
                    last_t = t
            prev = rate
    score_drops = 0
    ps = None
    for r in ticks:
        s = r.get("hud_score")
        if isinstance(s, (int, float)):
            if ps is not None and s < ps - 30:
                score_drops += 1
            ps = s
    return drops, score_drops


def _game_events(path):
    """游戏日志：120 分事件与回合统计（墙钟秒）。"""
    events = []
    rounds = []
    pat_ts = re.compile(r"\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2}):(\d{3})\]")
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = pat_ts.search(line)
            if not m:
                continue
            y, mo, da, h, mi, s, ms = map(int, m.groups())
            t = dt.datetime(y, mo, da, h, mi, s, ms * 1000).timestamp()
            if "dispatchPlayerCarScore" in line and "score=120" in line:
                ty = re.search(r"type=(\w+)", line)
                events.append((t, ty.group(1) if ty else "?"))
            elif "Round stat flush" in line:
                kv = dict(re.findall(r"(\w+)=(\d+)", line))
                rounds.append((t, kv))
            elif "Result:" in line and "Reason:" in line:
                rounds.append((t, {"result": line.split("Reason:")[-1].strip()}))
    return events, rounds


def _wall(rec):
    return rec["ts_wall_ns"] / 1e9


def _events_near(ticks, t, tol=0.3):
    """事件时刻 ±tol 内有 trace 拍才返回（阶段外/头尾被剔的事件不算）。"""
    idx = min(range(len(ticks)), key=lambda i: abs(_wall(ticks[i]) - t))
    return idx if abs(_wall(ticks[idx]) - t) <= tol else None


def analyze_trace(path, label, head, tail, events, rounds):
    ticks, stages = _load(path, head, tail)
    if not ticks:
        print(f"== {label}: 无拍")
        return
    n = len(ticks)
    pas = [r for r in ticks if r.get("sel_why") in ("pass", "graze")]
    runs = _runs(ticks, lambda r: r.get("sel_why") in ("pass", "graze"))
    print(f"== {label}  拍 {n}（{len(stages)} 阶段，掐头 {head}s 去尾 {tail}s）")
    print(f"   pass/graze 占空比 {len(pas) / n:.1%}；段数 {len(runs)}；"
          f"段长 P50 {_median(runs):.2f}s P90 "
          f"{sorted(runs)[int(len(runs) * 0.9)] if runs else float('nan'):.2f}s；"
          f"≤0.15s 段占 {sum(1 for r in runs if r <= 0.15) / max(len(runs), 1):.0%}")
    mo = _motion(ticks)
    print(f"   运动: {mo}")
    drops, score_drops = _crashes(ticks)
    dur = mo.get("dur_s", 1)
    print(f"   撞车代理：分速跌落 {len(drops)} 次（{len(drops) / dur * 60:.1f}/分），"
          f"hud 掉分 {score_drops} 次")
    # 闩账
    armed = [r for r in ticks
             if ((r.get("sel_dbg") or {}).get("graze_hold") or [None])[0]
             is not None]
    print(f"   闩武装拍 {len(armed) / n:.1%}（其中仍在贴边 "
          f"{sum(1 for r in armed if r.get('sel_why') in ('pass', 'graze')) / max(len(armed), 1):.0%}）")
    # 回合归属：墙钟落在本 trace 首尾 ±90s 的 flush
    w0, w1 = _wall(ticks[0]), _wall(ticks[-1])
    my_rounds = [(t, kv) for t, kv in rounds
                 if w0 - 90 <= t <= w1 + 90 and "overtake" in kv]
    for t, kv in my_rounds:
        print(f"   回合 {dt.datetime.fromtimestamp(t):%H:%M:%S}：超车 {kv.get('overtake')}"
              f" 金币 {kv.get('collect_coin')} 退场 {kv.get('destroy_car')}"
              f" 里程 {int(kv.get('mileage_cm', 0)) / 100:.0f}m")
    # 事件对齐（只算 ±0.3s 内有 trace 拍的事件）
    if events:
        ev = []
        for t, ty in events:
            idx = _events_near(ticks, t)
            if idx is not None:
                ev.append((t, ty, idx))
        hit = miss = 0
        gaps = []
        rows = []
        for t, ty, idx in ev:
            rec = ticks[idx]
            j = idx
            while j > 0 and _wall(rec) - _wall(ticks[j]) < 1.2:
                j -= 1
            pre = ticks[j]
            pre_pass = pre.get("sel_why") in ("pass", "graze")
            if pre_pass:
                hit += 1
            else:
                miss += 1
            gap = None
            ego = (rec.get("sel_dbg") or {}).get("ego")
            for o in (rec.get("scene") or {}).get("objs") or []:
                if not o or o[0] != "car" or len(o) < 15:
                    continue
                if o[6] not in ("metric", "metric_coarse", "line") or not o[9]:
                    continue
                if ego is None:
                    continue
                dx = abs(o[1] / 3.5 - ego)
                if gap is None or dx < gap:
                    gap = dx
            if gap is not None:
                gaps.append(gap)
            rows.append((t, ty, pre.get("sel_why"), rec.get("sel_why"), gap))
        tot = hit + miss
        if tot:
            print(f"   120 事件（±0.3s 内有拍）{tot}："
                  f"判定前 1.2s 仍贴边 {hit}/{tot} = {hit / tot:.0%}")
            inband = sum(1 for g in gaps if 0.54 <= g <= 0.77)
            print(f"   判定拍最近安全车横距 P50 {_median(gaps):.2f} 道；"
                  f"进带 [0.54,0.77] {inband}/{len(gaps)}（近似：最近车不一定是被超车）")
        for t, ty, pw, rw, g in rows:
            print(f"     {dt.datetime.fromtimestamp(t):%H:%M:%S} {ty:10s}"
                  f" 起意={pw} 判定={rw} gap={None if g is None else round(g, 2)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", required=True,
                    help="label=path")
    ap.add_argument("--game-log", action="append", default=[])
    ap.add_argument("--head", type=float, default=12.0)
    ap.add_argument("--tail", type=float, default=10.0)
    args = ap.parse_args()
    events = []
    rounds = []
    for gl in args.game_log:
        ev, rd = _game_events(gl)
        events.extend(ev)
        rounds.extend(rd)
        print(f"游戏日志 {gl.rsplit('/', 1)[-1]}：120 事件 {len(ev)}，"
              f"回合记录 {len(rd)}")
    for spec in args.trace:
        label, _, path = spec.partition("=")
        analyze_trace(path, label or path, args.head, args.tail, events, rounds)


if __name__ == "__main__":
    main()