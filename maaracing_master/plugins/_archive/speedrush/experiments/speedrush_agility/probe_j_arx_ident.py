# -*- coding: utf-8 -*-
"""P3-A J：有杆段「杆→车道线角速度」的可辨识性原型（离线、只读）。

唯一问题：**不依赖车速，能否从实际下发杆与车道线车头角 θ 的变化率辨识偏航环？**
车主物理在引擎侧、Lua 与 trace 都拿不到速度；θ 的变化率就是相对车道线的偏航角速度，
所以内环辨识不需要速度。

模型（每 50 ms 一拍，段内去均值吸收道路曲率）：
    r[k] = phi * r[k-1] + beta * u[k-d] + c_seg
    r = dθ/dt 由 SG 导数得到；u 为实际下发杆。

预注册判据（先写死，跑完不改）：
    P1 可辨识：留出段一步角速度 RMSE 比 AR(1) 低 ≥10%，且比同参数置换安慰剂低 ≥20%。
    P2 参数稳定：两半数据独立拟合的直流增益差 ≤30%。
    P3 在线可用：递推估计进入终值 ±25% 并保持所需时间 ≤20 s。

口径与边界：
- 人工局的 steer_x 是影子命令不是物理输入，必须用 pads（200 Hz 手柄）。
- 只用有杆段：零杆连续 ≥0.3 s 的样本剔除；游戏辅助在松杆 0.5 s 后接管，会把植物换掉。
- 4 s 分块；段内去均值消除道路曲率与零点；留出段的偏置只用该段前半估。
- 时延 d 用训练段内部的嵌套折选，不由留出段挑。
- 这是可辨识性原型，不接生产、不改行为。

用法：
    .venv\\Scripts\\python.exe probe_j_arx_ident.py all
    .venv\\Scripts\\python.exe probe_j_arx_ident.py batch
    .venv\\Scripts\\python.exe probe_j_arx_ident.py online
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
from scipy.signal import savgol_filter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from probe_b_stick_plant import load, zoh

OUT = os.path.join(HERE, '_out')
SESSIONS = ['20261118_195555', '20261118_225251', '20261119_104138']

H = 0.05          # 控制拍间隔（s）
CHUNK = 80        # 4 s 分块
SG_WIN = 3        # 0.15 s 导数的 SG 窗
D_MAX = 6         # 时延扫描上限（0.30 s）
LAM = 0.995       # 在线 RLS 遗忘因子（约 10 s 记忆）


def session_input(s):
    """人工局的物理输入是手柄；程序局是实际下发杆。"""
    if s.pads_t is not None:
        return s.pads_t + s.t[0], s.pads_u, 'pads'
    return s.t, s.steer_x, 'steer_x'


def build(s, ut, uu, win=SG_WIN):
    """统一栅格上的 θ 角速度、延迟杆、可信掩码与 4 s 分块。"""
    tt, th, va = s.theta_t, s.theta, s.valid
    grid = np.arange(tt.min(), tt.max() + 1e-9, H)
    thg = np.interp(grid, tt, th)
    vag = np.interp(grid, tt, va.astype(float)) > 0.999

    # 插值跨度：角样本离栅格点太远就不信
    j = np.searchsorted(tt, grid)
    d0 = np.where(j > 0, grid - tt[np.clip(j - 1, 0, len(tt) - 1)], 9.0)
    d1 = np.where(j < len(tt), tt[np.clip(j, 0, len(tt) - 1)] - grid, 9.0)
    gap = np.minimum(d0, d1)

    ug = zoh(ut, uu, grid)
    zero = np.abs(ug) < 0.02
    run = np.zeros(len(grid), int)
    for i in range(len(grid)):
        run[i] = run[i - 1] + 1 if zero[i] and i > 0 else (1 if zero[i] else 0)

    ok = vag & (gap < 0.12) & (run < 6)
    r = np.full(len(grid), np.nan)
    st = None
    for i, m in enumerate(list(ok) + [False]):
        if m and st is None:
            st = i
        if (not m) and st is not None:
            if i - st >= win:
                r[st:i] = savgol_filter(thg[st:i], win, 2, deriv=1, delta=H)
            st = None

    segs, st = [], None
    for i, m in enumerate(list(ok) + [False]):
        if m and st is None:
            st = i
        if (not m) and st is not None:
            for a in range(st, i, CHUNK):
                b = min(a + CHUNK - 1, i - 1)
                idx = np.arange(a + 1, b)
                idx = idx[np.isfinite(r[idx]) & np.isfinite(r[idx - 1])]
                if len(idx) >= 20:
                    segs.append(idx)
            st = None
    return grid, r, zoh(ut, uu, grid), ok, segs


def _rows(segs, r, ud, d):
    out = []
    for idx in segs:
        A = np.column_stack([r[idx - 1], ud[idx - d] if d else ud[idx]])
        y = r[idx]
        out.append((A - A.mean(0), y - y.mean()))
    return out


def _fit(rows, two=True):
    P = np.zeros((2, 2))
    b = np.zeros(2)
    for A, y in rows:
        if two:
            P += A.T @ A
            b += A.T @ y
        else:
            P[0, 0] += A[:, 0] @ A[:, 0]
            b[0] += A[:, 0] @ y
    if two:
        return np.linalg.solve(P + 1e-6 * np.eye(2), b)
    return np.array([b[0] / (P[0, 0] + 1e-12), 0.0])


def _holdout_rmse(segs, r, ud, d, coef):
    """留出段：偏置只用该段前半估，误差算后半。"""
    sse = n = 0
    for idx in segs:
        half = len(idx) // 2
        if half < 5:
            continue
        pre, post = idx[:half], idx[half:]
        c = np.mean(r[pre] - coef[0] * r[pre - 1] - coef[1] * ud[pre - d] if d
                    else r[pre] - coef[0] * r[pre - 1] - coef[1] * ud[pre])
        pred = coef[0] * r[post - 1] + coef[1] * (ud[post - d] if d else ud[post]) + c
        e = r[post] - pred
        sse += e @ e
        n += len(e)
    return float(np.sqrt(sse / n)) if n else float('nan')


def _placebo_rows(segs, r, ud, d, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for idx in segs:
        u = ud[idx - d] if d else ud[idx]
        out.append((idx, rng.permutation(u)))
    return out


def _placebo_rmse(segs, r, ud, d, coef, seed=0):
    sse = n = 0
    for idx, ush in _placebo_rows(segs, r, ud, d, seed):
        half = len(idx) // 2
        if half < 5:
            continue
        pre, post = idx[:half], idx[half:]
        uph = ush
        c = np.mean(r[pre] - coef[0] * r[pre - 1] - coef[1] * uph[:half])
        e = r[post] - (coef[0] * r[post - 1] + coef[1] * uph[half:]
                       + c)
        sse += e @ e
        n += len(e)
    return float(np.sqrt(sse / n)) if n else float('nan')


def _pick_delay(train, r, ug):
    """延迟只用训练段内部两折选，不碰评估段。"""
    a = [x for i, x in enumerate(train) if i % 2 == 0]
    b = [x for i, x in enumerate(train) if i % 2 == 1]
    cv = {}
    for d in range(D_MAX + 1):
        cv[d] = float((_holdout_rmse(b, r, ug, d, _fit(_rows(a, r, ug, d)))
                       + _holdout_rmse(a, r, ug, d, _fit(_rows(b, r, ug, d)))) / 2)
    d_best = int(min(cv, key=cv.get))
    return d_best, cv


def batch(sid):
    s = load(sid)
    ut, uu, src = session_input(s)
    grid, r, ug, ok, segs = build(s, ut, uu)
    if len(segs) < 6:
        return {'session': sid, 'error': 'segments too few', 'n_segs': len(segs)}

    # 对称两折：互训互验（每折 10~13 段做评估）
    a = [x for i, x in enumerate(segs) if i % 2 == 0]
    b = [x for i, x in enumerate(segs) if i % 2 == 1]
    folds = []
    for tr, te in ((a, b), (b, a)):
        d_best, cv = _pick_delay(tr, r, ug)
        coef = _fit(_rows(tr, r, ug, d_best))
        ar = _fit(_rows(tr, r, ug, d_best), two=False)
        folds.append({
            'd_best': d_best,
            'cv': cv,
            'coef': coef,
            'hold_rmse': _holdout_rmse(te, r, ug, d_best, coef),
            'ar1_rmse': _holdout_rmse(te, r, ug, d_best, ar),
            'placebo_rmse': _placebo_rmse(te, r, ug, d_best, coef),
            'n_eval': len(te),
        })

    hold_rmse = float(np.mean([f['hold_rmse'] for f in folds]))
    ar_rmse = float(np.mean([f['ar1_rmse'] for f in folds]))
    placebo_rmse = float(np.mean([f['placebo_rmse'] for f in folds]))
    coef = np.mean([f['coef'] for f in folds], axis=0)
    d_best = int(round(float(np.mean([f['d_best'] for f in folds]))))
    gain = coef[1] / (1 - coef[0]) if abs(1 - coef[0]) > 1e-6 else float('inf')
    tau = -H / np.log(coef[0]) if 0 < coef[0] < 1 else float('nan')

    # 分半稳定性：时间前后两半独立拟合（增益漂移的直接指标）
    half = len(segs) // 2
    coef_a = _fit(_rows(segs[:half], r, ug, d_best))
    coef_b = _fit(_rows(segs[half:], r, ug, d_best))
    gain_a = coef_a[1] / (1 - coef_a[0]) if abs(1 - coef_a[0]) > 1e-6 else float('nan')
    gain_b = coef_b[1] / (1 - coef_b[0]) if abs(1 - coef_b[0]) > 1e-6 else float('nan')
    gain_diff = (abs(gain_a - gain_b) / abs(gain)
                 if np.isfinite(gain_a) and np.isfinite(gain_b) and abs(gain) > 1e-9
                 else float('inf'))

    # 物理延迟档（B 线互相关口径 0.2 s）单独报一行
    d_phys = 4
    p_rmse, p_ar, p_gain = [], [], []
    for tr, te in ((a, b), (b, a)):
        cp = _fit(_rows(tr, r, ug, d_phys))
        p_gain.append(cp[1] / (1 - cp[0]) if abs(1 - cp[0]) > 1e-6 else float('nan'))
        p_rmse.append(_holdout_rmse(te, r, ug, d_phys, cp))
        p_ar.append(_holdout_rmse(te, r, ug, d_phys,
                                  _fit(_rows(tr, r, ug, d_phys), two=False)))

    return {
        'session': sid,
        'input': src,
        'n_segs': len(segs),
        'n_eval': int(sum(f['n_eval'] for f in folds)),
        'samples': int(sum(len(x) for x in segs)),
        'excitation': {
            'u_abs_mean': round(float(np.nanmean(np.abs(ug[ok]))), 3),
            'u_std': round(float(np.nanstd(ug[ok])), 3),
            'frac_gt_03': round(float(np.mean(np.abs(ug[ok]) > 0.3)), 3),
        },
        'delay_cv': [[str(d), round(v, 5)] for d, v in folds[0]['cv'].items()],
        'd_best': d_best,
        'phi': round(float(coef[0]), 4),
        'beta': round(float(coef[1]), 4),
        'gain': round(float(gain), 4),
        'tau_s': round(float(tau), 4),
        'hold_rmse': round(hold_rmse, 5),
        'ar1_rmse': round(ar_rmse, 5),
        'placebo_rmse': round(placebo_rmse, 5),
        'impr_vs_ar1': round(1 - hold_rmse / ar_rmse, 4),
        'impr_vs_placebo': round(1 - hold_rmse / placebo_rmse, 4),
        'gain_a': round(float(gain_a), 4),
        'gain_b': round(float(gain_b), 4),
        'gain_diff': round(float(gain_diff), 4),
        'phys_d4': {
            'gain': round(float(np.nanmean(p_gain)), 4),
            'hold_rmse': round(float(np.mean(p_rmse)), 5),
            'impr_vs_ar1': round(1 - float(np.mean(p_rmse)) / float(np.mean(p_ar)), 4),
        },
    }


def online(sid, d=1, lam=LAM):
    s = load(sid)
    ut, uu, src = session_input(s)
    grid, r, ug, ok, _ = build(s, ut, uu)
    good = ok & np.isfinite(r) & np.isfinite(np.roll(r, 1))
    good[0] = False
    P = np.eye(3) * 1e2
    c = np.zeros(3)
    tt, cc = [], []
    for k in np.where(good)[0]:
        x = np.array([r[k - 1], ug[k - d] if d else ug[k], 1.0])
        y = r[k]
        Px = P @ x
        g = Px / (lam + x @ Px)
        c = c + g * (y - x @ c)
        P = (P - np.outer(g, Px)) / lam
        if k % 10 == 0:
            tt.append(grid[k])
            cc.append(c.copy())
    cc = np.array(cc)
    tt = np.array(tt)
    gain = c[1] / (1 - c[0]) if abs(1 - c[0]) > 1e-6 else float('inf')
    gs = cc[:, 1] / (1 - cc[:, 0])
    conv = None
    for i in range(len(gs)):
        if np.all(np.abs(gs[i:] / gain - 1) < 0.25):
            conv = round(float(tt[i] - tt[0]), 1)
            break
    return {
        'session': sid,
        'input': src,
        'd': d,
        'lambda': lam,
        'phi': round(float(c[0]), 4),
        'beta': round(float(c[1]), 4),
        'gain': round(float(gain), 4),
        'tau_s': round(float(-H / np.log(c[0])), 4) if 0 < c[0] < 1 else None,
        'gain_range': [round(float(gs.min()), 3), round(float(gs.max()), 3)],
        'conv_s': conv,
    }


def roll(sid, d, win=20.0, step=10.0, min_n=100):
    """20 s 滚动窗的局内增益轨迹（检验「增益在局内是否常数」）。"""
    s = load(sid)
    ut, uu, _ = session_input(s)
    grid, r, ug, ok, segs = build(s, ut, uu)
    out = []
    t_end = float(grid[segs[-1][-1]])
    for a in np.arange(0.0, max(t_end - win, 0.0) + 1e-9, step):
        sel = [x for x in segs if a <= grid[x[0]] < a + win]
        if sum(len(x) for x in sel) < min_n:
            continue
        c = _fit(_rows(sel, r, ug, d))
        out.append([round(float(a), 1),
                    round(float(c[1] / (1 - c[0])), 3) if abs(1 - c[0]) > 1e-6 else None])
    return out


def verdict(b):
    ident = (b.get('impr_vs_ar1', 0) >= 0.10 and b.get('impr_vs_placebo', 0) >= 0.20
             and 0 < b.get('phi', 0) < 1)
    stable = b.get('gain_diff', 9) <= 0.30
    return {'P1_identifiable': bool(ident), 'P2_stable': bool(stable)}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'all'
    os.makedirs(OUT, exist_ok=True)
    result = {'sessions': {}, 'criteria': {'P1': 'holdout beats AR(1) by >=10% and placebo by >=20%',
                                           'P2': 'time-split gain difference <=30%',
                                           'P3': 'RLS converged within 20 s'}}
    rows = []
    for sid in SESSIONS:
        try:
            b = batch(sid)
        except FileNotFoundError:
            print(f'{sid}: trace missing')
            continue
        b.update(verdict(b))
        result['sessions'][sid] = {'batch': b}
        rows.append(b)

    print('\n=== 批量辨识（留出段一步角速度） ===')
    print(f"{'session':18} {'input':9} {'d':>2} {'phi':>6} {'gain':>6} {'tau':>5} "
          f"{'hold':>7} {'AR1':>7} {'placebo':>7} {'vsAR1':>7} {'vsPlac':>7} "
          f"{'Δgain':>6} {'P1':>3} {'P2':>3}")
    for b in rows:
        print(f"{b['session']:18} {b['input']:9} {b['d_best']:2} {b['phi']:6.3f} "
              f"{b['gain']:6.2f} {b['tau_s']:5.2f} {b['hold_rmse']:7.4f} "
              f"{b['ar1_rmse']:7.4f} {b['placebo_rmse']:7.4f} "
              f"{b['impr_vs_ar1']:+7.1%} {b['impr_vs_placebo']:+7.1%} "
              f"{b['gain_diff']:6.1%} "
              f"{'Y' if b['P1_identifiable'] else 'n':>3} "
              f"{'Y' if b['P2_stable'] else 'n':>3}")
        p = b['phys_d4']
        print(f"{'':18} {'(d=4)':9} {'':2} {'':6} "
              f"{(p['gain'] if p['gain'] is not None else float('nan')):6.2f} "
              f"{'':5} {p['hold_rmse']:7.4f} {'':7} {'':7} {p['impr_vs_ar1']:+7.1%}")

    if cmd in ('all', 'online'):
        print('\n=== 在线递推（3 参数 RLS，λ=0.995） ===')
        for b in rows:
            sid = b['session']
            o = online(sid, d=b['d_best'])
            result['sessions'][sid]['online'] = o
            print(f"{o['session']:18} {o['input']:9} d{o['d']} phi {o['phi']:.2f} "
                  f"gain {o['gain']:.2f} range [{o['gain_range'][0]:.2f},"
                  f"{o['gain_range'][1]:.2f}] conv {o['conv_s']} s "
                  f"{'Y' if (o['conv_s'] is not None and o['conv_s'] <= 20) else 'n'}")

    if cmd in ('all', 'roll'):
        print('\n=== 20 s 滚动窗增益（检验局内是否为常数） ===')
        for b in rows:
            rl = roll(b['session'], b['d_best'])
            result['sessions'][b['session']]['roll'] = rl
            print(f"{b['session']:18} d{b['d_best']} " +
                  ' '.join(f'{t:.0f}s:{g:.2f}' for t, g in rl))

    path = os.path.join(OUT, 'j_arx_ident.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f'\njson -> {path}')


if __name__ == '__main__':
    main()
