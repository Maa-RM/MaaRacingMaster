"""贴边目标翻面 × 纵深跳变的构成拆解（speedrush，离线）。

**问什么**：未知态 A/B 组（204716）90 次贴边目标翻面里 47 次与纵深跳变/
断供同拍（probe_x_z_coupling）——按设计该被横位未知态截住却没截。本探针
把这 47 次（对照其余 43 次）按四个维度拆构成：

1. **z 来源**：翻面拍命中车的 z 是不是框高比桥接值（`b` 标记）——桥接
   z 的跳变按设计不触发换锚重置（桥接阶梯是测量噪声），未知态判据因此
   看不见（观察点 ②）。
2. **锚龄**：命中车锚龄是否刚重置（<200ms）——新锚首拍横位无可信历史，
   弯道修正项跟新锚跳（fid2074 病灶），重武装拍目标就跳（观察点 ③）。
3. **同道/邻道**：命中车 |x−ego| ≤0.5 道记同道——未知态只关邻道得分
   候选，同道目标的变化设计上不滤（观察点 ①）。
4. **修正项占比**：翻面量里弯道修正项变化 Δc = center(z_prev)−center(z)
   占多少——|Δc| ≥ 0.5 道记「修正项主导」（z_reset 病灶签名：横位读数
   没坏、坐标系在动）。

口径与 probe_x_z_coupling 同源：翻面 = sel_why∈(pass,graze) 且相邻拍
|Δtarget|>0.5 道；断供 = 上一拍 z 有值本拍 None；跳变 = |Δz|>5 m；
只统计近场（上一拍 z≤40 m 或 t≤1.5 s）。

用法::

    .venv/Scripts/python.exe -X utf8 probe_zflip_anatomy.py <会话 ...>
"""

from __future__ import annotations

import collections
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402
from probe_x_z_coupling import LANE_W, Z_JUMP_M, _center  # noqa: E402

NEW_ANCHOR_MS = 200.0    # 锚龄 < 此值记「刚重锚」（重武装拍候选）
SAME_LANE = 0.5          # |x−ego| ≤ 此值记同道（未知态不滤）
FLIP_DOMINANT = 0.5      # |Δ修正| ≥ 此值记修正项主导


def analyze(sess: str) -> None:
    ticks, stage_s = P.load(sess)
    prev = {}          # (run_id, oid) → (z, t, x)
    prev_target = {}   # run_id → (target, hit_z, hit_oid)
    rows = []
    for r in ticks:
        road = (r.get("scene") or {}).get("road")
        ego = ((r.get("scene") or {}).get("ego_m") or 0.0) / LANE_W
        mcars = {m.get("id"): m for m in r.get("mcars") or [] if m}
        for o in (r.get("scene") or {}).get("objs") or []:
            if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
                continue
            prev[(r.get("run_id"), o[14])] = (o[7], o[2], o[1] / LANE_W)
        sd = r.get("sel_dbg") or {}
        if r.get("sel_why") not in ("pass", "graze") \
                or sd.get("target") is None or sd.get("hit_oid") is None:
            continue
        hit = next((o for o in (r.get("scene") or {}).get("objs") or []
                    if o and len(o) > 14 and o[14] == sd["hit_oid"]), None)
        z = None if hit is None else hit[7]
        x = None if hit is None else hit[1] / LANE_W
        last = prev_target.get(r.get("run_id"))
        prev_target[r.get("run_id")] = (sd["target"], z, sd["hit_oid"])
        if last is None or abs(sd["target"] - last[0]) <= 0.5:
            continue
        switched = last[2] != sd["hit_oid"]   # 翻面拍换了目标车
        pz = last[1]
        near = (pz is not None and pz <= 40.0) or \
               (z is not None and z is not None and z <= 40.0)
        if hit is not None and hit[2] is not None and hit[2] <= 1.5:
            near = True
        if not near:
            continue
        m = mcars.get(sd["hit_oid"]) or {}
        if z is None and pz is not None:
            cls = "断供"
        elif z is not None and pz is not None and abs(z - pz) > Z_JUMP_M:
            cls = "纵深跳变"
        else:
            cls = "平稳"
        dc = abs(_center(road, pz) - _center(road, z)) / LANE_W
        lane = "同道" if (x is not None and abs(x - ego) <= SAME_LANE) \
            else ("邻道" if x is not None else "无横位")
        rows.append({
            "cls": cls, "fid": r.get("fid"),
            "bridged": bool(m.get("b")) or m.get("bz") is not None,
            "age": m.get("age"),
            "lane": lane,
            "switched": switched,
            "dc": dc, "flip": abs(sd["target"] - last[0]),
        })

    coup = [r for r in rows if r["cls"] in ("断供", "纵深跳变")]
    calm = [r for r in rows if r["cls"] == "平稳"]
    print(f"== {sess}  阶段内 {stage_s:.0f}s  翻面 {len(rows)} 次"
          f"（纵深耦合 {len(coup)} / 平稳 {len(calm)}）")
    for label, grp in (("纵深耦合(47的同类)", coup), ("平稳翻面", calm)):
        if not grp:
            continue
        n = len(grp)
        n_b = sum(1 for r in grp if r["bridged"])
        n_new = sum(1 for r in grp
                    if r["age"] is not None and r["age"] < NEW_ANCHOR_MS)
        n_same = sum(1 for r in grp if r["lane"] == "同道")
        n_dc = sum(1 for r in grp if r["dc"] >= FLIP_DOMINANT)
        n_sw = sum(1 for r in grp if r["switched"])
        n_all = sum(1 for r in grp if r["bridged"] or
                    (r["age"] is not None and r["age"] < NEW_ANCHOR_MS)
                    or r["lane"] == "同道")
        print(f"   {label} {n} 次:")
        print(f"     换了目标车（选择层切目标）vs 同车读数跳: "
              f"{n_sw}（{n_sw/n:.0%}）/ {n - n_sw}（{(n-n_sw)/n:.0%}）")
        print(f"     桥接 z 在场（门看不见，观察点②）: {n_b}（{n_b/n:.0%}）")
        print(f"     锚龄<{NEW_ANCHOR_MS:.0f}ms 刚重锚（重武装拍，观察点③）:"
              f" {n_new}（{n_new/n:.0%}）")
        print(f"     同道目标（未知态设计不滤，观察点①）: {n_same}（{n_same/n:.0%}）")
        print(f"     修正项主导（|Δc|≥{FLIP_DOMINANT} 道，z_reset 病灶）:"
              f" {n_dc}（{n_dc/n:.0%}）")
        print(f"     ①②③ 任一命中（未知态口径外）: {n_all}（{n_all/n:.0%}）")
    # 明细（耦合类逐条）
    print("   耦合类明细（fid 类别 换车/桥接/锚龄/车道 Δ修正/翻面量）:")
    for r in coup:
        age = "-" if r["age"] is None else f"{r['age']:.0f}ms"
        print(f"     fid {r['fid']}  {r['cls']}  "
              f"{'换车' if r['switched'] else '同车'}  "
              f"{'桥接' if r['bridged'] else '锚点'}  锚龄{age}"
              f"  {r['lane']}  Δc{r['dc']:.2f}/翻{r['flip']:.2f}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
