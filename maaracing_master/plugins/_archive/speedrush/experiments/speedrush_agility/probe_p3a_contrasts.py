# -*- coding: utf-8 -*-
"""P3-A 离线可证伪对照：M1 目标交付 / M2 影子增益 / M3 预瞄量化。

只读回放与逐拍重算，不改生产代码。每项先写预测与证伪条件，再看结果：

M1（目标交付）：用选择层原始目标（sel_dbg.wp 末点）替掉"驻留按住"后
再按 2 道/s 整形，可选在 origin 跳变时平移 slew 前值。预测：104138 s2
1238–1300 的反向段缩短、追上目标拍数下降；证伪：反向不变。

M2（影子增益）：把生产影子增益 0.28 改为 0.45 重放固定实录输入。预测：
影子朝向与实测线角的 RMSE 下降、反向次数不升；证伪：RMSE 不降。

M3（预瞄量化）：按实录逐拍状态重算预瞄候选，把最小非零档从 0.30 降到
0.15/0.10。预测：全段命令级反向次数从 107 降到 ≤70；证伪：不变。

用法：.venv\\Scripts\\python.exe probe_p3a_contrasts.py {m1,m2,m3,all}
"""

from __future__ import annotations

import json
import math
import os
import sys
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', '..')))

import probe_steering_replay as R
from maaracing_master.plugins._archive.speedrush.config import load_decision
from maaracing_master.plugins._archive.speedrush import planner as P
from maaracing_master.plugins._archive.speedrush.planner import LateralPlanner
from maaracing_master.plugins._archive.speedrush.tracking import (
    DecisionOutput, DecisionState)

LOGS = os.path.join(os.environ['APPDATA'], 'MaaRacingMaster', 'logs')
SESSIONS = ('20261119_104138', '20261118_195555')
LW = 3.5
SLEW = 2.0
THRESH = 0.25


def load(session):
    path = os.path.join(LOGS, session, 'trace', 'speedrush.jsonl')
    rows = [json.loads(line) for line in open(path, encoding='utf-8')]
    return [r for r in rows if r.get('lat_ctrl')]


def segments(rows):
    out = []
    for row in rows:
        if (not out or row['stage_id'] != out[-1][-1]['stage_id']
                or row['seq'] <= out[-1][-1]['seq']):
            out.append([])
        out[-1].append(row)
    return out


def reversals(values, thresh=THRESH):
    sig = [v for v in values if abs(v) > thresh]
    return int(sum(a * b < 0 for a, b in zip(sig, sig[1:])))


def window_reversals(values, t, lo, hi):
    return reversals([v for v, x in zip(values, t) if lo <= x <= hi])


def times(seg):
    return list(np.cumsum([r['dt'] for r in seg]))


def resize(seg, values):
    out = json.loads(json.dumps(seg))
    for row, v in zip(out, values):
        row['x_target'] = float(v)
    return out


def slew_series(seg, raw, sync_frame=False, rate=SLEW):
    out = []
    prev = None
    prev_origin = None
    for r, x in zip(seg, raw):
        if x is None:
            x = out[-1] if out else 0.0
        dbg = r.get('sel_dbg') or {}
        origin = dbg.get('origin')
        if sync_frame and prev is not None and origin is not None \
                and prev_origin is not None:
            d = origin - prev_origin
            if abs(d) > 0.5:
                prev -= d / LW
        if prev is None:
            t = x
        else:
            step = rate * r['dt']
            t = prev + max(-step, min(step, x - prev))
        prev = t
        prev_origin = origin
        out.append(t)
    return out


def raw_target(seg):
    vals = []
    last = 0.0
    for r in seg:
        dbg = r.get('sel_dbg') or {}
        wps = dbg.get('wp') or []
        if wps and wps[-1][1] is not None:
            last = wps[-1][1]
        elif r.get('goal') is not None:
            last = r['goal']
        elif r.get('x_target') is not None:
            last = r['x_target']
        vals.append(last)
    return vals


def report(name, seg, baseline, variants, lo=None, hi=None):
    t = times(seg)
    rec = {}
    for label, values in [('baseline', baseline)] + list(variants.items()):
        rev_all = reversals(values)
        rev_win = (None if lo is None
                   else window_reversals(values, t, lo, hi))
        rec[label] = {'reversals': rev_all, 'window': rev_win,
                      'mean_abs': round(float(np.mean(np.abs(values))), 4)}
    print(json.dumps({'contrast': name, 'rows': len(seg), **rec},
                     ensure_ascii=False))


def m1():
    for session in SESSIONS:
        for seg in segments(load(session)):
            t = times(seg)
            actual = [r['steer_x'] / 32767 for r in seg]
            base = R.replay(seg, load_decision().planner)
            raw = raw_target(seg)
            no_dwell = slew_series(seg, raw)
            synced = slew_series(seg, raw, sync_frame=True)
            hold = slew_series(seg, [
                (r.get('goal') if r.get('goal') is not None
                 else r['x_target']) for r in seg])
            variants = {
                'hold_target': R.replay(resize(seg, hold),
                                        load_decision().planner),
                'no_dwell': R.replay(resize(seg, no_dwell),
                                     load_decision().planner),
                'no_dwell+frame_sync': R.replay(resize(seg, synced),
                                                load_decision().planner),
            }
            if session == '20261119_104138' and seg[0]['stage_id'] == 2:
                report('M1 104138 s2', seg, base, variants, 25.0, 29.0)
            else:
                report(f'M1 {session} s{seg[0]["stage_id"]}',
                       seg, base, variants)
            del t, actual


def _set_state(pl, row):
    lc = row['lat_ctrl']
    pl.state.heading = lc.get('pm')
    pl.state.yaw_rate = lc.get('rm')
    pl.state.executed_lane = row['executed_lane']
    pl.state.v_lat = row['v_lat']
    pl.state.v_mps = row['v_ego']


def m3():
    params = load_decision().planner
    sets = {'0.30': (0.30, 0.60, 0.90, 1.0),
            '0.15': (0.15, 0.30, 0.60, 0.90, 1.0),
            '0.10': (0.10, 0.20, 0.30, 0.60, 0.90, 1.0)}
    for session in SESSIONS:
        for seg in segments(load(session)):
            recorded, picked = [], {k: [] for k in sets}
            series = {k: [] for k in sets}
            prev = {k: None for k in sets}
            pl = LateralPlanner(params)
            match = total = 0
            for row in seg:
                lc = row['lat_ctrl']
                if not lc.get('pv') or row['x_target'] is None:
                    for k in prev:
                        prev[k] = None
                        series[k].append(row['steer_norm'])
                    continue
                _set_state(pl, row)
                u_orig = row['steer_pre_lpf']
                man = bool(lc.get('ppg') or (
                    lc.get('pvg') and (row.get('plan') or {}).get('layer') == 2))
                rec_u = row['steer_norm']
                for key, amps in sets.items():
                    P._PREVIEW_AMPS = amps
                    pl._preview_prev_u = prev[key]
                    u = pl._preview_steer(row['dt'], row['x_target'], u_orig,
                                          maneuver=man)
                    if u is None:
                        u = u_orig
                    prev[key] = u
                    picked[key].append(u)
                    series[key].append(u)
                pl._preview_prev_u = prev['0.30']
                P._PREVIEW_AMPS = (0.30, 0.60, 0.90, 1.0)
                recorded.append(rec_u)
                total += 1
                match += int(abs(rec_u - picked['0.30'][-1]) < 0.02)
            t = times(seg)
            out = {'contrast': f'M3 {session} s{seg[0]["stage_id"]}',
                   'preview_ticks': total,
                   'reproduce_rate': round(match / max(total, 1), 3)}
            for key in sets:
                out[key] = reversals(series[key])
            out['recorded'] = reversals([r['steer_norm'] for r in seg])
            windows = []
            for lo, hi in ((12.5, 13.6), (18.5, 19.1)):
                if not any(lo <= x <= hi for x in t):
                    continue
                win = {'t': [lo, hi]}
                for key in list(sets) + ['recorded']:
                    vals = ([r['steer_norm'] for r in seg] if key == 'recorded'
                            else series[key])
                    sub = [v for v, x in zip(vals, t) if lo <= x <= hi]
                    steps = [abs(b - a) for a, b in zip(sub, sub[1:])]
                    win[key] = {
                        'reversals': reversals(sub),
                        'mean_abs': round(float(np.mean(np.abs(sub))), 3),
                        'step_p90': (0.0 if not steps else
                                     round(float(np.percentile(steps, 90)), 3))}
                windows.append(win)
            out['windows'] = windows
            print(json.dumps(out, ensure_ascii=False))


def _replay_gain(seg, gain):
    old = P._MODEL_YAW_GAIN
    P._MODEL_YAW_GAIN = gain
    try:
        return R.replay(seg, load_decision().planner)
    finally:
        P._MODEL_YAW_GAIN = old


def _collect_shadow(seg, gain):
    """同 R.replay 的输入契约，额外收集车模朝向与观测朝向。"""
    params = load_decision().planner
    old = P._MODEL_YAW_GAIN
    P._MODEL_YAW_GAIN = gain
    pl = LateralPlanner(params)
    out = []
    try:
        for row in seg:
            lane = row.get('lane_obs') or {}
            rp = row.get('plan')
            plan = None if rp is None else SimpleNamespace(
                valid=rp['valid'], reason=rp['reason'],
                psi_feedforward=rp['pc'], layer=rp['layer'],
                collision_cost=rp['cc'])
            decision = DecisionOutput(
                5, DecisionState(row['state']), row['x_target'], row['reason'],
                row['fid'], row.get('dvalid') or row['fid'] + 3,
                maneuver=row['maneuver'], phase=row['intent_phase'],
                intent_precise=row['sel_why'] in ('pass', 'graze'))
            lane_ok = (lane.get('state') == 'TRACK'
                       and (lane.get('n_locked') or 0) >= 1
                       and lane.get('x') is not None)
            pl.update(
                decision, row['dt'], row['fid'],
                road_offset=row['road_offset'],
                road_offset_hold=row['ro_hold'],
                road_offset_new=row['dgeo_new'] and not row['ro_hold'],
                lane_x=lane.get('x'), lane_ok=lane_ok,
                lane_pos_ok=row['lat_ctrl']['lpok'],
                lane_relock=lane.get('state') == 'RELOCK',
                lane_theta=lane.get('theta'), lane_theta_ok=lane_ok,
                lane_fid=lane.get('det_fid'),
                lane_age_ms=row.get('obs_age', {}).get('lane', {}).get('age_ms'),
                lane_theta_age_ms=lane.get('theta_age_ms'),
                lane_n_locked=lane.get('n_locked'), plan=plan)
            lc = row['lat_ctrl']
            out.append((pl.head.mpsi, lc.get('pm'), lc.get('psi')))
    finally:
        P._MODEL_YAW_GAIN = old
    return out


def m2():
    for session in SESSIONS:
        for seg in segments(load(session)):
            actual = np.array([r['steer_x'] / 32767 for r in seg])
            base = _replay_gain(seg, 0.28)
            hi = _replay_gain(seg, 0.45)
            err = np.abs(hi - base)
            pairs = {'0.28': _collect_shadow(seg, 0.28),
                     '0.45': _collect_shadow(seg, 0.45)}
            rmse = {}
            for key, items in pairs.items():
                d_pm = [m - p for m, p, _ in items
                        if m is not None and p is not None]
                d_obs = [m - s for m, _, s in items
                         if m is not None and s is not None]
                rmse[key] = {
                    'vs_pm': (None if not d_pm else round(float(
                        np.sqrt(np.mean(np.square(d_pm)))), 4)),
                    'vs_psi': (None if not d_obs else round(float(
                        np.sqrt(np.mean(np.square(d_obs)))), 4))}
            print(json.dumps({
                'shadow_rmse': rmse,
                'contrast': f'M2 {session} s{seg[0]["stage_id"]}',
                'reversals_base': reversals(base),
                'reversals_gain45': reversals(hi),
                'reverse_ticks': int((base * hi < 0).sum()),
                'mean_abs_base': round(float(np.mean(np.abs(base))), 4),
                'mean_abs_gain45': round(float(np.mean(np.abs(hi))), 4),
                'mean_abs_rerecorded': round(float(np.mean(np.abs(actual))), 4),
                'mean_diff': round(float(np.mean(err)), 5),
                'max_diff': round(float(np.max(err)), 4),
            }, ensure_ascii=False))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if cmd in ('m1', 'all'):
        m1()
    if cmd in ('m2', 'all'):
        m2()
    if cmd in ('m3', 'all'):
        m3()
