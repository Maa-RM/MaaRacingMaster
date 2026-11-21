# -*- coding: utf-8 -*-
"""P3-A E 线：释放给车道保持辅助的机会与风险量化（只读，不仿真）。

唯一问题：实录里有多少「小误差带」拍可以连续输出 0 杆 ≥0.5 秒让游戏辅助接管，
这些机会与哪些高风险语境重叠。不做车辆闭环仿真、不预测物理横摆。

口径（全部来自 trace）：
  * 小误差带：|lat_ctrl.psi| <= psi_gate 且 |x_target - executed_lane| <= pos_gate。
    psi 是融合后的「相对车道车头角」（rad）；辅助的门是车头角，不是横位。
  * 硬风险（安全/承诺，释放即违规）：
      merging     intent_phase == merging（并线承诺）
      delivery    sel_why in {pass, graze}（交付/贴边）
      commit      sel_why == commit（承诺并线）
      near_wall   |executed_lane| >= 1.4（生产贴边逃生阈值）
      plan_avoid  计划有效且 (layer < 4 或 collision_cost > 0)
  * 软风险（机会成本，可被窗口边界吸收）：
      coinreward  sel_why in {coin, coin_hold, reward}（正在追金币/奖励）
      switch      本拍 x_target 或 goal 相对上一拍阶跃 > 0.3 道（目标跳变）
  * 机会窗 = 连续合格拍；时长 = 段内 dt 之和（含末拍帧）。
  * 释放代价代理：段内 |steer_x|/32767（实际下发杆值）；|u|<0.12 是浅杆无效带。

用法：
  .venv\\Scripts\\python.exe probe_e_release_opportunity.py [--grid] [--dump x.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics

LOGS = os.path.join(os.environ['APPDATA'], 'MaaRacingMaster', 'logs')
SESSIONS = ('20261119_104138', '20261118_195555')
HERE = os.path.dirname(os.path.abspath(__file__))
EPS = 1e-6
U_WEAK = 0.12        # 浅杆无效带（B 线：|u|<0.12 几乎不产生偏航率）
U_STRONG = 0.3
WALL_ESCAPE = 1.4    # 生产贴边逃生阈值
HARD = ('merging', 'delivery', 'commit', 'near_wall', 'plan_avoid')
SOFT = ('coinreward', 'switch')


def load(session):
    path = os.path.join(LOGS, session, 'trace', 'speedrush.jsonl')
    rows = [json.loads(line) for line in open(path, encoding='utf-8')]
    return [r for r in rows if r.get('lat_ctrl')]


def stages(rows):
    out = []
    for row in rows:
        if (not out or row['stage_id'] != out[-1][-1]['stage_id']
                or row.get('seq', 0) <= out[-1][-1].get('seq', 0)):
            out.append([])
        out[-1].append(row)
    return out


def annotate(rows):
    """按 stage 记时间轴、风险语境与目标跳变；返回 stage 列表。"""
    segs = stages(rows)
    for seg in segs:
        t = 0.0
        prev_xt = prev_goal = None
        for r in seg:
            r['_t'] = t
            t += r.get('dt') or 0.0
            plan = r.get('plan') or {}
            valid_plan = bool(plan.get('valid'))
            layer = plan.get('layer')
            cc = plan.get('collision_cost', plan.get('cc')) or 0.0
            why = r.get('sel_why')
            ex = r.get('executed_lane')
            xt = r.get('x_target')
            gl = r.get('goal')
            jump = False
            if xt is not None and prev_xt is not None and abs(xt - prev_xt) > 0.3:
                jump = True
            if gl is not None and prev_goal is not None and abs(gl - prev_goal) > 0.3:
                jump = True
            if xt is not None:
                prev_xt = xt
            if gl is not None:
                prev_goal = gl
            r['_f'] = {
                'merging': r.get('intent_phase') == 'merging',
                'delivery': why in ('pass', 'graze'),
                'commit': why == 'commit',
                'coinreward': why in ('coin', 'coin_hold', 'reward'),
                'near_wall': ex is not None and abs(ex) >= WALL_ESCAPE,
                'plan_avoid': valid_plan and ((layer is not None and layer < 4)
                                              or cc > 0.0),
                'plan_gap': not valid_plan,
                'switch': jump,
            }
    return segs


def mark(seg, psi_gate, pos_gate, xt_rate_gate=None):
    prev_xt = None
    for r in seg:
        psi = (r.get('lat_ctrl') or {}).get('psi')
        xt = r.get('x_target')
        r['_psi_ok'] = psi is not None and abs(psi) <= psi_gate
        r['_err'] = None if (xt is None) else xt - r['executed_lane']
        r['_pos_ok'] = r['_err'] is not None and abs(r['_err']) <= pos_gate
        rate_ok = True
        if xt_rate_gate is not None:
            rate_ok = (xt is None or prev_xt is None
                       or abs(xt - prev_xt) / max(1e-6, r.get('dt') or 0.05)
                       <= xt_rate_gate)
        if xt is not None:
            prev_xt = xt
        r['_xt_ok'] = rate_ok
        r['_q'] = r['_psi_ok'] and r['_pos_ok'] and rate_ok
        f = r['_f']
        r['_hard'] = any(f[k] for k in HARD)
        r['_soft'] = any(f[k] for k in SOFT)
        r['_safe_hard'] = r['_q'] and not r['_hard']
        r['_safe_all'] = r['_q'] and not r['_hard'] and not r['_soft']


def runs_of(seg, mask):
    out, cur = [], []
    for r, m in zip(seg, mask):
        if m:
            cur.append(r)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def span(ticks):
    return sum(t.get('dt') or 0.0 for t in ticks)


def bucket(d):
    if d + EPS < 0.2:
        return 'a_<0.2'
    if d + EPS < 0.3:
        return 'b_0.2-0.3'
    if d + EPS < 0.5:
        return 'c_0.3-0.5'
    if d + EPS < 1.0:
        return 'd_0.5-1.0'
    if d + EPS < 2.0:
        return 'e_1.0-2.0'
    return 'f_>=2.0'


def run_stats(ticks):
    u = [abs(t['steer_x'] / 32767.0) for t in ticks]
    psi = [abs(t['lat_ctrl']['psi']) for t in ticks
           if t['lat_ctrl'].get('psi') is not None]
    vl = [abs(t.get('v_lat') or 0.0) for t in ticks]
    div = [abs(t['lat_ctrl']['pm'] - t['lat_ctrl']['psi']) for t in ticks
           if t['lat_ctrl'].get('pm') is not None
           and t['lat_ctrl'].get('psi') is not None]
    errs = [abs(t['_err']) for t in ticks if t['_err'] is not None]
    return {
        'n': len(ticks), 'dur': round(span(ticks), 3),
        'stage': ticks[0]['stage_id'], 't0': round(ticks[0]['_t'], 2),
        't0_raw': ticks[0]['_t'],
        'u_mean': round(statistics.mean(u), 4), 'u_max': round(max(u), 4),
        'u_weak': round(sum(1 for x in u if x < U_WEAK) / len(u), 3),
        'u_strong': round(sum(1 for x in u if x >= U_STRONG) / len(u), 3),
        'u_zero': round(sum(1 for x in u if x == 0.0) / len(u), 3),
        'psi_max_deg': round(math.degrees(max(psi)), 2) if psi else None,
        'psi_end_deg': round(math.degrees(psi[-1]), 2) if psi else None,
        'err_max': round(max(errs), 3) if errs else None,
        'err_end': round(abs(ticks[-1]['_err']), 3) if ticks[-1]['_err'] is not None else None,
        'vlat_start': round(abs(ticks[0].get('v_lat') or 0.0), 3),
        'vlat_max': round(max(vl), 3),
        'div_max_deg': round(math.degrees(max(div)), 2) if div else None,
        'risk': {k: sum(1 for t in ticks if t['_f'][k])
                 for k in HARD + SOFT + ('plan_gap',)},
        # 进入前的静默前置：前 0.4s（含本拍）是否全部 |u|<0.12 且合格，
        # 用来检验「已在带内安静 ≥0.4s 才允许释放」这条在线可实现的入口条件
        'quiet_head': _quiet_head(ticks),
    }


def _quiet_head(ticks, span_s=0.4):
    acc, ok = 0.0, True
    for t in ticks:
        acc += t.get('dt') or 0.0
        if abs(t['steer_x']) / 32767.0 >= U_WEAK:
            ok = False
        if acc >= span_s - 1e-9:
            break
    return bool(ok and acc >= span_s - 1e-9)


def analyse(rows, psi_gate, pos_gate, xt_rate_gate=None):
    segs = annotate(rows)
    for seg in segs:
        mark(seg, psi_gate, pos_gate, xt_rate_gate)
    res = {'psi_gate_deg': round(math.degrees(psi_gate), 3),
           'pos_gate': pos_gate, 'xt_rate_gate': xt_rate_gate, 'ticks': 0, 'time': 0.0,
           'psi_ok_time': 0.0, 'pos_ok_time': 0.0, 'q_time': 0.0,
           'hard_time': 0.0, 'all_time': 0.0,
           'risk_q': {}, 'runs': {}, 'opps_hard': [], 'opps_all': [],
           'zero': {}, 'stages': []}
    for seg in segs:
        for r in seg:
            d = r.get('dt') or 0.0
            res['ticks'] += 1
            res['time'] += d
            res['psi_ok_time'] += d * r['_psi_ok']
            res['pos_ok_time'] += d * r['_pos_ok']
            res['q_time'] += d * r['_q']
            res['hard_time'] += d * r['_safe_hard']
            res['all_time'] += d * r['_safe_all']
            if r['_q']:
                for k in HARD + SOFT + ('plan_gap',):
                    if r['_f'][k]:
                        res['risk_q'][k] = res['risk_q'].get(k, 0) + 1
        res['stages'].append({
            'stage_id': seg[0]['stage_id'], 'n': len(seg),
            'time': round(span(seg), 2),
            'q': sum(1 for r in seg if r['_q']),
            'safe_hard': sum(1 for r in seg if r['_safe_hard']),
            'safe_all': sum(1 for r in seg if r['_safe_all'])})
    for kind, key in (('psi', '_psi_ok'), ('pos', '_pos_ok'), ('q', '_q'),
                      ('hard', '_safe_hard'), ('all', '_safe_all')):
        buckets = {}
        for seg in segs:
            for run in runs_of(seg, [r[key] for r in seg]):
                st = run_stats(run)
                buckets.setdefault(bucket(st['dur']), []).append(st)
        res['runs'][kind] = buckets
    for seg in segs:
        for run in runs_of(seg, [r['_safe_hard'] for r in seg]):
            st = run_stats(run)
            if st['dur'] + EPS >= 0.5:
                res['opps_hard'].append(st)
        for run in runs_of(seg, [r['_safe_all'] for r in seg]):
            st = run_stats(run)
            if st['dur'] + EPS >= 0.5:
                res['opps_all'].append(st)
    zr = []
    for seg in segs:
        zr += [span(x) for x in runs_of(seg, [abs(r['steer_x']) == 0 for r in seg])]
    res['zero'] = {'n': len(zr),
                   'ge_0.5': sum(1 for d in zr if d + EPS >= 0.5),
                   'time_ge_0.5': round(sum(d for d in zr if d + EPS >= 0.5), 2),
                   'max': round(max(zr), 2) if zr else 0.0}
    # 全段基率（不含合格门）：风险语境在整局占多少，用来判断小误差带是否富集风险
    base = {}
    for seg in segs:
        for r in seg:
            for k in HARD + SOFT + ('plan_gap',):
                if r['_f'][k]:
                    base[k] = base.get(k, 0) + 1
    res['base'] = {k: round(v / res['ticks'], 3) for k, v in base.items()}
    # 机会窗结束后 1.0s 内的硬风险（释放尾段是否正撞上要动作的时刻）
    for tag in ('opps_hard', 'opps_all'):
        for o in res[tag]:
            seg = next(s for s in segs if s[0]['stage_id'] == o['stage'])
            i = next(j for j, r in enumerate(seg) if r['_t'] == o['t0_raw'])
            post = [r for r in seg[i:] if r['_t'] - o['t0'] <= o['dur'] + 1.0]
            o['post_hard'] = sum(1 for r in post if r['_hard'])
    # 窗口因何结束（合格段的收尾原因）
    ends = {'psi': 0, 'pos': 0, 'both': 0, 'data': 0, 'stage_end': 0}
    for seg in segs:
        idx = {id(r): i for i, r in enumerate(seg)}
        for run in runs_of(seg, [r['_q'] for r in seg]):
            i = idx[id(run[-1])]
            if i + 1 >= len(seg):
                ends['stage_end'] += 1
                continue
            nx = seg[i + 1]
            if nx['_q']:
                continue
            if not nx['_psi_ok'] and not nx['_pos_ok']:
                ends['both'] += (nx['_err'] is not None)
            elif not nx['_psi_ok']:
                ends['psi'] += 1
            elif not nx['_pos_ok']:
                ends['pos'] += (nx['_err'] is not None)
            else:
                ends['data'] += 1
    res['ends'] = ends
    return res


def show(res, name):
    T = res['time']
    xg = res.get('xt_rate_gate')
    print(f"----- {name}  psi<={res['psi_gate_deg']}deg "
          f"pos<={res['pos_gate']}lane"
          f"{'' if xg is None else f' xt_rate<={xg}'}  {res['ticks']}p {T:.1f}s")
    print(f"  |psi| 单独合格占时 {res['psi_ok_time']/T:.1%}；"
          f"|pos| 单独 {res['pos_ok_time']/T:.1%}；两者同时 {res['q_time']/T:.1%}")
    print(f"  硬风险排除后 {res['hard_time']/T:.1%}；软风险也排除 {res['all_time']/T:.1%}")
    q = max(1, sum(1 for s in res['stages'] for _ in range(s['q'])))
    print("  合格拍内风险语境：" + ' '.join(
        f"{k}={v}({v/q:.0%})" for k, v in
        sorted(res['risk_q'].items(), key=lambda kv: -kv[1])))
    print("  全段风险基率：" + ' '.join(
        f"{k}={v:.1%}" for k, v in sorted(res['base'].items(),
                                          key=lambda kv: -kv[1])))
    print("  合格段收尾原因：" + str(res['ends']))
    for kind in ('psi', 'pos', 'q', 'hard', 'all'):
        b = res['runs'][kind]
        tot = sum(len(v) for v in b.values())
        ge = sum(len(b.get(k, [])) for k in ('d_0.5-1.0', 'e_1.0-2.0', 'f_>=2.0'))
        print(f"  [{kind:4s}] 窗 {tot} 个，>=0.5s {ge} 个")
        for k in ('a_<0.2', 'b_0.2-0.3', 'c_0.3-0.5', 'd_0.5-1.0',
                  'e_1.0-2.0', 'f_>=2.0'):
            v = b.get(k, [])
            if not v:
                continue
            print(f"      {k:9s} n={len(v):3d} time={sum(x['dur'] for x in v):5.2f}s "
                  f"u_m={statistics.mean([x['u_mean'] for x in v]):.3f} "
                  f"u_weak={statistics.mean([x['u_weak'] for x in v]):.2f} "
                  f"p_risk={sum(1 for x in v if x['dur']+EPS>=0.5 and any(x['risk'][j] for j in HARD))/max(1,len([x for x in v if x['dur']+EPS>=0.5])):.2f}")
    for tag, opps in (('hard', res['opps_hard']), ('all', res['opps_all'])):
        print(f"  >=0.5s 机会窗({tag}) {len(opps)} 个 "
              f"时长合计 {sum(o['dur'] for o in opps):.2f}s "
              f"0.5s 块 {sum(int(o['dur']/0.5) for o in opps)} 个 "
              f"({sum(int(o['dur']/0.5) for o in opps)*0.5/T:.1%} 占时)")
        if opps:
            for o in sorted(opps, key=lambda x: -x['dur']):
                print(f"      s{o['stage']} t={o['t0']:6.2f} dur={o['dur']:4.2f}s "
                      f"u_m={o['u_mean']:.3f} u_weak={o['u_weak']:.2f} "
                      f"vlat0={o['vlat_start']:.3f} psid={o['psi_max_deg']}deg "
                      f"errd={o['err_max']} div={o['div_max_deg']}deg "
                      f"quiet={o['quiet_head']} post_hard={o['post_hard']} "
                      f"soft={ {k:v for k,v in o['risk'].items() if v} }")
    print(f"  基线 下发 0 杆连续段 n={res['zero']['n']} "
          f">=0.5s {res['zero']['ge_0.5']} 段 "
          f"{res['zero']['time_ge_0.5']:.2f}s "
          f"({res['zero']['time_ge_0.5']/T:.1%}) 最长 {res['zero']['max']}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--psi-gate-deg', type=float, default=1.0)
    ap.add_argument('--pos-gate', type=float, default=0.10)
    ap.add_argument('--xt-rate-gate', type=float, default=None,
                    help='额外要求 |Δx_target|/dt <= 该值（道/秒），即目标基本静止')
    ap.add_argument('--grid', action='store_true')
    ap.add_argument('--dump')
    args = ap.parse_args()
    out = {}
    for s in SESSIONS:
        rows = load(s)
        if args.grid:
            for psi_deg in (1.0, 2.0, 5.0):
                for pos in (0.10, 0.15, 0.20):
                    res = analyse(rows, math.radians(psi_deg), pos)
                    out[f'{s}|{psi_deg}|{pos}'] = res
                    show(res, s)
        else:
            res = analyse(rows, math.radians(args.psi_gate_deg), args.pos_gate,
                          args.xt_rate_gate)
            out[s] = res
            show(res, s)
    if args.dump:
        p = os.path.join(HERE, args.dump)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print('dump ->', p)


if __name__ == '__main__':
    main()
