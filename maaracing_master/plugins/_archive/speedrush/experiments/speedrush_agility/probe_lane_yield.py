"""车道内让位可行性（speedrush，离线）。

**问什么**：三次真碰撞里，如果不动车道、只在车道内横向让开，来不来得及？代价是什么？

口径（全部复用生产同一套量）：

- **包络**：横向中心距必须 ≥ 自车半宽 + 目标半宽（选择层两车半宽真源）+ 计划碰撞余量，
  也就是碰撞观察器的 `x_clear ≥ 0` 那条线。缺额 = 该值 − 当前 |x|。
- **可用时间** T = z / 闭速（`otab` 的 v_close，正=接近；缺失时用相邻拍的 z 差分）。
- **让位耗时** = `lane_move_time(缺额/道宽)` + 反应 0.13 s（与路线紧急门同一套运动学）。
- **可行性** = 让位耗时 ≤ T。逐拍算，报「最早哪一拍开始让还来得及」。
- 方向：往远离目标的一侧让；车道内让位不跨道，故不受相邻车道占用约束。

用法::

    .venv/Scripts/python.exe -X utf8 probe_lane_yield.py <会话>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(REPO))

from maaracing_master.plugins._archive.speedrush.planner import lane_move_time  # noqa: E402
from maaracing_master.plugins._archive.speedrush.route import (  # noqa: E402
    RouteParams, urgent_need_s)

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
LANE_W = 3.5
EGO_HALF = 0.27 * LANE_W          # selection.ego_half_lane
CAR_HALF = 0.27 * LANE_W          # 标称；有框宽投票时用实测 hw
MARGIN = 0.05                     # motion.collision_margin_m
REACT = 0.13


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
    idx = {r["fid"]: i for i, r in enumerate(ticks)}
    p = RouteParams()
    print(f"== {sess}  整道换道门 {urgent_need_s(p):.2f}s"
          f"  标称包络 {EGO_HALF + CAR_HALF + MARGIN:.2f}m")
    for r in ticks:
        for c in (r.get("collisions") or []):
            oid, fid = c["id"], c["fid"]
            i = idx.get(fid)
            if i is None:
                continue
            t0 = ticks[i]["ts_ns"]
            first = None
            for j in range(i, -1, -1):
                if _row(ticks[j], oid) is not None:
                    first = j
                else:
                    break
            lo = next((j for j in range(first if first is not None else i, -1, -1)
                       if ticks[j]["ts_ns"] < t0 - 3.0 * 1e9), 0)
            hw0 = c.get("hw") or CAR_HALF
            print(f"\n  ── id{oid} 落定 fid {c['settle']} 触发 fid {fid}"
                  f"  实测半宽 {hw0}")
            # 收集本段有纵深的拍（对象表行）
            seq = []
            for j in range(lo, i + 1):
                r2 = ticks[j]
                o = _row(r2, oid)
                if o is None or o[4] is None:
                    continue
                seq.append((r2["ts_ns"], o))
            # 闭速：优先用 trace 的 v_close；缺失时用锚点纵深最近 0.4 s 的线性斜率
            met = [(ts, o) for ts, o in seq
                   if len(o) >= 12 and o[9] == "metric" and o[4] is not None]
            cl = {}
            for k, (ts, o) in enumerate(met):
                v = o[11]
                if v is None:
                    win = [(t, oo[4]) for t, oo in met
                           if ts - 0.4e9 <= t <= ts]
                    if len(win) >= 3:
                        tm = sum(t for t, _ in win) / len(win)
                        zm = sum(z for _, z in win) / len(win)
                        num = sum((t - tm) * (z - zm) for t, z in win)
                        den = sum((t - tm) ** 2 for t, _ in win)
                        if den > 1e12:
                            v = -num / den * 1e9      # dz/dt 取反 = 接近速度
                cl[ts] = v
            rows = []
            for ts, o in seq:
                if len(o) < 12 or o[9] != "metric":
                    continue          # 只用锚点米制，粗距（框高）另算，噪声太大
                x = o[3]
                if x is None or o[4] <= 0.3:
                    continue
                z = o[4]
                v = cl.get(ts)
                need_m = EGO_HALF + hw0 + MARGIN
                deficit = max(0.0, need_m - abs(x))
                T = None if not v or v <= 0.5 else z / v
                mv = lane_move_time(deficit / LANE_W, k_lat_lane_s=p.k_lat_lane_s,
                                    tau_heading_s=p.tau_heading_s,
                                    heading_max_rad=p.heading_max_rad,
                                    settle_lane=0.02)
                rows.append(((ts - t0) / 1e9, x, z, v, deficit, T, mv + REACT))
            # 打印：首检、缺额最小、最后一拍
            if not rows:
                print("     没有可用米制拍")
                continue
            for tag, row in (("首拍", rows[0]), ("末拍", rows[-1])):
                rel, x, z, v, deficit, T, need = row
                ok = (T is not None and need <= T)
                print(f"     {tag} {rel:+.2f}s x={x:+.2f} z={z:.1f}"
                      f" 闭速={None if v is None else round(v, 1)}"
                      f" 缺额={deficit:.2f}m 需时={need:.2f}s"
                      f" 可用={None if T is None else round(T, 2)}s"
                      f" → {'来得及' if ok else '来不及'}")
            # 最早来得及的拍
            best = None
            for row in rows:
                rel, x, z, v, deficit, T, need = row
                if T is not None and need <= T:
                    best = row
                    break
            if best is None:
                print("     全程没有任何一拍来得及（含首检）")
            else:
                rel, x, z, v, deficit, T, need = best
                print(f"     最早来得及：{rel:+.2f}s x={x:+.2f} z={z:.1f}"
                      f" 缺额={deficit:.2f}m 需时={need:.2f}s 可用={T:.2f}s")
                # 这之后缺额是否继续变大
                later = [r for r in rows if r[0] >= rel]
                print(f"     该点之后缺额 {min(r[4] for r in later):.2f}"
                      f"~{max(r[4] for r in later):.2f}m"
                      f"  可用时间缩到 {min((r[5] or 0) for r in later):.2f}s")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
