"""横移活动与「相对横位摆幅」归因（speedrush，离线）。

**问什么**：碰撞前看到的「肇事车几秒内横移三米」到底是社会车在变道，还是自车自己
横移造成的相对位移？RULES 口径是社会车辆几乎不变道，所以先把自车的横移活动量与
人工局对照，再把每次碰撞的相对横位摆幅拆成「自车位移」和「余项」。

口径：

- **横移活动**：`|v_lat|`（车体横速，m/s）超过 0.3 / 0.8 的拍占比与中位；
  横位跨半道次数（`executed_lane` 跨 0.5 边界）作为变道次数的近似。
- **摆幅归因**：碰撞触发前 0.8 秒窗口内，`Δ相对` = 肇事车相对横位变化，
  `Δ自车` = 自车道位移 × 道宽；`余项` = Δ相对 + Δ自车。余项才是「车自己走了多少」，
  但它仍含道路弯曲与锚点噪声，只能当量级参考，不能当变道证据。

用法::

    .venv/Scripts/python.exe -X utf8 probe_lateral_swing.py <会话 ...>
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
LANE_W = 3.5
WIN_S = 0.8


def load(sess: str):
    p = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    ticks = [json.loads(l) for l in open(p, encoding="utf-8")
             if l.startswith("{") and '"fid"' in l]
    ticks.sort(key=lambda r: r["ts_ns"])
    return ticks


def _row(r, oid):
    for x in ((r.get("otab") or {}).get("t") or []):
        if len(x) > 12 and x[0] == "car" and x[1] == oid:
            return x
    return None


def analyze(sess: str) -> None:
    ticks = load(sess)
    dur = (ticks[-1]["ts_ns"] - ticks[0]["ts_ns"]) / 1e9
    lat = [abs(r.get("v_lat") or 0.0) for r in ticks]
    n = len(lat)
    cross, prev = 0, None
    for r in ticks:
        e = r.get("executed_lane")
        if e is None:
            continue
        if prev is not None and int(e // 0.5) != int(prev // 0.5):
            cross += 1
        prev = e
    nc = sum(len(r.get("collisions") or []) for r in ticks)
    print(f"== {sess}  {dur:.0f}s  拍 {n}  碰撞 {nc}")
    print(f"   横移活动 |v_lat|>0.3 道/秒 {sum(1 for x in lat if x > 0.3)/n:.0%}"
          f"  >0.8 道/秒 {sum(1 for x in lat if x > 0.8)/n:.0%}"
          f"  中位 {st.median(lat):.2f} 道/秒（≈{st.median(lat)*LANE_W:.2f} m/s）"
          f"  跨半道 {cross}（{cross/dur*60:.0f}/分钟）")

    idx = {r["fid"]: i for i, r in enumerate(ticks)}
    for r in ticks:
        for c in (r.get("collisions") or []):
            oid, fid = c["id"], c["fid"]
            i = idx.get(fid)
            if i is None:
                continue
            t0 = ticks[i]["ts_ns"]
            back = next((j for j in range(i, -1, -1)
                         if ticks[j]["ts_ns"] <= t0 - WIN_S * 1e9), i)
            o1 = _row(ticks[back], oid)
            o2 = _row(ticks[i], oid)
            if not o1 or not o2 or None in (o1[3], o2[3],
                                            ticks[back].get("executed_lane"),
                                            ticks[i].get("executed_lane")):
                continue
            dx_rel = o2[3] - o1[3]
            # 自车横移：v_lat 单位是道/秒（planner 注释），积分后乘道宽；executed_lane
            # 会在重锚时跳变，不能拿来当平滑横位（2026-11-19 踩过）
            dx_ego = 0.0
            for j in range(back + 1, i + 1):
                dt = (ticks[j]["ts_ns"] - ticks[j - 1]["ts_ns"]) / 1e9
                dx_ego += (ticks[j].get("v_lat") or 0.0) * LANE_W * dt
            print(f"   id{oid} 触发前 {WIN_S:.1f}s：Δ相对 {dx_rel:+.2f}m"
                  f"  Δ自车 {dx_ego:+.2f}m  余项 {dx_rel + dx_ego:+.2f}m")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
