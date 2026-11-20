"""并线杆值曲线离线台架（speedrush）。

**问什么**：并线承诺期该用多大杆？现状的控制律在 0.6~0.7 道误差上只给 0.18~0.23 杆，
一道要走近 2 秒；而判断带里只剩 0.8~1.1 秒。本台架比较几种杆值曲线在同一个
车辆模型下的「到位时间 / 超调 / 0.8 秒能走多远」。

车辆模型（辨识自两局最小二乘，R²=0.94，出处见 policy 的 planner 注记）::

    ψ(k+1) = 1.01·ψ(k) + 0.023·u(k−4)     # 50ms 拍，满杆 ≈0.46 rad/s、滞后 0.2s
    x' = v_ref·tanψ / lane_w               # 车道/秒

只用这一个模型、不引入新参数；滞后与增益各做 ±30% 敏感性。

曲线（半拍一算，全部走闭环：误差按 x + v_lat·0.3 外推）：
- now     ：现状律 u = k_psi·(ψ_des − ψ) − k_r·r，ψ_des = atan(k_lat·e·LW/v_ref)
- sustain ：承诺分支的 sustain 解 u = k_psi·ψ_des（不带车头角/角速度反馈）
- prop_g  ：比例杆 u = g·e（g=1.0/1.5/2.0），上限 maneuver_stick_max
- pulse   ：高挡到残差 <0.35 道，再回落到 sustain

用法::

    .venv/Scripts/python.exe -X utf8 bench_merge_stick.py
"""

from __future__ import annotations

import math

V_REF = 41.0
LANE_W = 3.5
K_LAT = 1.2
K_PSI = 6.0
K_R = 2.0
TAU_LOOK = 0.3
STICK_MAX = 0.85
HEADING_MAX = 0.15
DT = 0.05
LAG_TICKS = 4           # 0.2 s
YAW_PER_STICK = 0.023   # 每拍每单位杆的偏航增量（辨识值）


def v_lat_of(psi: float) -> float:
    return V_REF * math.tan(max(-HEADING_MAX, min(HEADING_MAX, psi))) / LANE_W


def _psi_des(e: float) -> float:
    return max(-HEADING_MAX, min(HEADING_MAX,
                                 math.atan(K_LAT * e * LANE_W / V_REF)))


def _now(e: float, psi: float, r: float) -> float:
    u = K_PSI * (_psi_des(e) - psi) - K_R * r
    return max(-STICK_MAX, min(STICK_MAX, u))


def _sustain(e: float, psi: float, r: float) -> float:
    u = K_PSI * _psi_des(e)
    return max(-STICK_MAX, min(STICK_MAX, u))


def _prop(g: float):
    def f(e: float, psi: float, r: float) -> float:
        return max(-STICK_MAX, min(STICK_MAX, g * e))
    return f


def _pulse(e: float, psi: float, r: float) -> float:
    if abs(e) > 0.35:
        return STICK_MAX if e > 0 else -STICK_MAX
    return _sustain(e, psi, r)


def run(rule, err0: float, *, t_max: float = 2.0,
        lag: int = LAG_TICKS, yaw: float = YAW_PER_STICK):
    """从静止朝向、给定初始误差跑闭环，返 (到位时间, 最大超调, 0.8s 位移)。"""
    x, psi, r = err0, 0.0, 0.0
    hist = [0.0] * lag
    t, settle, over, d08 = 0.0, None, 0.0, None
    while t < t_max - 1e-9:
        e = x - TAU_LOOK * v_lat_of(psi)     # 剩多少要补（e>0 = 还差这么多）
        u = rule(e, psi, r)
        # 下发链有 0.2 s 滞后：本拍发的是队列里 lag 拍前的杆
        u_eff = hist.pop(0)
        hist.append(u)
        psi_new = max(-HEADING_MAX, min(HEADING_MAX,
                                        1.01 * psi + yaw * u_eff))
        r = (psi_new - psi) / DT
        psi = psi_new
        x -= v_lat_of(psi) * DT
        t += DT
        over = max(over, -x)
        if settle is None and abs(x) <= 0.10:
            settle = t
        if d08 is None and t >= 0.8 - 1e-9:
            d08 = abs(err0 - x)
    return settle, over, d08


def _now_k(klat: float, tau_look: float = TAU_LOOK):
    """现状律只换闭环带宽 k_lat（位置误差→期望横速的增益）与外推时长。"""
    def f(e0: float, psi: float, r: float) -> float:
        v = v_lat_of(psi)
        e = e0 - tau_look * v
        psi_des = max(-HEADING_MAX, min(HEADING_MAX,
                                       math.atan(klat * e * LANE_W / V_REF)))
        u = K_PSI * (psi_des - psi) - K_R * r
        return max(-STICK_MAX, min(STICK_MAX, u))
    return f


def _run_err(rule, err0: float, *, t_max: float = 2.0,
             lag: int = LAG_TICKS, yaw: float = YAW_PER_STICK):
    """同 run()，但把「外推修正」交给 rule 自己，误差用真实剩余量。"""
    x, psi, r = err0, 0.0, 0.0
    hist = [0.0] * lag
    t, settle, over, d08 = 0.0, None, 0.0, None
    while t < t_max - 1e-9:
        u = rule(x, psi, r)
        u_eff = hist.pop(0)
        hist.append(u)
        psi_new = max(-HEADING_MAX, min(HEADING_MAX, 1.01 * psi + yaw * u_eff))
        r = (psi_new - psi) / DT
        psi = psi_new
        x -= v_lat_of(psi) * DT
        t += DT
        over = max(over, -x)
        if settle is None and abs(x) <= 0.10:
            settle = t
        if d08 is None and t >= 0.8 - 1e-9:
            d08 = abs(err0 - x)
    return settle, over, d08


def main() -> None:
    rules = [("现状律", _now), ("sustain", _sustain),
             ("比例 1.0", _prop(1.0)), ("比例 1.5", _prop(1.5)),
             ("比例 2.0", _prop(2.0)), ("高挡脉冲", _pulse)]
    print("模型：满杆 0.46 rad/s、滞后 0.2 s、ψ 限 0.15 rad；目标=收进 0.10 道")
    print(f"{'曲线':<10}{'0.5道 到位':>12}{'0.8道 到位':>12}"
          f"{'0.8s位移(0.8道)':>18}{'超调(0.8道)':>14}")
    for name, rule in rules:
        a = run(rule, 0.5)
        b = run(rule, 0.8)
        print(f"{name:<10}"
              f"{'—' if a[0] is None else f'{a[0]:.2f}s':>12}"
              f"{'—' if b[0] is None else f'{b[0]:.2f}s':>12}"
              f"{b[2]:>18.2f}"
              f"{b[1]:>14.2f}")
    print("\n敏感性（比例 1.5）：")
    for dlag, dyaw in ((-1, 0.7), (0, 1.0), (1, 0.7), (0, 1.3), (2, 1.3)):
        b = run(_prop(1.5), 0.8, lag=max(1, LAG_TICKS + dlag),
                yaw=YAW_PER_STICK * dyaw)
        print(f"  滞后 {(LAG_TICKS + dlag) * DT:.2f}s、增益 ×{dyaw:.1f}: "
              f"到位 {'—' if b[0] is None else f'{b[0]:.2f}s'}，"
              f"0.8s 位移 {b[2]:.2f} 道，超调 {b[1]:.2f} 道")

    print("\n闭环带宽扫描（现状律只改 k_lat 与外推时长，误差 0.8 道）：")
    print(f"{'k_lat':>6}{'外推':>7}{'到位':>8}{'0.8s位移':>11}{'超调':>8}")
    for klat in (1.2, 1.8, 2.4, 3.0):
        for look in (0.3, 0.45, 0.6):
            s, o, d = _run_err(_now_k(klat, look), 0.8)
            print(f"{klat:>6.1f}{look:>7.2f}"
                  f"{'—' if s is None else f'{s:.2f}s':>8}"
                  f"{d:>11.2f}{o:>8.2f}")


if __name__ == "__main__":
    main()
