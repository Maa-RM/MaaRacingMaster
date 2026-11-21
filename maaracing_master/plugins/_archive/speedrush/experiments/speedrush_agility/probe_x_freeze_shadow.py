"""近场横位滤波影子复算（speedrush，离线）。

**问什么**：贴边目标在贴身前 0.5 s 内翻面，是「看到车横位跳了」造成的。给
米制车的横位加一道输入滤波，能不能把翻面压下来，同时不把贴边线推远？
本探针在录制的 trace 上做**影子复算**：只改「选择层看到的车横位」，自车
轨迹仍取录制值——它只回答「目标是否还在追噪声」，不回答「改了会不会撞」。

三个影子：

- `live`：不过滤，基线。
- `hold`：与冻结值差 >0.5 道时保持旧值，同一新位置（±0.15 道）连续 3 拍
  才改。会把连续漂移切成台阶。
- `slew N`：逐拍横位变化限幅 N 道（真车道变化约 0.03 道/拍，N=0.05 仍能
  跟住真变道，只截断 1 道级跳变）。

产出（三种影子并排）：

1. 米制车近场（z≤40 m 或 t≤1.5 s）相邻拍横位跳 >1 道的次数。
2. 同车贴边目标翻面（相邻 pass/graze 拍 |Δtarget|>0.5 道）次数。
   冻结后的目标按 ``xf + (target − x_live)`` 重建，保留原选择侧与偏移量。
3. 冻结改侧拍数（原侧 vs 冻结侧的符号不一致）。
4. 近距候选拍的横距分布（可见框、zg≤5 m、横向≤0.54 道）。

局限：目标重建假设选择侧与偏移量不变；侧别真正重算需要整套选择层重放。
本探针只做筛选，不作验收。

用法::

    .venv/Scripts/python.exe -X utf8 probe_x_freeze_shadow.py <会话 ...>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402

LANE_W = 3.5
Y_H = 354.2
FOCAL = 807.7
ACCEPT_LANE = 0.5           # hold：新读数与冻结值差 ≤ 半道直接接受
CONFIRM_N = 3               # hold：大跳变需连续确认拍数
CONFIRM_TOL_LANE = 0.15


class Live:
    def step(self, key, x: float, ts_ns: int) -> float:
        return x


class Hold:
    """保持-确认：大跳变先保持旧值，连续确认后一次跳过去。"""

    def __init__(self) -> None:
        self.xf: dict = {}
        self.pend: dict = {}

    def step(self, key, x: float, ts_ns: int) -> float:
        xf = self.xf.get(key)
        if xf is None or abs(x - xf) <= ACCEPT_LANE:
            self.xf[key] = x
            self.pend.pop(key, None)
            return x
        seq = self.pend.get(key)
        if seq and abs(x - seq[0]) <= CONFIRM_TOL_LANE:
            seq = (seq[0], seq[1] + 1)
        else:
            seq = (x, 1)
        self.pend[key] = seq
        if seq[1] >= CONFIRM_N:
            self.xf[key] = x
            self.pend.pop(key, None)
        return self.xf[key]


class Slew:
    """逐拍变化限幅；不保持-跳变，平滑跟随。"""

    def __init__(self, max_lane: float) -> None:
        self.max_lane = max_lane
        self.xf: dict = {}

    def step(self, key, x: float, ts_ns: int) -> float:
        xf = self.xf.get(key)
        if xf is None:
            xf = x
        else:
            d = x - xf
            xf = xf + max(-self.max_lane, min(self.max_lane, d))
        self.xf[key] = xf
        return xf


class IdHold:
    """身份错挂作废：同纵深下横位大跳（>1 道且 |Δz|≤1 m）先保持旧值，
    连续 hold_n 拍仍在同一新位置才接受。平滑漂移与真变道不触发。"""

    def __init__(self, hold_n: int = 5) -> None:
        self.hold_n = hold_n
        self.xf: dict = {}
        self.pend: dict = {}
        self.pz: dict = {}

    def step(self, key, x: float, ts_ns: int, z: float | None = None) -> float:
        xf = self.xf.get(key)
        pz = self.pz.get(key)
        self.pz[key] = z
        if xf is None:
            self.xf[key] = x
            return x
        same_z = (pz is not None and z is not None and abs(z - pz) <= 1.0)
        if not same_z or abs(x - xf) <= 1.0:
            self.xf[key] = x
            self.pend.pop(key, None)
            return x
        seq = self.pend.get(key)
        if seq and abs(x - seq[0]) <= CONFIRM_TOL_LANE:
            seq = (seq[0], seq[1] + 1)
        else:
            seq = (x, 1)
        self.pend[key] = seq
        if seq[1] >= self.hold_n:
            self.xf[key] = x
            self.pend.pop(key, None)
        return self.xf[key]


def _near(z, t) -> bool:
    return (z is not None and z <= 40.0) or (t is not None and t <= 1.5)


def _run(ticks, filt) -> dict:
    prev_x: dict = {}
    prev_tgt: dict = {}
    jumps = jumps_f = flips = flips_f = side = 0
    near = []
    for r in ticks:
        run = r.get("run_id")
        ts = r["ts_ns"]
        ego = (r.get("scene") or {}).get("ego_m")
        ego_l = None if ego is None else ego / LANE_W
        pos = {d["id"]: d for d in (r.get("car_pos") or [])}
        hh = {d["id"]: d for d in (r.get("car_h") or [])}
        cam_h = r.get("dgeo_cam_h")
        live_x, filt_x = {}, {}
        for o in (r.get("scene") or {}).get("objs") or []:
            if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
                continue
            oid, x, t, z, src = o[14], o[1] / LANE_W, o[2], o[7], o[6]
            if src != "metric":
                continue
            key = (run, oid)
            xf = filt.step(key, x, ts, z) if isinstance(filt, IdHold) else filt.step(key, x, ts)
            live_x[oid] = x
            filt_x[oid] = xf
            prev = prev_x.get(key)
            if prev is not None and _near(z, t):
                jumps += 1
                if abs(x - prev[0]) > 1.0:
                    jumps_f += 1
                if abs(xf - prev[1]) > 1.0:
                    filt.xj = getattr(filt, "xj", 0) + 1
            prev_x[key] = (x, xf)
        # 目标翻面（同车）
        sd = r.get("sel_dbg") or {}
        if r.get("sel_why") in ("pass", "graze") \
                and sd.get("target") is not None and sd.get("hit_oid") is not None:
            oid = sd["hit_oid"]
            if oid in live_x and oid in filt_x:
                tgt = sd["target"]
                tgt_f = filt_x[oid] + (tgt - live_x[oid])
                last = prev_tgt.get((run, oid))
                if last is not None:
                    if abs(tgt - last[0]) > 0.5:
                        flips += 1
                    if abs(tgt_f - last[1]) > 0.5:
                        flips_f += 1
                prev_tgt[(run, oid)] = (tgt, tgt_f)
                if (1.0 if tgt >= live_x[oid] else -1.0) != \
                        (1.0 if tgt >= filt_x[oid] else -1.0):
                    side += 1
        # 近距候选拍
        if ego_l is not None:
            for o in (r.get("scene") or {}).get("objs") or []:
                if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
                    continue
                oid = o[14]
                if oid not in filt_x:
                    continue
                cy = (pos.get(oid) or {}).get("cy")
                h = (hh.get(oid) or {}).get("h")
                if None in (cy, h) or not (cam_h and 1.0 <= cam_h <= 1.6):
                    continue
                cb = cy + h / 2.0
                if cb - Y_H < 20:
                    continue
                if not (0 <= cb - h and cb <= 720 and 60 <= h <= 500):
                    continue
                zg = FOCAL * cam_h / (cb - Y_H)
                if zg <= 5.0 and abs(live_x[oid] - ego_l) <= 0.54:
                    near.append((abs(live_x[oid] - ego_l),
                                 abs(filt_x[oid] - ego_l)))
    return {"jumps": jumps, "jumps_f": jumps_f, "flips": flips,
            "flips_f": flips_f, "side": side, "near": near,
            "xj": getattr(filt, "xj", 0)}


def _run_tgt_hold(ticks) -> dict:
    """命中车读数无效时保持上一贴边目标（不是保持车 x）。

    无效 = 该车本拍缺读数，或横位相邻拍跳 >0.5 道，或纵深跳 >5 m/断供。
    这直接对应生产里的一个空档：`_graze_hold` 只在选择层没认领时续住，
    raw.why 一直是 pass/graze 时，目标会照着坏读数重算。
    """
    prev_read: dict = {}      # (run, oid) -> (x, z)
    held: dict = {}           # run -> 目标
    flips_live = flips_held = active = active_near = 0
    diff_ticks = 0
    prev_live_tgt = {}
    for r in ticks:
        run = r.get("run_id")
        sd = r.get("sel_dbg") or {}
        if r.get("sel_why") not in ("pass", "graze")                 or sd.get("target") is None or sd.get("hit_oid") is None:
            continue
        oid = sd["hit_oid"]
        car = next((o for o in (r.get("scene") or {}).get("objs") or []
                    if o and len(o) > 14 and o[14] == oid and o[1] is not None),
                   None)
        x = None if car is None else car[1] / LANE_W
        z = None if car is None else car[7]
        t = None if car is None else car[2]
        pr = prev_read.get((run, oid))
        invalid = car is None or (pr is not None and (
            (x is not None and pr[0] is not None and abs(x - pr[0]) > 0.5)
            or (pr[1] is not None and z is None)
            or (pr[1] is not None and z is not None
                and abs(z - pr[1]) > 5.0)))
        prev_read[(run, oid)] = (x, z)
        live = sd["target"]
        h = held.get(run)
        if invalid and h is not None:
            use = h
            active += 1
            if t is not None and t < 1.0:
                active_near += 1
        else:
            use = live
            held[run] = live
        last = prev_live_tgt.get((run, oid))
        if last is not None:
            if abs(live - last) > 0.5:
                flips_live += 1
            if abs(use - last) > 0.5:
                flips_held += 1
        if abs(use - live) > 0.5:
            diff_ticks += 1
        prev_live_tgt[(run, oid)] = live
    return {"flips_live": flips_live, "flips_held": flips_held,
            "active": active, "active_near": active_near,
            "diff": diff_ticks}


def analyze(sess: str) -> None:
    ticks, stage_s = P.load(sess)
    print(f"== {sess}  阶段内 {stage_s:.0f}s")
    th = _run_tgt_hold(ticks)
    print(f"   目标保持（读数无效时）: 翻面 {th['flips_held']}/{th['flips_live']}"
          f"  生效 {th['active']} 拍（t<1 s 内 {th['active_near']}）"
          f"  与 live 差 >0.5 道 {th['diff']} 拍")
    for name, filt in (("live", Live()), ("hold", Hold()),
                       ("idhold3", IdHold(3)), ("idhold5", IdHold(5)),
                       ("slew .15", Slew(0.15))):
        st = _run(ticks, filt)
        lv = sorted(a[0] for a in st["near"])
        fz = sorted(a[1] for a in st["near"])
        med_l = (lv[len(lv) // 2] if lv else float("nan"))
        med_f = (fz[len(fz) // 2] if fz else float("nan"))
        print(f"   {name:9s} 近场x跳>1道 {st['xj']:3d}/{st['jumps']:<4d}"
              f" 目标翻面 {st['flips_f']:3d}/{st['flips']:<3d}"
              f" 改侧 {st['side']:2d} 拍"
              f" 近距中位 {med_l:.2f}→{med_f:.2f}"
              f" 最小 {(lv[0] if lv else float('nan')):.2f}→"
              f"{(fz[0] if fz else float('nan')):.2f}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
