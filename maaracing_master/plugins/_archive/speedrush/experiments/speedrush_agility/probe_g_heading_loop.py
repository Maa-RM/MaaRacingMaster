# -*- coding: utf-8 -*-
"""P3-A G：车头角环量化 + 单变量回放敏感性。

主量：相对车道线的车头角误差（ψ_des − ψ）、越过幅度、收敛时间、延迟、半周期。
横向位移不参与拟合，只作背景。

用法：
  .venv\\Scripts\\python.exe probe_g_heading_loop.py metrics
  .venv\\Scripts\\python.exe probe_g_heading_loop.py sweep
"""

from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys
from dataclasses import replace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', '..')))

import probe_steering_replay as R
from maaracing_master.plugins._archive.speedrush.config import load_decision

OUT = os.path.join(HERE, '_out')


def sgn(x):
    return 0 if x is None or abs(x) < 1e-12 else (1 if x > 0 else -1)


def q(xs, p):
    return None if not xs else round(float(np.percentile(xs, p)), 4)


def load_dumps():
    out = []
    for f in sorted(glob.glob(os.path.join(OUT, 'c_*.json'))):
        if '225251' in f:
            continue
        out.append((os.path.basename(f), json.load(open(f, encoding='utf-8'))))
    return out


def rev_pairs(ticks):
    sig = [i for i, x in enumerate(ticks) if abs(x['emit'] / 32767) > 0.25]
    return [(a, b) for a, b in zip(sig, sig[1:])
            if ticks[a]['emit'] * ticks[b]['emit'] < 0]


def metrics():
    over, cmd, ratio, rates, trans, half, settle, delay = \
        [], [], [], [], [], [], [], []
    grow = []
    for name, ticks in load_dumps():
        revs = rev_pairs(ticks)
        prev_over = None
        prev_b = None
        for a, b in revs:
            sb = ticks[b].get('stick')
            sa = ticks[a].get('stick')
            if not sb:
                continue
            ov = abs(sb['psi'] - sb['psi_des'])
            over.append(ov)
            cmd.append(abs(sb['psi_des']))
            if abs(sb['psi_des']) >= 0.01:
                ratio.append(ov / abs(sb['psi_des']))
            rates.append(abs(sb.get('r') or 0.0))
            trans.append(ticks[b]['t'] - ticks[a]['t'])
            if prev_b is not None:
                half.append(ticks[b]['t'] - ticks[prev_b]['t'])
            prev_b = b
            if prev_over is not None and prev_over > 1e-4:
                grow.append(ov / prev_over)
            prev_over = ov
            # 收敛：反向之后 |ψ−ψ_des| 回到 10% 过冲或 0.01 rad 以内并保持 3 拍
            band = max(0.01, 0.2 * ov)
            done = None
            run = 0
            for j in range(b, min(len(ticks), b + 40)):
                sj = ticks[j].get('stick')
                if not sj:
                    continue
                if abs(sj['psi'] - sj['psi_des']) < band:
                    run += 1
                    if run >= 3:
                        done = ticks[j]['t'] - ticks[b]['t']
                        break
                else:
                    run = 0
            settle.append(done)
            # 延迟：车头越过指令的时刻 − 指令最后一个局部极值时刻
            cross = None
            for j in range(b - 1, max(a, b - 12) - 1, -1):
                sj = ticks[j].get('stick')
                sk = ticks[j + 1].get('stick')
                if not sj or not sk:
                    continue
                e0 = sj['psi'] - sj['psi_des']
                e1 = sk['psi'] - sk['psi_des']
                if e0 * e1 < 0:
                    cross = ticks[j]['t']
                    break
            if cross is not None:
                pk = None
                for j in range(b, max(a, b - 16) - 1, -1):
                    sj = ticks[j].get('stick')
                    if not sj:
                        continue
                    lo = j == 0 or (ticks[j - 1].get('stick') or sj)['psi_des'] <= sj['psi_des']
                    hi = j == len(ticks) - 1 or \
                        (ticks[j + 1].get('stick') or sj)['psi_des'] >= sj['psi_des']
                    if ticks[j]['t'] <= cross and lo and hi:
                        pk = ticks[j]['t']
                        break
                if pk is not None:
                    delay.append(cross - pk)
    settled = [x for x in settle if x is not None]
    print(json.dumps({
        'n_reversals': len(over),
        'overshoot_rad': {'p50': q(over, 50), 'p90': q(over, 90),
                          'max': q(over, 100)},
        'cmd_rad': {'p50': q(cmd, 50), 'p90': q(cmd, 90)},
        'overshoot_over_cmd_cmd_ge_0.01': {
            'n': len(ratio), 'p50': q(ratio, 50), 'p90': q(ratio, 90),
            'max': q(ratio, 100)},
        'yaw_rate_rad_s': {'p50': q(rates, 50), 'p90': q(rates, 90)},
        'transition_s': {'p50': q(trans, 50), 'p90': q(trans, 90)},
        'half_period_s': {'n': len(half), 'p50': q(half, 50),
                          'p90': q(half, 90)},
        'settle_s': {'n': len(settled), 'unsettled': len(settle) - len(settled),
                     'p50': q(settled, 50), 'p90': q(settled, 90),
                     'max': q(settled, 100)},
        'overshoot_growth_ratio': {'n': len(grow), 'p50': q(grow, 50),
                                   'p90': q(grow, 90),
                                   'share_gt1': round(
                                       float(np.mean(np.array(grow) > 1)), 3)},
        'cmd_peak_to_cross_s': {'n': len(delay), 'p50': q(delay, 50),
                                'p90': q(delay, 90)},
        'command_to_yaw_delay_s': delay_xcorr(),
    }, ensure_ascii=False))


def delay_xcorr():
    """指令与角速度的互相关峰：每条会话所有段的合并估计。"""
    lags = []
    for sid in ('20261119_104138', '20261118_195555'):
        for s in load_rows(sid):
            t = np.cumsum([0.0] + [r['dt'] for r in s[:-1]])
            u = np.array([r['steer_x'] / 32767 for r in s])
            r = np.array([(r['lat_ctrl'] or {}).get('r') or 0.0 for r in s])
            best, best_lag = -1e9, None
            for lag in range(0, 9):                 # 0~0.4 s，按 0.05 s
                a = u[:len(u) - lag] if lag else u
                b = r[lag:] if lag else r
                if len(a) < 20:
                    continue
                c = float(np.corrcoef(a, b)[0, 1])
                if c > best:
                    best, best_lag = c, lag * 0.05
            if best_lag is not None:
                lags.append(best_lag)
    return {'n_segments': len(lags), 'p50': q(lags, 50),
            'p90': q(lags, 90)}


def load_rows(sid):
    path = os.path.join(os.path.expandvars(r'%APPDATA%\MaaRacingMaster\logs'),
                        sid, 'trace', 'speedrush.jsonl')
    rows = [json.loads(line) for line in open(path, encoding='utf-8')]
    rows = [r for r in rows if r.get('lat_ctrl')]
    segs = []
    for r in rows:
        if not segs or r['stage_id'] != segs[-1][-1]['stage_id'] \
                or r['seq'] <= segs[-1][-1]['seq']:
            segs.append([])
        segs[-1].append(r)
    return segs


def reversals(v):
    sig = [x for x in v if abs(x) > 0.25]
    return int(sum(a * b < 0 for a, b in zip(sig, sig[1:])))


def sweep():
    base = load_decision().planner
    variants = {
        'baseline': {},
        'k_r-25%': {'k_r': base.k_r * 0.75},
        'k_r+25%': {'k_r': base.k_r * 1.25},
        'k_psi-25%': {'k_psi': base.k_psi * 0.75},
        'k_psi+25%': {'k_psi': base.k_psi * 1.25},
        'lead 0.0': {'heading_lead_s': 0.0},
        'lead 0.2': {'heading_lead_s': 0.2},
        'tau 0.15': {'tau_heading_s': 0.15},
        'tau 0.35': {'tau_heading_s': 0.35},
    }
    rows = {sid: load_rows(sid) for sid in ('20261119_104138', '20261118_195555')}
    out = {}
    for label, kv in variants.items():
        params = replace(base, **kv)
        per = []
        for sid, segs in rows.items():
            for s in segs:
                vals = R.replay(s, params)
                per.append({'sid': sid, 'stage': s[0]['stage_id'],
                            'rev': reversals(vals),
                            'mean_abs': round(float(np.mean(np.abs(vals))), 4)})
        out[label] = {
            'rev': sum(p['rev'] for p in per),
            'mean_abs': round(float(np.mean([p['mean_abs'] for p in per])), 4),
            'per': per,
        }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'metrics'
    if cmd == 'sweep':
        sweep()
    else:
        metrics()
