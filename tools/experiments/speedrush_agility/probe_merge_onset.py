"""并线起手探针（speedrush，离线）。

**问什么**：每次并线起手时本道还剩多少时间（route.tc）、起手前 1.5 s 在做什么、
上一个动作是并线还是擦边让位、何时结束。第八轮只给了汇总（12/14 起手拍 <
1.2 s），这里给逐条链，用来区分「上一动作没交出去就接并线」和「一直空闲却
晚起手」。

口径（与 README 第八轮一致）：

- **起手拍** = route.vd == merge_start 的拍；**起手 TTC** = 该拍 route.tc
  （生产同一份 cur_ttc，源道最早到站时间）。
- **近距事件**：检测框完整在画面内（0 ≤ top、bottom ≤ 719）、高 60~500 px、
  连续 ≥3 拍、接地距离 zg ≤ 5 m、横向 ≤0.54 道（两车半宽和）。坏框（框底在
  画面外、框高覆盖全屏）不计——第七轮已证明它会把距离算成 1.5~3 m。
- **起手前状态**：取起手前 1.5 s 最近的一拍，报 route.why / sel_why / 执行道。
- **上一动作**：起手前最近的 finish/cancel/timeout 拍，报结束方式与间隔；
  若起手前 1.5 s 已在 merging，则说明是「换目标重起手」，单列。

用法::

    .venv/Scripts/python.exe -X utf8 probe_merge_onset.py <会话 ...>
"""

from __future__ import annotations

import collections
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402
from probe_event_classes import _banners  # noqa: E402

IMG_H = 720
NEAR_M = 5.0
HALF_LANE = 0.54
LANE_W = 3.5
BOX_MIN_H, BOX_MAX_H = 60, 500
SUSTAIN = 3


def _visible_box(bottom, h):
    """框完整在画面内且高度合理；bottom 是 `P._cars` 返回的框底接地行。"""
    if bottom is None or h is None:
        return False
    top = bottom - h
    return top >= -1 and bottom <= IMG_H + 1 and BOX_MIN_H <= h <= BOX_MAX_H


def _contacts(ticks):
    """可见框近距事件：按 (run_id, oid) 分组，连续 ≥3 拍才入账。"""
    per = collections.defaultdict(list)
    for r in ticks:
        ego = (r.get("scene") or {}).get("ego_m")
        if ego is None:
            continue
        for oid, x, t, z, src, zg, h, clip, cb in P._cars(r):
            if x is None or zg is None:
                continue
            if not _visible_box(cb, h):
                continue
            per[(r.get("run_id"), oid)].append(
                (r, abs(x - ego) / LANE_W, zg))
    out = {}
    for key, seq in per.items():
        cc = [s for s in seq if s[1] <= HALF_LANE and s[2] <= NEAR_M]
        if len(cc) < SUSTAIN:
            continue
        best = min(cc, key=lambda s: s[2])
        out[key] = {"fid": best[0]["fid"], "ts": best[0]["ts_ns"],
                    "gap": best[1], "zg": best[2]}
    return out


def _episodes(ticks):
    eps, cur, prev_ref = [], None, None
    for r in ticks:
        rt = r.get("route") or {}
        vd = rt.get("vd")
        if vd == "merge_start":
            if cur is not None:
                cur["end"] = "restart"
                eps.append(cur)
            cur = {"run": r.get("run_id"), "start": r, "prev_ref": prev_ref,
                   "end": None, "end_fid": None}
        elif vd in ("finish", "cancel", "timeout") and cur is not None:
            cur["end"] = vd
            cur["end_fid"] = r["fid"]
            cur["end_ts"] = r["ts_ns"]
            eps.append(cur)
            cur = None
        if rt.get("ref") is not None:
            prev_ref = rt["ref"]
    return eps


def _at(ticks, idx, ts_ns, offset_s):
    """idx 之前、距起手拍 offset_s 的最近一拍（同 run 内由调用方保证）。"""
    best, bestd = None, None
    for j in range(idx, -1, -1):
        r = ticks[j]
        if r.get("run_id") != ticks[idx].get("run_id"):
            break
        d = (ts_ns - r["ts_ns"]) / 1e9
        if d > offset_s + 3.0:
            break
        if best is None or abs(d - offset_s) < bestd:
            best, bestd = j, abs(d - offset_s)
    return best


def _fmt_state(r):
    if r is None:
        return "—"
    rt = r.get("route") or {}
    ex = (r.get("lat_ctrl") or {}).get("exec")
    ex = "—" if ex is None else f"{ex:+.2f}"
    return f"{rt.get('vd')}/{rt.get('why')} sel={r.get('sel_why')} ex={ex}"


def analyze(sess: str) -> None:
    ticks, stage_s = P.load(sess)
    banners = _banners(sess)
    contacts = _contacts(ticks)
    eps = _episodes(ticks)
    by_key = {}
    for j, r in enumerate(ticks):
        by_key[(r.get("run_id"), r["fid"])] = j
    print(f"== {sess}  阶段内 {stage_s:.0f}s  拍 {len(ticks)}"
          f"  并线起手 {len(eps)}  可见框近距事件 {len(contacts)}")

    ends = collections.Counter(e["end"] for e in eps)
    print(f"   结束方式 {dict(ends)}")

    for ep in eps:
        r0 = ep["start"]
        rt = r0.get("route") or {}
        idx = by_key[(r0.get("run_id"), r0["fid"])]
        tc = rt.get("tc")
        j15 = _at(ticks, idx, r0["ts_ns"], 1.5)
        # 起手前的上一个结束拍
        prev_end, prev_end_gap = None, None
        for j in range(idx - 1, -1, -1):
            r = ticks[j]
            if r.get("run_id") != r0.get("run_id"):
                break
            if (r.get("route") or {}).get("vd") in (
                    "finish", "cancel", "timeout"):
                prev_end = r
                prev_end_gap = (r0["ts_ns"] - r["ts_ns"]) / 1e9
                break
        # 上一段并线的起手目标（撤销/完成前目标道）
        prev_start = None
        for j in range(idx - 1, -1, -1):
            r = ticks[j]
            if r.get("run_id") != r0.get("run_id"):
                break
            if (r.get("route") or {}).get("vd") == "merge_start":
                prev_start = r
                break
        # 起手前后 2 s 的横幅
        bn = [k for ts, k in banners if abs(ts - r0["ts_ns"]) <= 2e9]
        # 起手后本回合内、起手后 3 s 内的可见框近距事件
        got = []
        for (run, oid), c in contacts.items():
            if run != r0.get("run_id"):
                continue
            d = (c["ts"] - r0["ts_ns"]) / 1e9
            if -0.5 <= d <= 3.0:
                got.append(f"oid{oid}@{c['fid']} zg{c['zg']:.1f}"
                           f" gap{c['gap']:.2f}")
        # 起手前后 2 s 的状态串（每 0.5 s）
        tl = []
        for off in (2.0, 1.5, 1.0, 0.5, 0.15):
            j = _at(ticks, idx, r0["ts_ns"], off)
            if j is not None and (ticks[j].get("run_id")
                                  == r0.get("run_id")):
                tl.append(f"-{off:g}s:{_fmt_state(ticks[j])}")
        pe = ("—" if prev_end is None else
              f"{(prev_end.get('route') or {}).get('vd')}"
              f"@{prev_end['fid']} -{prev_end_gap:.1f}s")
        ps = ("—" if prev_start is None else
              f"start@{prev_start['fid']}"
              f" tgt {(prev_start.get('route') or {}).get('tgt')}"
              f" {(prev_start.get('route') or {}).get('why')}")
        ex15 = (ticks[j15].get("lat_ctrl") or {}).get("exec")
        ex0 = (r0.get("lat_ctrl") or {}).get("exec")
        drift = ("—" if ex15 is None or ex0 is None else f"{ex0 - ex15:+.2f}")
        print(f"   [{r0.get('run_id')} fid {r0['fid']}] why {rt.get('why')}"
              f" tgt {rt.get('tgt')} tc {tc} ev {rt.get('ev')}"
              f" blk {rt.get('blk')} 结束 {ep['end']}@{ep['end_fid']}")
        print(f"       1.5s前 {_fmt_state(ticks[j15]) if j15 is not None else '—'}"
              f" | 上一动作 {pe} | 上一段 {ps}")
        print(f"       横移 1.5s内 {drift} 道 | 横幅 {'+'.join(bn) or '—'}")
        print(f"       状态串 {' | '.join(tl)}")
        if got:
            print(f"       起手后近距 {' ; '.join(got)}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
