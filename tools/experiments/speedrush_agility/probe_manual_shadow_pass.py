"""人工判定拍上的影子选择检查（speedrush，离线）。

**问什么**：人工贴着车过的那一刻，以及前 1.2 s，程序影子选择有没有选中
这辆车、指令线是不是 0.68 道？和程序自己真正跑出来的判定拍比，差在哪。

口径：

- 判定拍 = `dispatchPlayerCarScore ... score=120` 的时刻对到的 trace 最近拍；
  被打分车 = 判定拍 |t|≤0.7、横向最近的邻道真车（非 grid）。
- 起意拍 = 判定拍 −1.2 s；按被打分车的 oid 找回同一辆车。
- 选择读数取 trace 的 `sel_dbg`（人工局是影子选择、程序局是生产选择）。
- 指令偏移 = `sel_dbg.target` − 车心（两者都换算到道）；只在命中车 = 被打分车
  时统计。
- 人工窗口必须按 `debug/speedrush/*_manual_p*/meta.json` 的起止裁剪。

局限：人工 trace 的影子选择是当时的代码（2026-11-09），不是当前 HEAD 的
重放——它回答「选择层当时有没有这个意图」，不回答「现在的代码会不会」。

用法::

    .venv/Scripts/python.exe -X utf8 probe_manual_shadow_pass.py \
        --trace <trace> [--trace ...] --log <g112.log> \
        [--start ISO --end ISO] [--label 名称]
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import re
import statistics

LANE_W = 3.5
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


def _nearest(ticks, t):
    i = bisect.bisect_left([r["ts_wall_ns"] for r in ticks], t)
    cand = [ticks[j] for j in (i - 1, i, i + 1) if 0 <= j < len(ticks)]
    if not cand:
        return None
    cand.sort(key=lambda r: abs(r["ts_wall_ns"] - t))
    return cand[0]


def _cars(rec):
    out = {}
    for o in (rec.get("scene") or {}).get("objs") or []:
        if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
            continue
        if o[14] is not None and o[14] < 0:
            continue
        out[o[14]] = {"x": o[1] / LANE_W, "t": o[2], "z": o[7]}
    return out


def _nearest_car(cars, ego):
    cands = [c for c in cars.items()
             if c[1]["t"] is not None and abs(c[1]["t"]) <= 0.7
             and abs(c[1]["x"] - ego) <= 1.2]
    if not cands:
        return None
    return min(cands, key=lambda kv: abs(kv[1]["t"]))[0]


def _state(rec, oid):
    sd = rec.get("sel_dbg") or {}
    cars = _cars(rec)
    ego_m = (rec.get("scene") or {}).get("ego_m")
    ego = None if ego_m is None else ego_m / LANE_W
    car = cars.get(oid)
    tgt = sd.get("target")
    hit = sd.get("hit_oid")
    off = None
    if car is not None and tgt is not None:
        off = tgt - car["x"]
    return {
        "fid": rec.get("fid"), "sel": rec.get("sel_why"), "target": tgt,
        "hit": hit, "hit_is_car": hit == oid, "off": off,
        "car": car, "ego": ego, "why": (rec.get("route") or {}).get("why"),
    }


def analyze(path: str, log_path: str, start, end, label: str) -> None:
    ticks = _load(path)
    if not ticks:
        print(f"[skip] {path}")
        return
    if start and end:
        t0, t1 = start, end
    else:
        t0 = dt.datetime.fromtimestamp(ticks[0]["ts_wall_ns"] / 1e9) - dt.timedelta(seconds=2)
        t1 = dt.datetime.fromtimestamp(ticks[-1]["ts_wall_ns"] / 1e9) + dt.timedelta(seconds=2)
    ev = [(ts, k) for ts, k in _events(log_path, t0, t1) if k == "overtake"]
    rows = []
    for ts, _ in ev:
        j = _nearest(ticks, ts)
        if j is None:
            continue
        ego = ((j.get("scene") or {}).get("ego_m") or 0) / LANE_W
        oid = _nearest_car(_cars(j), ego)
        if oid is None:
            rows.append({"judge": None, "early": None})
            continue
        early = _nearest(ticks, ts - 1.2e9)
        rows.append({
            "judge": _state(j, oid),
            "early": _state(early, oid) if early is not None else None,
        })
    judged = [r for r in rows if r["judge"]]
    print(f"== {label}  120 超车 {len(ev)}  可判 {len(judged)}")
    for key, name in (("judge", "判定拍"), ("early", "前1.2s")):
        sub = [r[key] for r in judged if r[key]]
        pg = sum(1 for s in sub if s["sel"] in ("pass", "graze"))
        same = sum(1 for s in sub if s["hit_is_car"])
        same_pg = sum(1 for s in sub if s["hit_is_car"]
                      and s["sel"] in ("pass", "graze"))
        offs = [abs(s["off"]) for s in sub
                if s["hit_is_car"] and s["off"] is not None
                and s["sel"] in ("pass", "graze")]
        med = statistics.median(offs) if offs else float("nan")
        print(f"   {name}: n {len(sub)}  pass/graze {pg}  命中本车 {same}"
              f"  命中且贴边 {same_pg}  指令偏移中位 {med:.2f} 道"
              f"  偏移样本 {len(offs)}")
        for s in sub:
            print(f"     {name} fid {s['fid']} sel {s['sel']} 命中 {s['hit']}"
                  f"{'*' if s['hit_is_car'] else ''} 目标 {s['target']}"
                  f" 车心 {None if s['car'] is None else round(s['car']['x'],2)}"
                  f" 偏移 {None if s['off'] is None else round(s['off'],2)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    st = dt.datetime.fromisoformat(a.start) if a.start else None
    en = dt.datetime.fromisoformat(a.end) if a.end else None
    for p in a.trace:
        analyze(p, a.log, st, en, a.label or p)
