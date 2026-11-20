"""横移限速离线复算（speedrush）。

**问什么**：侧方 3 米内有车、且前方 12 米内时，把自车横速压到 0.3 m/s，能不能让开
三次真碰撞的包络？代价是要多花多少时间。

口径：

- **激活条件**：肇事车米制横向 |x| ≤ 3.0 米且纵深 0 ≤ z ≤ 12 米（贴上来之前的窗口）。
- **反事实**：保持车的世界横位不变，只替换自车横移——用 trace 的 `v_lat` 积分，
  实际位移 vs 限速后位移，差值回加到肇事车相对横位上：
  `x_cap(t) = x_actual(t) + (Δ自车实际 − Δ自车限速)`。
- **清障判据**：`|x_cap| ≥ 自车半宽 + 目标半宽 + 碰撞余量`（与碰撞观察器同一条线）。
- **代价**：限速窗口内被压缩的横移量，以及按 0.3 m/s 补回这些量需要的时间。

用法::

    .venv/Scripts/python.exe -X utf8 probe_lateral_cap.py <会话 ...>
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
LANE_W = 3.5
EGO_HALF = 0.27 * LANE_W
MARGIN = 0.05
CAP = 0.3                 # m/s（trace 的 v_lat 单位是道/秒，积分时换算）
LAT_M, Z_LO, Z_HI = 3.0, 0.0, 12.0


def _set(cap: float, z_hi: float) -> None:
    global CAP, Z_HI
    CAP, Z_HI = cap, z_hi


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
    print(f"== {sess}  限速 {CAP} m/s  激活 |x|≤{LAT_M}m 且 z≤{Z_HI:.0f}m")
    for r in ticks:
        for c in (r.get("collisions") or []):
            oid, fid = c["id"], c["fid"]
            i = idx.get(fid)
            if i is None:
                continue
            hw = c.get("hw") or 0.9
            need = EGO_HALF + hw + MARGIN
            t0 = ticks[i]["ts_ns"]
            # 找激活起点
            start = None
            for j in range(i, -1, -1):
                o = _row(ticks[j], oid)
                if not o or o[3] is None or o[4] is None:
                    continue
                if abs(o[3]) <= LAT_M and Z_LO <= o[4] <= Z_HI:
                    start = j
                elif start is not None:
                    break
            if start is None:
                print(f"   id{oid}: 触发前没有进入激活窗")
                continue
            # 从激活起点积分自车横移（v_lat 是道/秒，乘道宽换米）
            cap_lane = CAP / LANE_W
            d_act = d_cap = 0.0
            prev = None
            min_clear_act = min_clear_cap = 9.9
            for j in range(start, i + 1):
                rj = ticks[j]
                v = rj.get("v_lat") or 0.0
                if prev is not None:
                    dt = (rj["ts_ns"] - prev) / 1e9
                    d_act += v * LANE_W * dt
                    d_cap += max(-cap_lane, min(cap_lane, v)) * LANE_W * dt
                prev = rj["ts_ns"]
                o = _row(rj, oid)
                if o and o[3] is not None and o[4] is not None and o[4] <= 5.0:
                    min_clear_act = min(min_clear_act, abs(o[3]) - need)
                    min_clear_cap = min(min_clear_cap,
                                        abs(o[3] + (d_act - d_cap)) - need)
            dur = (t0 - ticks[start]["ts_ns"]) / 1e9
            o_end = _row(ticks[i], oid)
            end_act = end_cap = None
            if o_end and o_end[3] is not None:
                end_act = abs(o_end[3]) - need
                end_cap = abs(o_end[3] + (d_act - d_cap)) - need
            print(f"   id{oid}: 激活 {dur:.2f}s 内 实际横移 {d_act:+.2f}m"
                  f" 限速后 {d_cap:+.2f}m（压缩 {d_act - d_cap:+.2f}m）")
            print(f"      贴身处(z≤5m)包络余量 实际 {min_clear_act:+.2f}m"
                  f"  限速后 {min_clear_cap:+.2f}m"
                  f" → {'清障' if min_clear_cap >= 0 else '仍重叠'}")
            if end_act is not None:
                print(f"      触发拍余量 实际 {end_act:+.2f}m"
                      f"  限速后 {end_cap:+.2f}m")
            if d_act - d_cap != 0:
                print(f"      补回被压掉的横移需 "
                      f"{abs(d_act - d_cap) / CAP:.1f}s（按 {CAP} m/s）")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--sweep" in sys.argv:
        for cap in (0.3, 0.6):
            for z in (12.0, 25.0, 40.0):
                _set(cap, z)
                for s in args:
                    analyze(s)
    else:
        for s in args:
            analyze(s)
