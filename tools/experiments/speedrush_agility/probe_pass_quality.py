#!/usr/bin/env python
"""贴边交付质量离线复算（speedrush）：掠过时刻的横向间距落在哪。

**问什么**：选择层说要贴某辆车（hit_oid）之后，车真的在计分带
[0.54, 0.77] 道里掠过它了吗？还是欠调（>0.77 只拿 30 分）或贴死（<0.54
= 车体接触）？这是 RULES §4.2 极限超车判定的几何口径，也是
`decision.json` 里写下的验收口径。

**口径**：只认真正发生掠过的段（段内到站时间出现过 t≤0.5），避免把
「车压根没过来」的贴边段算成成功或失败。每个段在若干 t 窗口各取一拍
（该窗口里第一拍），报 |自车 − 被贴车心| 的分布与进带/欠调/接触占比。
t 是到站时间（0=并排）；极超判定发生在对方车脱出车头前 20m 框时，对应
t≈1.2~2s，故 1.6~1.2 窗最接近判定时刻。

**注意（2026-11-09 裁决）**：判定帧正是被超车贴着自车侧方、被自车遮挡的时刻，
它的横位读数最差——本探针在 t<0.8s 的「接触」占比会系统性虚高（实证：某阶段
拿了 6 个动作事件，7 个掠过点在读数上全被分到接触/欠调）。**得分验收不用本
探针**，用 `probe_action_events.py` 的横幅计数；本探针只作交付趋势的辅助诊断，
看 t≥0.8s 的窗口。

**基线**（2026-11-09_094822，新近场模型四程序场；贴边与让位合计，真掠过
62 段）：判定窗 t 1.6~1.2s 横距中位 0.79 道，进带 3 / 欠调 8 / 接触 4；
1.2~0.8s 同样 0.79（3 / 6 / 3）。欠调量（自车−贴边目标）在 1.2~0.8s 中位
−0.14 道（欠 7 / 过 5）——即车还没走到 0.68 的贴边位。

用法：``.venv/Scripts/python.exe -X utf8 probe_pass_quality.py <trace.jsonl ...>``
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics

WINDOWS = ((1.6, 1.2), (1.2, 0.8), (0.8, 0.4), (0.4, 0.0))
BAND = (0.54, 0.77)


def _episodes(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("type") is not None:
                continue
            d = r.get("sel_dbg") or {}
            if d.get("ego") is None:
                continue
            cars = d.get("car") or []
            opp = (d.get("opp") or {}).get("car") or []
            ids = ([o[0] for o in opp] if len(opp) == len(cars)
                   else [None] * len(cars))
            rows.append({
                "ego": d["ego"], "tgt": d.get("target"),
                "why": r.get("sel_why"), "oid": d.get("hit_oid"),
                "cars": {i: (c[0], c[1]) for i, c in zip(ids, cars)},
            })
    eps, cur = [], None
    for row in rows:
        oid = row["oid"] if row["why"] in ("graze", "pass") else None
        if cur and (oid != cur["oid"] or row["why"] not in ("graze", "pass")):
            eps.append(cur)
            cur = None
        if oid is not None:
            c = row["cars"].get(oid)
            if c and c[1] is not None:
                if cur is None:
                    cur = {"oid": oid, "s": []}
                cur["s"].append((c[1], row["ego"] - c[0],
                                 None if row["tgt"] is None
                                 else row["tgt"] - c[0]))
    if cur:
        eps.append(cur)
    return eps


def analyze(path: str) -> dict:
    eps = _episodes(path)
    real = [e for e in eps if min(q[0] for q in e["s"]) <= 0.5]
    out: dict = {"episodes": len(eps), "real": len(real), "windows": {}}
    for lo, hi in WINDOWS:
        gaps, errs, cat = [], [], collections.Counter()
        for e in real:
            q = next((q for q in e["s"] if hi < q[0] <= lo), None)
            if q is None:
                continue
            gap = abs(q[1])
            gaps.append(gap)
            cat["band" if BAND[0] <= gap <= BAND[1]
                else "contact" if gap < BAND[0] else "far"] += 1
            if q[2] is not None and abs(q[2]) > 0.3:
                errs.append(q[1] - q[2])
        if gaps:
            out["windows"][f"{lo}~{hi}"] = {
                "n": len(gaps),
                "gap_p50": round(statistics.median(gaps), 2),
                "band": cat["band"], "far": cat["far"],
                "contact": cat["contact"],
                "target_err_p50": (round(statistics.median(errs), 2)
                                   if errs else None),
                "undershoot": sum(1 for x in errs if x < 0),
                "overshoot": sum(1 for x in errs if x > 0),
            }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    args = ap.parse_args()
    total = collections.Counter()
    for path in args.traces:
        res = analyze(path)
        print(f"== {path}")
        print(f"   段 {res['episodes']}，真掠过 {res['real']}")
        for win, v in res["windows"].items():
            print(f"   t {win}s  n={v['n']} 横距P50 {v['gap_p50']}  "
                  f"进带 {v['band']} / 欠调 {v['far']} / 接触 {v['contact']}"
                  f"  欠调量P50 {v['target_err_p50']}"
                  f"（欠 {v['undershoot']} / 过 {v['overshoot']}）")
            total[win + ":band"] += v["band"]
            total[win + ":far"] += v["far"]
            total[win + ":contact"] += v["contact"]
            total[win + ":n"] += v["n"]
    print("== 合计（进带 / 欠调 / 接触 / 段）")
    for lo, hi in WINDOWS:
        w = f"{lo}~{hi}"
        print(f"   t {w}s  {total[w+':band']} / {total[w+':far']} / "
              f"{total[w+':contact']} / {total[w+':n']}")


if __name__ == "__main__":
    main()
