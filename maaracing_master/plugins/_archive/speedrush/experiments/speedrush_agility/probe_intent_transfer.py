"""贴身意图按位置续接（离线影子，speedrush）。

**问什么**：判定拍前同一辆车经常换 oid 或掉出车表；如果贴边意图在 oid 断掉
时按**最后有效位置**续到新 oid，能救回多少事件？指令线还是 0.68 吗？

规则（影子，不改生产）：

- 意图 = 最近一次选择层 pass/graze 的命中车（oid、x、z、目标、侧别）。
- 同一 oid 还在表里：逐拍更新位置，意图不动。
- oid 消失：在最后位置 ±`LANE_TOL` 道、±`Z_TOL` 米内找车；**唯一**候选才续接
  （更新 oid/位置，保留目标相对车心的偏移与侧别）；0 个或多个候选不接。
- 意图断超 `GAP_S` 秒未续上即释放。
- 对照（oid 键）：oid 一消失就释放，不做位置续接。

验收量（41 个人工 120 超车）：

1. 判定拍时意图还活着且位置与被打分车相差 ≤`LANE_TOL` 道/`Z_TOL` 米；
2. 判定前 1.2 s 的指令偏移（目标−车心，道），应仍为 0.68；
3. 续接发生时的候选唯一率（多候选不接，避免接错车）。

只做读数，不作验收；自车轨迹取录制值，交付距离因此不变。

用法::

    .venv/Scripts/python.exe -X utf8 probe_intent_transfer.py \
        --trace <trace> --log <g112.log> --start ISO --end ISO --label 名称
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import re
import statistics

LANE_W = 3.5
LANE_TOL = 0.5
Z_TOL = 5.0
T_TOL = 0.5
GAP_S = 1.0
TS_RE = re.compile(r"\[(\d{4}\.\d{2}\.\d{2})-(\d{2}\.\d{2}\.\d{2}):(\d{3})\]")
EV_RE = re.compile(r"dispatchPlayerCarScore: type=(\w+), score=(\d+)")


def _events(log_path, t0, t1):
    out = []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TS_RE.search(line)
            e = EV_RE.search(line)
            if not (m and e) or e.group(2) != "120" or e.group(1) != "overtake":
                continue
            stamp = dt.datetime.strptime(
                m.group(1) + " " + m.group(2) + "." + m.group(3),
                "%Y.%m.%d %H.%M.%S.%f")
            if t0 <= stamp <= t1:
                out.append(stamp.timestamp() * 1e9)
    return out


def _load(path):
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


def _cars(rec):
    out = {}
    for o in (rec.get("scene") or {}).get("objs") or []:
        if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
            continue
        if o[14] is not None and o[14] < 0:
            continue
        out[o[14]] = {"x": o[1] / LANE_W, "z": o[7], "t": o[2]}
    return out


def _near(ticks, keys, t):
    i = bisect.bisect_left(keys, t)
    cand = [ticks[j] for j in (i - 1, i, i + 1) if 0 <= j < len(ticks)]
    if not cand:
        return None
    cand.sort(key=lambda r: abs(r["ts_wall_ns"] - t))
    return cand[0]


def _match(a, b):
    if a is None or b is None:
        return False
    if a["x"] is None or b["x"] is None:
        return False
    if abs(a["x"] - b["x"]) > LANE_TOL:
        return False
    if a["z"] is not None and b["z"] is not None:
        return abs(a["z"] - b["z"]) <= Z_TOL
    if a["t"] is not None and b["t"] is not None:
        return abs(a["t"] - b["t"]) <= T_TOL
    return True


def _candidates(cars, pos):
    out = []
    for oid, c in cars.items():
        if c["x"] is None:
            continue
        if abs(c["x"] - pos["x"]) > LANE_TOL:
            continue
        if pos["z"] is not None and c["z"] is not None:
            if abs(c["z"] - pos["z"]) > Z_TOL:
                continue
        elif pos["t"] is not None and c["t"] is not None:
            if abs(c["t"] - pos["t"]) > T_TOL:
                continue
        out.append(oid)
    return out


def _walk(ticks, keys, j_ts, scored_oid, transfer: bool):
    """从判定拍前 1.5 s 走到判定拍，返回意图状态与续接统计。"""
    i0 = bisect.bisect_left(keys, j_ts - 1.5e9)
    i1 = bisect.bisect_right(keys, j_ts)
    win = ticks[i0:i1]
    if len(win) < 3:
        return None
    intent = None          # {"oid","x","z","t","off","gap_s"}
    last_ts = None
    stats = {"transfers": 0, "ambiguous": 0, "drops": 0}
    for rec in win:
        ts = rec["ts_wall_ns"]
        dt_s = 0.0 if last_ts is None else (ts - last_ts) / 1e9
        last_ts = ts
        cars = _cars(rec)
        sd = rec.get("sel_dbg") or {}
        sel = rec.get("sel_why")
        hit = sd.get("hit_oid")
        if sel in ("pass", "graze") and hit in cars:
            car = cars[hit]
            tgt = sd.get("target")
            off = None if tgt is None or car["x"] is None else tgt - car["x"]
            intent = {"oid": hit, "x": car["x"], "z": car["z"],
                      "t": car["t"], "off": off, "gap_s": 0.0}
            continue
        if intent is None:
            continue
        if intent["oid"] in cars:                 # 同 oid 还在：位置更新
            car = cars[intent["oid"]]
            intent.update(x=car["x"], z=car["z"], t=car["t"], gap_s=0.0)
            continue
        if not transfer:
            stats["drops"] += 1
            intent = None
            continue
        cand = _candidates(cars, intent)
        if len(cand) == 1:
            oid = cand[0]
            car = cars[oid]
            intent.update(oid=oid, x=car["x"], z=car["z"], t=car["t"],
                          gap_s=0.0)
            stats["transfers"] += 1
        elif len(cand) > 1:
            stats["ambiguous"] += 1
            intent["gap_s"] += dt_s
            if intent["gap_s"] > GAP_S:
                intent = None
                stats["drops"] += 1
        else:
            intent["gap_s"] += dt_s
            if intent["gap_s"] > GAP_S:
                intent = None
                stats["drops"] += 1
    return intent, stats


def analyze(path, log_path, start, end, label):
    ticks = _load(path)
    if not ticks:
        print(f"[skip] {path}")
        return
    keys = [r["ts_wall_ns"] for r in ticks]
    if start and end:
        t0, t1 = start, end
    else:
        t0 = dt.datetime.fromtimestamp(keys[0] / 1e9) - dt.timedelta(seconds=2)
        t1 = dt.datetime.fromtimestamp(keys[-1] / 1e9) + dt.timedelta(seconds=2)
    res = {"n": 0, "base": 0, "tr": 0, "off": [], "transfers": 0,
           "ambiguous": 0, "drops": 0, "base_off": []}
    for ts in _events(log_path, t0, t1):
        j = _near(ticks, keys, ts)
        if j is None:
            continue
        jcars = _cars(j)
        ego = ((j.get("scene") or {}).get("ego_m") or 0) / LANE_W
        cand = [(oid, c) for oid, c in jcars.items()
                if c["t"] is not None and abs(c["t"]) <= 0.7
                and c["x"] is not None and abs(c["x"] - ego) <= 1.2]
        if not cand:
            continue
        oid, car = min(cand, key=lambda kv: abs(kv[1]["t"]))
        res["n"] += 1
        for key, transfer in (("base", False), ("tr", True)):
            got = _walk(ticks, keys, ts, oid, transfer)
            if got is None:
                continue
            intent, st = got
            if intent is not None and _match(intent, car):
                res[key] += 1
                if intent.get("off") is not None:
                    res["off" if key == "tr" else "base_off"].append(
                        abs(intent["off"]))
                if key == "tr":
                    res["transfers"] += st["transfers"]
                    res["ambiguous"] += st["ambiguous"]
                    res["drops"] += st["drops"]
    n = res["n"] or 1
    med = (statistics.median(res["off"]) if res["off"] else float("nan"))
    bmed = (statistics.median(res["base_off"]) if res["base_off"]
            else float("nan"))
    print(f"== {label}  事件 {n}")
    print(f"   判定拍意图活着且对上被打分车：oid 键 {res['base']}/{n}"
          f"  按位置续接 {res['tr']}/{n}"
          f"  （续接 {res['transfers']} 次、多候选 {res['ambiguous']}、"
          f"断开 {res['drops']}）")
    print(f"   意图指令偏移中位：oid 键 {bmed:.2f} 道  续接 {med:.2f} 道")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    st = dt.datetime.fromisoformat(a.start) if a.start else None
    en = dt.datetime.fromisoformat(a.end) if a.end else None
    analyze(a.trace, a.log, st, en, a.label or a.trace)
