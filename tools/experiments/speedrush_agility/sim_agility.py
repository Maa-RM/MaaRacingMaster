#!/usr/bin/env python
"""机动时间尺度闭环仿真（speedrush 执行律候选扫描，带延迟完整台架）。

**问什么**：0.68 道贴边 / 1 道换道，各控制律在**含杆环延迟与观测延迟**的模型下
要多久、超调多大、稳态是否抖。只读；模型自包含，不 import 业务代码。

**为什么不用无延迟简化模型**：2026-10-30 的历史坑——首版台架把车模写成"杆→车头角"，
增益定了 3~5 倍，实车出现 ±0.6 道、周期 2.5 s 的极限环（"打醉拳"）。延迟是稳定性
的决定项，必须建模。

**模型**（照 decision.json `_sources` 的实车辨识口径）：
- 车辆：ψ(t+1) = 1.01·ψ(t) + 0.023·u(t−0.2s)（满杆 ≈0.46 rad/s，R²=0.94）
- 观测：0.15 s 延迟；角度噪声 0.011 rad、位置噪声 0.05 道
- 控制：u = k_psi·(ψ_des − (ψ + lead·r)) − k_r·r，限幅 ±1；
  ψ_des = clamp(atan(k_lat·e·LANE_W/v_ref), ±heading_max)
"""

from __future__ import annotations

import math
import random

LANE_W = 3.5
V_REF = 41.0
DT = 0.05                # 20 Hz 控制拍
TAU_VEH_LAG_S = 0.2      # 杆→偏航率滞后
OBS_LAG_S = 0.15         # 观测链路延迟
HEADING_MAX = 0.15
K_PSI = 6.0
K_R = 2.0
LEAD = 0.1
ANGLE_NOISE = 0.011
POS_NOISE = 0.05
SETTLE_LANE = 0.1


def simulate(target: float, k_lat: float, *, seed: int = 0,
             t_max: float = 4.0, noise: bool = True) -> dict:
    rng = random.Random(seed)
    steps = int(t_max / DT)
    lag_steps = max(1, int(round(TAU_VEH_LAG_S / DT)))
    obs_steps = max(1, int(round(OBS_LAG_S / DT)))
    u_hist = [0.0] * lag_steps
    hist: list[tuple[float, float]] = [(0.0, 0.0)] * obs_steps   # (x, psi)
    x, psi = 0.0, 0.0
    r_f = 0.0
    settle_t = None
    peak = 0.0
    overshoot = 0.0
    tail_min = tail_max = 0.0
    trace = []
    for n in range(steps):
        xo, po = hist[0]
        if noise:
            xo += rng.gauss(0.0, POS_NOISE)
            po += rng.gauss(0.0, ANGLE_NOISE)
        e = target - xo
        psi_des = max(-HEADING_MAX, min(HEADING_MAX,
                                        math.atan(k_lat * e * LANE_W / V_REF)))
        prev_po = hist[1][1] if len(hist) > 1 else po
        # 角速度低通（同实现的 yaw_rate 低通口径，tau≈0.2 s）；hist[1] 比 hist[0] 新
        r_raw = (prev_po - po) / DT
        r_f += (r_raw - r_f) * min(1.0, DT / 0.2)
        u = K_PSI * (psi_des - (po + LEAD * r_f)) - K_R * r_f
        u = max(-1.0, min(1.0, u))
        u_eff = u_hist.pop(0)
        u_hist.append(u)
        psi = 1.01 * psi + 0.023 * u_eff
        x += V_REF / LANE_W * math.tan(psi) * DT
        hist.append((x, psi))
        hist.pop(0)
        trace.append((n * DT, x, psi, u))
        peak = max(peak, x)
        if x > target:
            overshoot = max(overshoot, x - target)
        if settle_t is None and abs(target - x) <= SETTLE_LANE:
            settle_t = (n + 1) * DT
        if n * DT >= t_max - 1.0:
            tail_min = min(tail_min, x - target)
            tail_max = max(tail_max, x - target)
    return {"settle_s": settle_t, "overshoot_lane": overshoot,
            "peak_lane": peak, "tail_pp_lane": tail_max - tail_min,
            "x_at_1s": next((p[1] for p in trace if p[0] >= 1.0 - 1e-9), None),
            "trace": trace}


def simulate_pulse_train(target: float, *, pulse_s: float = 0.18,
                         gap_s: float = 0.20, u: float = 1.0,
                         t_max: float = 6.0) -> dict:
    """人工式开环满杆脉冲：u=1 保持 pulse_s、松开 gap_s，循环到 t_max。

    同一套车模与杆环延迟；用来对照「连续闭环律」和「短脉冲串」到达同一
    0.68 道目标的到位时间、超调、尾段摆动与满杆占比。"""
    steps = int(t_max / DT)
    lag_steps = max(1, int(round(TAU_VEH_LAG_S / DT)))
    u_hist = [0.0] * lag_steps
    x, psi = 0.0, 0.0
    settle_t = None
    peak = 0.0
    overshoot = 0.0
    tail_min = tail_max = 0.0
    full = 0
    pulses = 0
    in_pulse = False
    period = pulse_s + gap_s
    trace = []
    for n in range(steps):
        t = n * DT
        phase = t % period
        on = phase < pulse_s
        if on and not in_pulse:
            pulses += 1
        in_pulse = on
        u_cmd = u if on else 0.0
        full += int(on)
        u_eff = u_hist.pop(0)
        u_hist.append(u_cmd)
        psi = 1.01 * psi + 0.023 * u_eff
        x += V_REF / LANE_W * math.tan(psi) * DT
        trace.append((t, x, psi, u_cmd))
        peak = max(peak, x)
        if x > target:
            overshoot = max(overshoot, x - target)
        if settle_t is None and abs(target - x) <= SETTLE_LANE:
            settle_t = (n + 1) * DT
        if t >= t_max - 1.0:
            tail_min = min(tail_min, x - target)
            tail_max = max(tail_max, x - target)
    return {"settle_s": settle_t, "overshoot_lane": overshoot,
            "peak_lane": peak, "tail_pp_lane": tail_max - tail_min,
            "full_stick_frac": full / steps, "pulses": pulses,
            "x_at_1s": next((p[1] for p in trace if p[0] >= 1.0 - 1e-9), None),
            "trace": trace}


def simulate_pulse_closed_loop(target: float, *, pulse_s: float = 0.18,
                               gap_s: float = 0.15, t_max: float = 4.0
                               ) -> dict:
    """带回授的短脉冲：每拍看误差与横向速度决定「打满 pulse_s」还是
    「松开 gap_s」，不是固定周期振荡。人工的满舵段 0.10~0.12s、松开
    0.28~0.50s，就是这个结构。"""
    steps = int(t_max / DT)
    lag_steps = max(1, int(round(TAU_VEH_LAG_S / DT)))
    pulse_steps = max(1, int(round(pulse_s / DT)))
    gap_steps = max(0, int(round(gap_s / DT)))
    u_hist = [0.0] * lag_steps
    x, psi = 0.0, 0.0
    pulse_left = gap_left = 0
    pulse_sign = 0.0
    settle_t = None
    peak = 0.0
    overshoot = 0.0
    tail_min = tail_max = 0.0
    full = 0
    pulses = 0
    trace = []
    for n in range(steps):
        t = n * DT
        e = target - x
        v_lat = V_REF * math.tan(psi) / LANE_W
        u = 0.0
        if pulse_left > 0:
            u = pulse_sign
            pulse_left -= 1
            if pulse_left == 0:
                gap_left = gap_steps
        elif gap_left > 0:
            gap_left -= 1
        elif abs(e) > SETTLE_LANE and e * v_lat <= 0.0:
            pulse_sign = 1.0 if e > 0 else -1.0
            pulse_left = pulse_steps - 1
            u = pulse_sign
            pulses += 1
        full += int(u != 0.0)
        u_eff = u_hist.pop(0)
        u_hist.append(u)
        psi = 1.01 * psi + 0.023 * u_eff
        x += V_REF / LANE_W * math.tan(psi) * DT
        trace.append((t, x, psi, u))
        peak = max(peak, x)
        if x > target:
            overshoot = max(overshoot, x - target)
        if settle_t is None and abs(target - x) <= SETTLE_LANE:
            settle_t = (n + 1) * DT
        if t >= t_max - 1.0:
            tail_min = min(tail_min, x - target)
            tail_max = max(tail_max, x - target)
    return {"settle_s": settle_t, "overshoot_lane": overshoot,
            "peak_lane": peak, "tail_pp_lane": tail_max - tail_min,
            "full_stick_frac": full / steps, "pulses": pulses,
            "x_at_1s": next((p[1] for p in trace if p[0] >= 1.0 - 1e-9), None),
            "trace": trace}


def fmt(x, w=7, d=2):
    return f"{'—':>{w}}" if x is None else f"{x:>{w}.{d}f}"


def main() -> None:
    print("== 复现历史台架（k_lat=1.2，一道阶跃：目标 1 秒走 0.73 道、峰值 1.06）")
    r = simulate(1.0, 1.2)
    print(f"   1s 位移 {fmt(r['x_at_1s'], 6, 3)} 道 / 峰值 {r['peak_lane']:.3f} 道"
          f" / 收敛(0.1 道) {fmt(r['settle_s'])} s / 尾段峰峰 {r['tail_pp_lane']:.3f}")
    print()
    print("== 0.68 道贴边，扫描带宽（含延迟与噪声，3 个种子取中位）")
    print(" k_lat  到位(s)   超调(道)  尾段峰峰(道)  峰值(道)  1s 位移(道)")
    for k in (1.2, 1.6, 2.0, 2.5, 3.0):
        runs = [simulate(0.68, k, seed=s) for s in (0, 1, 2)]
        st = sorted(r["settle_s"] for r in runs if r["settle_s"] is not None)
        med = st[len(st) // 2] if st else None
        ov = sum(r["overshoot_lane"] for r in runs) / len(runs)
        tp = sum(r["tail_pp_lane"] for r in runs) / len(runs)
        pk = sum(r["peak_lane"] for r in runs) / len(runs)
        x1 = sum(r["x_at_1s"] for r in runs) / len(runs)
        print(f"{k:>6.1f}  {fmt(med)}  {ov:>8.3f}  {tp:>12.3f}  {pk:>8.3f}  {x1:>11.3f}")
    print()
    print("== 1 道换道")
    print(" k_lat  到位(s)   超调(道)  尾段峰峰(道)")
    for k in (1.2, 1.6, 2.0, 2.5):
        runs = [simulate(1.0, k, seed=s) for s in (0, 1, 2)]
        st = sorted(r["settle_s"] for r in runs if r["settle_s"] is not None)
        med = st[len(st) // 2] if st else None
        ov = sum(r["overshoot_lane"] for r in runs) / len(runs)
        tp = sum(r["tail_pp_lane"] for r in runs) / len(runs)
        print(f"{k:>6.1f}  {fmt(med)}  {ov:>8.3f}  {tp:>12.3f}")
    print()
    print("== 0.68 道贴边：连续律 vs 人工式满杆短脉冲（带 0.2s 杆环延迟）")
    r = simulate(0.68, 1.2)
    print(f"   连续律 k=1.2  : 到位 {fmt(r['settle_s'])} s"
          f" 超调 {r['overshoot_lane']:.3f} 道 尾段 {r['tail_pp_lane']:.3f} 道")
    for ps, gs in ((0.15, 0.20), (0.18, 0.20), (0.18, 0.15)):
        q = simulate_pulse_train(0.68, pulse_s=ps, gap_s=gs)
        print(f"   开环脉冲 {ps:.2f}s/松 {gs:.2f}s : 到位 {fmt(q['settle_s'])} s"
              f" 超调 {q['overshoot_lane']:.3f} 道 尾段 {q['tail_pp_lane']:.3f} 道"
              f" 满杆 {q['full_stick_frac']:.0%} 脉冲 {q['pulses']}")
    print("   —— 带回授（每拍判误差/横速，打满后松开）——")
    for ps, gs in ((0.15, 0.15), (0.18, 0.15), (0.18, 0.20)):
        q = simulate_pulse_closed_loop(0.68, pulse_s=ps, gap_s=gs)
        print(f"   回授脉冲 {ps:.2f}s/松 {gs:.2f}s : 到位 {fmt(q['settle_s'])} s"
              f" 超调 {q['overshoot_lane']:.3f} 道 尾段 {q['tail_pp_lane']:.3f} 道"
              f" 满杆 {q['full_stick_frac']:.0%} 脉冲 {q['pulses']}")


if __name__ == "__main__":
    main()
