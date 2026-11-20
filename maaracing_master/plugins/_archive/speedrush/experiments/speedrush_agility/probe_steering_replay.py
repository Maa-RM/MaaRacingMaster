"""只读实录执行层回放：校验基线，再比较固定实录输入下的命令敏感性。

观测、目标、计划与语义资格门沿用实录，执行层内部状态逐拍推进。
变更命令后不生成新的游戏观测，不能据此认定根因或预测闭环改善。
决策有效期未完整记录，回放假定本拍有效；基线误差超限则拒绝比较。
只冻结目标而保留原计划不能代表全链路目标冻结；平滑线角不是朝向真值。
"""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[6]))

from maaracing_master.plugins._archive.speedrush.config import load_decision
from maaracing_master.plugins._archive.speedrush.planner import LateralPlanner
from maaracing_master.plugins._archive.speedrush.tracking import DecisionOutput, DecisionState


def replay(rows, params):
    planner = LateralPlanner(params)
    values = []
    for row in rows:
        lane = row.get('lane_obs') or {}
        raw_plan = row.get('plan')
        plan = None if raw_plan is None else SimpleNamespace(
            valid=raw_plan['valid'], reason=raw_plan['reason'],
            psi_feedforward=raw_plan['pc'], layer=raw_plan['layer'],
            collision_cost=raw_plan['cc'])
        decision = DecisionOutput(
            5, DecisionState(row['state']), row['x_target'], row['reason'],
            row['fid'], row.get('dvalid') or row['fid'] + 3,
            maneuver=row['maneuver'],
            phase=row['intent_phase'], intent_precise=row['sel_why'] in ('pass', 'graze'))
        lane_ok = (lane.get('state') == 'TRACK'
                   and (lane.get('n_locked') or 0) >= 1 and lane.get('x') is not None)
        command = planner.update(
            decision, row['dt'], row['fid'], road_offset=row['road_offset'],
            road_offset_hold=row['ro_hold'],
            road_offset_new=row['dgeo_new'] and not row['ro_hold'],
            lane_x=lane.get('x'), lane_ok=lane_ok,
            lane_pos_ok=row['lat_ctrl']['lpok'], lane_relock=lane.get('state') == 'RELOCK',
            lane_theta=lane.get('theta'), lane_theta_ok=lane_ok,
            lane_fid=lane.get('det_fid'),
            lane_age_ms=row.get('obs_age', {}).get('lane', {}).get('age_ms'),
            lane_theta_age_ms=lane.get('theta_age_ms'),
            lane_n_locked=lane.get('n_locked'), plan=plan)
        values.append(command.steer_x / 32767)
    return np.array(values)


def swings(values):
    significant = values[abs(values) > .25]
    return int(sum(significant[1:] * significant[:-1] < 0))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--assert-quiet', action='store_true')
    args = parser.parse_args()
    source = args.trace
    rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines()]
    rows = [row for row in rows if row.get('lat_ctrl')]
    params = load_decision().planner
    noisy = False
    if not rows:
        raise ValueError('记录中没有执行层控制拍')
    groups = []
    for row in rows:
        if (not groups or row['stage_id'] != groups[-1][-1]['stage_id']
                or row['seq'] <= groups[-1][-1]['seq']):
            groups.append([])
        groups[-1].append(row)
    for segment, selected in enumerate(groups, 1):
        stage = selected[0]['stage_id']
        actual = np.array([row['steer_x'] / 32767 for row in selected])
        baseline = replay(selected, params)
        no_lead = replay(selected, replace(params, heading_lead_s=0))
        no_preview = replay(selected, replace(params, preview_horizon_s=0))
        # Trace 数值有舍入；重放不是逐位同一，先核对基线误差再解释反事实。
        error = float(np.mean(abs(actual - baseline)))
        assert error < .003, f'baseline mismatch: {error}'
        t = np.cumsum([row['dt'] for row in selected])
        counts = []
        for i in range(len(selected)):
            end = int(np.searchsorted(t, t[i] + 6))
            if end < len(selected):
                counts.append((swings(actual[i:end]), i, end))
        count, start, end = max(counts, default=(0, 0, len(selected)))
        noisy |= count >= 5
        print(json.dumps({
            'segment': segment,
            'stage': stage,
            'first_seq': selected[0]['seq'], 'baseline_mean_absolute_error': round(error, 5),
            'no_lead_mean_difference': round(float(np.mean(abs(baseline - no_lead))), 5),
            'no_lead_correlation': round(float(np.corrcoef(baseline, no_lead)[0, 1]), 5),
            'max_reversals_in_6_seconds_above_25_percent': count,
            'mean_absolute_steer_actual': round(float(np.mean(abs(actual))), 4),
            'mean_absolute_steer_no_preview': round(float(np.mean(abs(no_preview))), 4),
            'substantial_reversals_actual': swings(actual),
            'substantial_reversals_no_lead': swings(no_lead),
            'substantial_reversals_no_preview': swings(no_preview),
            'window_time': [round(float(t[start]), 2), round(float(t[end - 1]), 2)],
            'window_target_range_lanes': round(float(np.ptp([r['x_target'] or 0 for r in selected[start:end]])), 3),
            'warning': '固定实录输入的执行层反事实，不是实车闭环效果'
        }, ensure_ascii=False))
    if args.assert_quiet:
        assert not noisy, 'Recorded symptom reproduced: at least 5 substantial steering reversals in a 6-second window'


if __name__ == '__main__':
    main()
