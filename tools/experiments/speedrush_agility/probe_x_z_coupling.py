"""横位×纵深耦合探针（speedrush，离线）。

**问什么**：贴边目标为什么会在 0.3 s 内翻 1~1.8 道？选择层的车横位是

    x = x_m − origin − center_at(z)

`center_at` 是按**该车纵深**插值的弯道中心线修正；纵深一断供（z=None）修正
直接归零，纵深一跳（换锚）修正跟着跳。本探针把相邻两拍的实际横位变化拆成
「修正项变化」与「残差」，看横位跳变有多少来自纵深断供/跳变。

口径：

- **断供**：上一拍 z 有值、本拍 z=None。
- **跳变**：两拍都有 z 且 |Δz|>5 m。
- **修正变化** Δc = center(z_prev) − center(z_cur)（z 缺省按 0 修正）。
- 只统计近场（上一拍 z≤40 m 或到站 t≤1.5 s），远车修正本来就该慢变。
- 另外统计贴边目标翻面（相邻两拍 |Δtarget|>0.5 道）时命中车是否恰好断供/
  跳变——目标翻面若与断供同步，说明是输入项在推目标，不是选择层改主意。

用法::

    .venv/Scripts/python.exe -X utf8 probe_x_z_coupling.py <会话 ...>
"""

from __future__ import annotations

import collections
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402

LANE_W = 3.5
Z_JUMP_M = 5.0


def _center(road: dict | None, z: float | None) -> float:
    """与 `RoadFrame.center_at` 同算法：分带中点线性插值，z=None 记 0。"""
    if z is None or not road:
        return 0.0
    pts = [(0.5 * (b[0] + b[1]), 0.5 * (b[2] + b[3]))
           for b in road.get("bands") or []
           if len(b) >= 4 and b[2] is not None and b[3] is not None]
    if not pts:
        return 0.0
    if z <= pts[0][0]:
        return pts[0][1]
    for (z0, c0), (z1, c1) in zip(pts, pts[1:]):
        if z <= z1:
            w = 0.0 if z1 <= z0 else (z - z0) / (z1 - z0)
            return c0 + (c1 - c0) * w
    return pts[-1][1]


def _median(vals):
    vals = sorted(vals)
    return vals[len(vals) // 2] if vals else float("nan")


def analyze(sess: str) -> None:
    ticks, stage_s = P.load(sess)
    prev = {}
    st = collections.defaultdict(lambda: {"n": 0, "dx": [], "dc": [], "res": []})
    flips = collections.Counter()
    for r in ticks:
        road = (r.get("scene") or {}).get("road")
        for o in (r.get("scene") or {}).get("objs") or []:
            if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
                continue
            oid, x, t, z = o[14], o[1] / LANE_W, o[2], o[7]
            k = (r.get("run_id"), oid)
            pr = prev.get(k)
            if pr is not None:
                pz, pt, px = pr
                near = (pz is not None and pz <= 40.0) or \
                       (pt is not None and pt <= 1.5)
                if near:
                    if pz is not None and z is None:
                        cls = "断供"
                    elif pz is not None and z is not None \
                            and abs(z - pz) > Z_JUMP_M:
                        cls = "纵深跳变"
                    elif pz is None and z is not None:
                        cls = "恢复"
                    else:
                        cls = "平稳"
                    dc = (_center(road, pz) - _center(road, z)) / LANE_W
                    dx = x - px
                    e = st[cls]
                    e["n"] += 1
                    e["dx"].append(abs(dx))
                    e["dc"].append(abs(dc))
                    e["res"].append(abs(dx - dc))
            prev[k] = (z, t, x)
        # 贴边目标翻面是否与命中车断供/跳变同拍
        sd = r.get("sel_dbg") or {}
        if r.get("sel_why") in ("pass", "graze") \
                and sd.get("target") is not None \
                and sd.get("hit_oid") is not None:
            fk = r.get("run_id")
            hit = next((o for o in (r.get("scene") or {}).get("objs") or []
                        if o and len(o) > 14 and o[14] == sd["hit_oid"]),
                       None)
            z = None if hit is None else hit[7]
            last = prev.get((fk, "target"))
            if last is not None and abs(sd["target"] - last[0]) > 0.5:
                pz = last[1]
                if z is None and pz is not None:
                    flips["翻面且纵深断供"] += 1
                elif z is not None and pz is not None \
                        and abs(z - pz) > Z_JUMP_M:
                    flips["翻面且纵深跳变"] += 1
                else:
                    flips["翻面其余"] += 1
            prev[(fk, "target")] = (sd["target"], z)

    print(f"== {sess}  阶段内 {stage_s:.0f}s")
    for cls in ("断供", "纵深跳变", "恢复", "平稳"):
        e = st[cls]
        if not e["n"]:
            continue
        print(f"   {cls:6s} n {e['n']:5d}  |Δx|中位 {_median(e['dx']):.2f} 道"
              f"  |Δ修正|中位 {_median(e['dc']):.2f}"
              f"  |残差|中位 {_median(e['res']):.2f}"
              f"  |Δx|>1道 {sum(v > 1.0 for v in e['dx']) / e['n']:.1%}")
    print(f"   贴边目标翻面(>0.5道) {sum(flips.values())} 次"
          f"  {dict(flips)}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
