# -*- coding: utf-8 -*-
"""P3-A 闭环台架第一步：用实录标定/验证车辆响应模型。

参数来源（不是猜）：
- 杆→偏航率增益：人工局脉冲积分 0.367、程序局 0.488/0.530，分段增益
  凸形（|u|<0.12 近零，0.35 以上 0.4~0.7）。这里用 B 线的分段中位曲线做
  名义值，另跑 0.8/1.2 倍带做敏感性。
- 总延迟 0.19~0.21 s（互相关与脉冲拟合一致）。
- 弱回正 k≈-0.9/s：人工局松杆段 dθ/dt=kθ。
- 一阶滞后 τ=0.02 s（该数据上不可辨识，只取可辨识的「总延迟 + 直流增益」）。

口径：
- 人工局用 200 Hz 物理手柄（外生输入）标定/验证植物。
- 程序局用实录下发杆值做 teacher forcing，验证同一植物能否复现真实响应；
  这不是控制器闭环效果评估。
- 每段用前半段估一个慢变偏置（道路曲率/零点），后半段验证；偏置在
  后续控制器对照中固定不变。

用法：.venv\\Scripts\\python.exe probe_p3a_bench.py [--band]
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from probe_b_stick_plant import FULL, Session, load, zoh

LOGS = os.path.join(os.environ['APPDATA'], 'MaaRacingMaster', 'logs')
LW = 3.5


def load_stages(sid: str):
    path = os.path.join(LOGS, sid, 'trace', 'speedrush.jsonl')
    out = []
    for line in open(path, encoding='utf-8'):
        d = json.loads(line)
        if d.get('type') or 'fid' not in d or 'steer_x' not in d:
            continue
        out.append(d)
    return out


KNOTS_U = np.array([0.0, 0.12, 0.25, 0.375, 0.525, 0.675, 0.85, 1.0])
KNOTS_G = np.array([0.0, 0.0, 0.14, 0.445, 0.59, 0.695, 0.69, 0.69])


def gain(u, scale=1.0):
    a = np.abs(u)
    g = np.interp(a, KNOTS_U, KNOTS_G) * scale
    return np.sign(u) * g


def road_kappa(rows):
    """前方车道中心斜率（rad）：道路自身的转向率输入。"""
    out = []
    for r in rows:
        dbg = r.get('sel_dbg') or {}
        c0, c8, look = dbg.get('c0'), dbg.get('c8'), dbg.get('look')
        if c0 is None or c8 is None or not look:
            out.append(0.0)
        else:
            out.append(float(np.arctan2((c8 - c0) * LW, look)))
    return np.array(out)


def simulate(rows, src_t, src_u, base_ns, d=0.20, lam=0.9, tau=0.02,
             scale=1.0, bias=0.0, road=True):
    t = np.array([(r['ts_ns'] - base_ns) / 1e9 for r in rows])
    v = np.array([r.get('v_ego') or 0.0 for r in rows])
    kappa = road_kappa(rows) if road else np.zeros(len(rows))
    u = zoh(src_t, src_u, t - d)
    psi = np.zeros(len(t))
    r = 0.0
    for i in range(1, len(t)):
        dt = max(t[i] - t[i - 1], 1e-4)
        target = gain(u[i], scale) - lam * psi[i - 1] + bias
        a = 1.0 - np.exp(-dt / tau)
        r += a * (target - r)
        psi[i] = psi[i - 1] + (r - v[i] * kappa[i]) * dt
    vlat = v * np.tan(psi) / LW
    return t, psi, vlat


def metrics(rows, psi, vlat):
    ps = np.array([(r.get('lat_ctrl') or {}).get('psi', np.nan)
                   for r in rows], dtype=float)
    ok = np.isfinite(ps)
    if ok.sum() < 10:
        return None
    err = psi[ok] - ps[ok]
    n = ok.sum() // 2
    err2 = err[n:]
    vr = np.array([r.get('v_lat') or 0.0 for r in rows])
    return {
        'n': int(ok.sum() // 2),
        'psi_rmse': round(float(np.sqrt(np.mean(err2 ** 2))), 4),
        'psi_mae': round(float(np.mean(np.abs(err2))), 4),
        'bias_val': round(float(np.mean(err[n:])), 4),
        'sway_model': round(float(np.mean(np.abs(vlat) > 0.8)), 3),
        'sway_rec': round(float(np.mean(np.abs(vr) > 0.8)), 3),
    }


def run_session(sid, params):
    sess: Session = load(sid)
    rows = load_stages(sid)
    stages = []
    for d in rows:
        if not stages or d['stage_id'] != stages[-1][-1]['stage_id'] \
                or d['seq'] <= stages[-1][-1]['seq']:
            stages.append([])
        stages[-1].append(d)
    src_t, src_u = (sess.pads_t, sess.pads_u) if sess.pads_t is not None \
        else (sess.t, sess.steer_x)
    res = []
    base_ns = rows[0]['ts_ns']
    for rows_s in stages:
        _, psi0, _ = simulate(rows_s, src_t, src_u, base_ns, **params)
        ps = np.array([(r.get('lat_ctrl') or {}).get('psi', np.nan)
                       for r in rows_s], dtype=float)
        ok = np.isfinite(ps)
        idx = np.where(ok)[0]
        half = len(idx) // 2
        e = psi0[idx[:half]] - ps[idx[:half]]
        lam = params.get('lam', 0.9)
        bias = -float(np.mean(e)) * lam if half else 0.0
        _, psi, vlat = simulate(rows_s, src_t, src_u, base_ns,
                                **{**params, 'bias': bias})
        m = metrics(rows_s, psi, vlat)
        if m:
            m.update({'stage': rows_s[0]['stage_id'],
                      'bias': round(bias, 3)})
            res.append(m)
    return sess, res


def stage_rmse(rows_s, src_t, src_u, base_ns, params):
    _, psi, _ = simulate(rows_s, src_t, src_u, base_ns, **params)
    ps = np.array([(r.get('lat_ctrl') or {}).get('psi', np.nan)
                   for r in rows_s], dtype=float)
    ok = np.isfinite(ps)
    if ok.sum() < 20:
        return None
    idx = np.where(ok)[0]
    half = len(idx) // 2
    if half < 10:
        return None
    e = psi[idx[:half]] - ps[idx[:half]]
    bias = -float(np.mean(e)) * params.get('lam', 0.9)
    _, psi, _ = simulate(rows_s, src_t, src_u, base_ns,
                         **{**params, 'bias': bias})
    d = psi[idx[half:]] - ps[idx[half:]]
    return float(np.sqrt(np.mean(d ** 2))), bias


def fit():
    """人工局第一段联合拟合；其余段留出验证。"""
    manual = '20261118_225251'
    sess = load(manual)
    rows = load_stages(manual)
    base = rows[0]['ts_ns']
    st = []
    for d in rows:
        if not st or d['stage_id'] != st[-1][-1]['stage_id'] \
                or d['seq'] <= st[-1][-1]['seq']:
            st.append([])
        st[-1].append(d)
    src = (sess.pads_t, sess.pads_u)
    best = None
    for scale in np.arange(0.1, 1.21, 0.1):
        for lam in np.arange(0.0, 2.01, 0.25):
            for dly in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35):
                r = stage_rmse(st[0], *src, base,
                               {'scale': round(float(scale), 2),
                                'lam': round(float(lam), 2),
                                'd': dly})
                if r is None:
                    continue
                if best is None or r[0] < best[0]:
                    best = (r[0], round(float(scale), 2),
                            round(float(lam), 2), dly, r[1])
    print(json.dumps({'fit_manual_s1': {
        'rmse': round(best[0], 4), 'scale': best[1],
        'lam': best[2], 'delay': best[3], 'bias': round(best[4], 3)}}))
    params = {'scale': best[1], 'lam': best[2], 'd': best[3]}
    for sid in ('20261118_225251', '20261119_104138', '20261118_195555'):
        s2 = load(sid)
        rows2 = load_stages(sid)
        base2 = rows2[0]['ts_ns']
        src2 = (s2.pads_t, s2.pads_u) if s2.pads_t is not None \
            else (s2.t, s2.steer_x)
        groups = []
        for d in rows2:
            if not groups or d['stage_id'] != groups[-1][-1]['stage_id'] \
                    or d['seq'] <= groups[-1][-1]['seq']:
                groups.append([])
            groups[-1].append(d)
        out = []
        for g in groups:
            r = stage_rmse(g, *src2, base2, params)
            if r:
                out.append({'stage': g[0]['stage_id'],
                            'rmse': round(r[0], 4),
                            'bias': round(r[1], 3)})
        print(json.dumps({'holdout': sid,
                          'kind': 'manual' if s2.pads_t is not None
                          else 'program',
                          'stages': out}, ensure_ascii=False))


def main():
    if '--fit' in sys.argv:
        fit()
        return
    band = '--band' in sys.argv
    sessions = ('20261118_225251', '20261119_104138', '20261118_195555')
    grid = [{'scale': 1.0, 'lam': 0.9}]
    if band:
        grid = [{'scale': s, 'lam': l} for s in (0.8, 1.0, 1.2)
                for l in (0.8, 1.0, 1.2)]
    for sid in sessions:
        for params in grid:
            sess, res = run_session(sid, params)
            kind = 'manual' if sess.pads_t is not None else 'program'
            print(json.dumps({'session': sid, 'kind': kind,
                              **{k: round(v, 3) if isinstance(v, float) else v
                                 for k, v in params.items()},
                              'stages': res}, ensure_ascii=False))


if __name__ == '__main__':
    main()
