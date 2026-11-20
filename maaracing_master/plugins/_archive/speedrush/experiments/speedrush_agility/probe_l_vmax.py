# -*- coding: utf-8 -*-
"""P3-A L：从起步平台期读当前车最大速度（离线、只读）。

问题：每局开局的固定空旷段里，HUD 得分速度（1 分 = 1 米）第一次进入稳定平台期的
值，就是当前车在当前改装下的最大速度。用它替换写死的 41 m/s。

口径：
- 得分速度 ``hud_rate`` 按 1 s 桶取**最小值**：金币/超车只加分，取最小可去掉事件；
- 稳定平台期 = 连续 ≥3 个 1 s 桶落在中位数 ±1 内，且值在 15~90 m/s；
- 锁定值 = **第一个三秒平台期，且其后不再出现更高的平台期**（终态平台）：
  195555 在加速段先有 38 的平台、1 s 后才到 41，直接锁 38 会低 7%；
- 三局都输出平台值与锁定时刻，供换车/换改装后复核。

用法：
    .venv\\Scripts\\python.exe probe_l_vmax.py
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '_out')
LOGS = os.path.join(os.environ['APPDATA'], 'MaaRacingMaster', 'logs')
SESSIONS = ['20261118_195555', '20261118_225251', '20261119_104138']

BUCKET_S = 1.0
MIN_RUN = 3
LO, HI = 15.0, 90.0


def load_rate(sid):
    ts, hr = [], []
    for line in open(os.path.join(LOGS, sid, 'trace', 'speedrush.jsonl'),
                     encoding='utf-8'):
        d = json.loads(line)
        if d.get('type') or 'fid' not in d:
            continue
        v = d.get('hud_rate')
        if isinstance(v, (int, float)):
            ts.append(d['ts_ns'])
            hr.append(float(v))
    ts = np.array(ts)
    return (ts - ts[0]) / 1e9, np.array(hr)


def per_second(t, r, t_end=30.0):
    """1 s 桶取最小（事件只加分），空桶丢弃。"""
    out = []
    k = 0
    while k * BUCKET_S < t_end:
        m = (t >= k * BUCKET_S) & (t < (k + 1) * BUCKET_S)
        if m.any():
            out.append((k, float(r[m].min())))
        k += 1
    return out


def plateaus(seq):
    """连续 ≥3 桶落在中位数 ±1 内。"""
    out = []
    i = 0
    while i < len(seq):
        j = i
        while j + 1 < len(seq) and abs(seq[j + 1][1] - seq[i][1]) <= 1.0:
            j += 1
        run = seq[i:j + 1]
        if len(run) >= MIN_RUN:
            val = float(np.median([v for _, v in run]))
            if LO <= val <= HI:
                out.append((run[0][0], run[-1][0], round(val, 1)))
        i = j + 1
    return out


def pick_vmax(pl):
    """第一个终态平台：其后不再有更高的平台期。"""
    for i, (a, b, val) in enumerate(pl):
        if not any(v > val + 1.0 for _, _, v in pl[i + 1:]):
            return (a, b, val)
    return None


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {}
    for sid in SESSIONS:
        t, r = load_rate(sid)
        seq = per_second(t, r)
        pl = plateaus(seq)
        first = pick_vmax(pl)
        vmax = first[2] if first else None
        res[sid] = {
            'per_second': [[k, round(v, 1)] for k, v in seq[:20]],
            'plateaus': pl,
            'vmax': vmax,
            'lock_at_s': first[1] if first else None,
        }
        print(f'{sid}: 1s序列(前20) {[round(v, 1) for _, v in seq[:20]]}')
        print(f'  稳定平台 {pl}  -> v_max {vmax} m/s'
              + (f'（{first[0]}~{first[1]} s 平台）' if first else ''))
    path = os.path.join(OUT, 'l_vmax.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f'\njson -> {path}')


if __name__ == '__main__':
    main()
