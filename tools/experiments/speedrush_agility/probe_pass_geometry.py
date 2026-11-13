"""120 分判定拍的贴车几何（人工 vs 程序，离线）。

**问什么**：人工到底贴多近？程序是「目标定错」还是「交付不准」？

口径与 `probe_manual_events.py` 同源：取游戏日志
``dispatchPlayerCarScore: type=... score=120`` 的时刻作为判定拍，对到 trace 的
最近一拍；在该拍用场景车表找被打分车（到站 t≈0 且横向最近的邻道车），报：

- 自车距最近车道中心的偏差（米，车道宽 3.5 m）。
- 自车与被打分车的横向中心距（道）：判定窗是 [0.54, 0.77] 道（游戏 120 分
  带），<0.54 = 车体重叠。
- 表面余量（米）= (中心距 − 0.54) × 3.5。

只做读数，不做裁决；判定拍的车表缺样会单独计数。

用法::

    .venv/Scripts/python.exe -X utf8 probe_pass_geometry.py \
        --trace <trace.jsonl> [--trace ...] --log <g112.log>
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import re
import statistics

LANE_W = 3.5
HALF_SUM = 0.54
TS_RE = re.compile(r"\[(\d{4}\.\d{2}\.\d{2})-(\d{2}\.\d{2}\.\d{2}):(\d{3})\]")
EV_RE = re.compile(r"dispatchPlayerCarScore: type=(\w+), score=(\d+)")


def _events(log_path: str, t0: dt.datetime, t1: dt.datetime):
    out = []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TS_RE.search(line)
            e = EV_RE.search(line)
            if not (m and e) or e.group(2) != "120":
                continue
            stamp = dt.datetime.strptime(
                m.group(1) + " " + m.group(2) + "." + m.group(3),
                "%Y.%m.%d %H.%M.%S.%f")
            if t0 <= stamp <= t1:
                out.append((stamp.timestamp() * 1e9, e.group(1)))
    return out


def _load(path: str):
    ticks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("fid") is not None and rec.get("scene"):
                ticks.append(rec)
    ticks.sort(key=lambda r: r["ts_wall_ns"])
    return ticks


def _nearest(keys, ticks, t):
    i = bisect.bisect_left(keys, t)
    cand = [ticks[j] for j in (i - 1, i, i + 1) if 0 <= j < len(ticks)]
    if not cand:
        return None
    cand.sort(key=lambda r: abs(r["ts_wall_ns"] - t))
    return cand[0]


def _pct(vals, q):
    vals = sorted(vals)
    if not vals:
        return float("nan")
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return vals[k]


def analyze(path: str, log_path: str,
            start: dt.datetime | None = None,
            end: dt.datetime | None = None) -> None:
    ticks = _load(path)
    if not ticks:
        print(f"[skip] 无有效拍 {path}")
        return
    keys = [r["ts_wall_ns"] for r in ticks]
    if start is not None and end is not None:
        t0, t1 = start, end
    else:
        t0 = dt.datetime.fromtimestamp(keys[0] / 1e9) - dt.timedelta(seconds=2)
        t1 = dt.datetime.fromtimestamp(keys[-1] / 1e9) + dt.timedelta(seconds=2)
    events = _events(log_path, t0, t1)
    rows = []
    miss = 0
    for ts, kind in events:
        rec = _nearest(keys, ticks, ts)
        if rec is None:
            miss += 1
            continue
        ego_m = (rec.get("scene") or {}).get("ego_m")
        if ego_m is None:
            miss += 1
            continue
        ego = ego_m / LANE_W
        cars = []
        for o in (rec.get("scene") or {}).get("objs") or []:
            if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
                continue
            if o[14] is not None and o[14] < 0:
                continue                      # grid 伪车不参与计分
            x = o[1] / LANE_W
            t = o[2]
            if abs(x - ego) > 1.2:
                continue
            cars.append((x, t, o[7]))
        if not cars:
            miss += 1
            continue
        # 判定拍：被打分车刚出 20 m 框（t≈0）；只有 |t|≤0.7 的邻道车算识别成功
        cands = [c for c in cars if c[1] is not None and abs(c[1]) <= 0.7]
        if not cands:
            miss += 1
            continue
        car = min(cands, key=lambda c: abs(c[1]))
        dx = abs(car[0] - ego)
        off = ego_m - round(ego_m / LANE_W) * LANE_W
        rows.append({"kind": kind, "fid": rec.get("fid"), "dx": dx,
                     "gap_m": (dx - HALF_SUM) * LANE_W,
                     "off_m": off, "t": car[1], "z": car[2]})
    name = path.split("logs")[-1].split("trace")[0].strip("\\/")
    print(f"== {name}  事件 {len(events)}  可用 {len(rows)}  缺表 {miss}")
    for kind in ("overtake", "laneChange"):
        sub = [r for r in rows if r["kind"] == kind]
        if not sub:
            continue
        dx = [r["dx"] for r in sub]
        gap = [r["gap_m"] for r in sub]
        off = [abs(r["off_m"]) for r in sub]
        print(f"   {kind:10s} n {len(sub):3d}"
              f"  中心距 P10 {_pct(dx,0.1):.2f} P50 {_pct(dx,0.5):.2f}"
              f" P90 {_pct(dx,0.9):.2f} 道"
              f"  [0.54,0.77] {sum(0.54 <= v <= 0.77 for v in dx)}"
              f"  <0.54 {sum(v < 0.54 for v in dx)}"
              f"  >0.77 {sum(v > 0.77 for v in dx)}"
              f"  表面余量 P50 {_pct(gap,0.5):.2f}m"
              f"  自车偏道心 P50 {_pct(off,0.5):.2f}m")
    for r in sorted(rows, key=lambda r: r["dx"])[:6]:
        print(f"     {r['kind']:10s} fid {r['fid']} 中心距 {r['dx']:.2f} 道"
              f" 余量 {r['gap_m']:.2f}m 偏道心 {r['off_m']:+.2f}m"
              f" t {r['t']} z {r['z']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args()
    start = (dt.datetime.fromisoformat(a.start) if a.start else None)
    end = (dt.datetime.fromisoformat(a.end) if a.end else None)
    for p in a.trace:
        analyze(p, a.log, start, end)
