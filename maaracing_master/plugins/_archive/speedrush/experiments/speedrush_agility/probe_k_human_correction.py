# -*- coding: utf-8 -*-
"""P3-A K：人工「边跑边修」的杆形态与车头角响应（离线、只读）。

问题：人工到底怎么打杆？与程序的持续持杆差在哪？用 20261118_225251 的
200 Hz 物理手柄做测量，并在车道内保持（|横向位|<0.3 道）同口径对比程序局。

口径：
- 脉冲 = 杆绝对值 ≥ 死区（2000/32767）的连续段；≤20 ms 的零缝并回同一段。
- 松杆超前列 = 松杆时刻 → 松杆后车头角（按杆方向取极值）的时间。
- 滑行占比 = 松杆后车头角继续同向的变化量 ÷ 脉冲期内的变化量。
- 车道内保持 = 脉冲起点（对比项为样本）横向位绝对值 <0.3 道；非换道工况。
- 程序局用实际下发杆；人工局用物理手柄，不用 trace 里的反事实命令。

用法：
    .venv\\Scripts\\python.exe probe_k_human_correction.py all
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from probe_b_stick_plant import load

OUT = os.path.join(HERE, '_out')
LOGS = os.path.join(os.environ['APPDATA'], 'MaaRacingMaster', 'logs')
MANUAL = '20261118_225251'
PROGRAM = ['20261118_195555', '20261119_104138']
DEAD = 2000 / 32767


def _x_series(sid):
    ts, xs = [], []
    for line in open(os.path.join(LOGS, sid, 'trace', 'speedrush.jsonl'),
                     encoding='utf-8'):
        d = json.loads(line)
        if d.get('type') or 'fid' not in d:
            continue
        v = (d.get('lane_obs') or {}).get('x')
        if v is None:
            continue
        ts.append(d['ts_ns'])
        xs.append(v)
    ts = np.array(ts)
    return (ts - ts.min()) / 1e9, np.array(xs)


def pulses(t, u):
    on = np.abs(u) >= DEAD
    runs, st = [], None
    for i, v in enumerate(on):
        if v and st is None:
            st = i
        if not v and st is not None:
            j = i
            while j < len(on) and not on[j] and t[j] - t[i] < 0.02:
                j += 1
            if j < len(on) and on[j]:
                continue
            runs.append((st, i - 1))
            st = None
    if st is not None:
        runs.append((st, len(on) - 1))
    return runs


def stick_stats(t, u):
    runs = pulses(t, u)
    dur = np.array([t[b] - t[a] for a, b in runs])
    gap = np.array([t[runs[i + 1][0]] - t[runs[i][1]] for i in range(len(runs) - 1)])
    amp = np.array([np.max(np.abs(u[a:b + 1])) for a, b in runs])
    T = float(t[-1] - t[0])
    sig = np.sign(u[np.abs(u) >= DEAD])
    rev = int(np.sum(sig[1:] * sig[:-1] < 0)) if len(sig) > 1 else 0
    return {
        'pulses': len(runs),
        'pulses_per_min': round(len(runs) / T * 60, 1),
        'dur_s': [round(float(np.percentile(dur, p)), 3) for p in (50, 90, 100)],
        'gap_s': round(float(np.median(gap)), 3) if len(gap) else None,
        'duty': round(float(dur.sum() / T), 3),
        'amp': [round(float(np.percentile(amp, p)), 2) for p in (50, 25, 75)],
        'frac_amp_gt_08': round(float((amp > 0.8).mean()), 2),
        'u_abs_median': round(float(np.median(np.abs(u))), 2),
        'frac_u_gt_05': round(float((np.abs(u) > 0.5).mean()), 2),
        'frac_zero': round(float((np.abs(u) < DEAD).mean()), 2),
        'rev_per_min': round(rev / T * 60, 1),
    }


def align(sid=MANUAL, limit=0.3):
    s = load(sid)
    tt, th = s.theta_t, s.theta
    pt, pu = s.pads_t, s.pads_u
    tg, xs = _x_series(sid)
    thg = lambda q: np.interp(q, tt, th)
    rows = []
    for a, b in pulses(pt, pu):
        t0, t1 = pt[a], pt[b]
        if t1 + 0.8 > tt.max():
            continue
        if abs(np.interp(t0, tg, xs)) >= limit:
            continue
        sg = np.sign(np.mean(pu[a:b + 1]))
        q = np.arange(t1, t1 + 0.8, 0.01)
        v = sg * np.array([thg(x) for x in q])
        peak = q[int(np.argmax(v))]
        rows.append((sg * thg(t0), sg * thg(t1), sg * thg(peak), peak - t1))
    r = np.array(rows)
    med = lambda x: round(float(np.median(x)), 4)
    return {
        'n': len(r),
        'theta_start': med(r[:, 0]),
        'theta_release': med(r[:, 1]),
        'theta_peak_after': med(r[:, 2]),
        'move_in_pulse': med(r[:, 1] - r[:, 0]),
        'coast_after_release': med(r[:, 2] - r[:, 1]),
        'release_to_peak_s': med(r[:, 3]),
        'coast_share': round(float(np.median((r[:, 2] - r[:, 1])
                                            / np.maximum(np.abs(r[:, 1] - r[:, 0]) + 1e-6,
                                                         1e-6))), 2),
        'still_turning_at_release': round(float(np.mean(r[:, 2] - r[:, 1] > 1e-4)), 2),
    }


def heading_stats(sid, limit=0.3):
    s = load(sid)
    tg, xs = _x_series(sid)
    th = np.interp(tg, s.theta_t, s.theta)
    keep = np.abs(xs) < limit
    a = th[keep]
    dur = float(tg[keep][-1] - tg[keep][0])
    sgn = np.zeros(len(a))
    cur = 0
    for i, v in enumerate(a):
        if v > 0.01:
            cur = 1
        elif v < -0.01:
            cur = -1
        sgn[i] = cur
    zc = int(np.sum(np.abs(np.diff(sgn)) > 0))
    return {
        'n': int(keep.sum()),
        'dur_s': round(dur, 1),
        'theta_std': round(float(a.std()), 4),
        'theta_p90': round(float(np.percentile(np.abs(a), 90)), 4),
        'zero_cross_per_min': round(zc / dur * 60, 1),
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    ms = load(MANUAL)
    res = {'manual': {'stick': stick_stats(ms.pads_t, ms.pads_u),
                      'align_lane_keep': align(),
                      'heading_lane_keep': heading_stats(MANUAL)},
           'program': {}}
    print('=== 人工局（200 Hz 手柄） ===')
    print('杆形态', json.dumps(res['manual']['stick'], ensure_ascii=False))
    print('脉冲→车头角', json.dumps(res['manual']['align_lane_keep'], ensure_ascii=False))
    print('车道内车头角', json.dumps(res['manual']['heading_lane_keep'], ensure_ascii=False))
    print('\n=== 程序局（实际下发杆） ===')
    for sid in PROGRAM:
        s = load(sid)
        res['program'][sid] = {'stick': stick_stats(s.t, s.steer_x),
                               'heading_lane_keep': heading_stats(sid)}
        print(sid, json.dumps(res['program'][sid], ensure_ascii=False))
    path = os.path.join(OUT, 'k_human_correction.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f'\njson -> {path}')


if __name__ == '__main__':
    main()
