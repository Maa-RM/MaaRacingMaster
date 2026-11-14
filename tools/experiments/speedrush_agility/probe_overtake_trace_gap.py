# -*- coding: utf-8 -*-
"""横幅事件 × trace：人工局极限超车的表面间隙分布。

**事件源**：HUD 横幅 OCR（`hud/hud.jsonl` 的 `event_banner`），连击数变化即
一次事件——不需要游戏日志给时刻。

**目标身份**：事件时刻对到同会话 trace 最近一拍，在逐车表里取**邻道车**
（0.4~1.5 道、纵深未知或 ≤5 m）中横向最小的一辆；同道前车（<0.4 道）与
远车不参与。被超车在判定拍通常已丢纵深、只剩像素横位，这一档正是它的样子。

**量法**：2D 尺子横位 `x_lane`（道），中心距 = x_lane×3.5 m，表面间隙 =
中心距 − 2×0.95 m。极限带按规则为 `0 < 间隙 ≤ 0.8 m`（中心距 0.54~0.77 道）。

输出 `overtake_gap_out/trace_summary.json`。
"""
from __future__ import annotations

import bisect
import json
import os
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import probe_overtake_scene as S  # noqa: E402

OUT = Path(__file__).parent / "overtake_gap_out"
SESSIONS = ["20261113_203839_manual_p1", "20261113_203839_manual_p2",
            "20261113_204052_manual_p1", "20261113_204052_manual_p2"]
LANE_W = 3.5
HALF_SUM = 1.9


def load_trace(sess: Path) -> list[dict]:
    meta = json.loads((sess / "meta.json").read_text(encoding="utf-8"))
    path = (Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "logs"
            / meta["session"] / "trace" / "speedrush.jsonl")
    rows = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            r = json.loads(ln)
            if r.get("type") is None and r.get("fid") is not None:
                rows.append(r)
    rows.sort(key=lambda r: r["ts_ns"])
    return rows


def main() -> None:
    OUT.mkdir(exist_ok=True)
    trace_by_session = {}
    records = []
    for name in SESSIONS:
        sess = S.DEBUG / name
        if not (sess / "hud" / "hud.jsonl").exists():
            continue
        meta = json.loads((sess / "meta.json").read_text(encoding="utf-8"))
        key = meta["session"]
        rows = trace_by_session.setdefault(key, load_trace(sess))
        tsl = [r["ts_ns"] for r in rows]
        for ev in S.banner_events(sess):
            i = bisect.bisect_left(tsl, ev)
            j = min(i, len(rows) - 1)
            if i > 0 and abs(tsl[i - 1] - ev) < abs(tsl[j] - ev):
                j = i - 1
            row = rows[j]
            cand = [e for e in (row.get("otab") or {}).get("t", [])
                    if e[0] == "car" and e[2] is not None
                    and 0.4 <= abs(e[2]) <= 1.5
                    and (e[4] is None or e[4] <= 5.0)]
            if not cand:
                records.append({"session": name, "event": ev, "gap": None})
                continue
            e = min(cand, key=lambda e: abs(e[2]))
            gap = abs(e[2]) * LANE_W - HALF_SUM
            records.append({"session": name, "event": ev,
                            "x_lane": abs(e[2]), "gap": gap,
                            "z": e[4], "src": e[9]})
    gaps = [r["gap"] for r in records if r["gap"] is not None]
    per = {}
    for name in SESSIONS:
        v = [r["gap"] for r in records
             if r["session"] == name and r["gap"] is not None]
        if v:
            per[name] = {"n": len(v), "median": st.median(v),
                         "in_band": sum(1 for g in v if 0 < g <= 0.8)}
    summary = {
        "events": len(records), "resolved": len(gaps),
        "in_band": sum(1 for g in gaps if 0 < g <= 0.8),
        "below0": sum(1 for g in gaps if g <= 0.0),
        "above": sum(1 for g in gaps if g > 0.8),
        "median": st.median(gaps) if gaps else None,
        "p10": st.quantiles(gaps, n=10)[0] if gaps else None,
        "p90": st.quantiles(gaps, n=10)[8] if gaps else None,
        "per_session": per,
    }
    (OUT / "trace_summary.json").write_text(
        json.dumps({"summary": summary, "records": records},
                   indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
