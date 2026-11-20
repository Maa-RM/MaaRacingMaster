"""跟踪真实执行语句，区分杆值计算路径与位置估计修正路径。

复用实录回放，不改生产文件。输出每拍最后生效的控制语句及跨路径反向事件。
行跟踪只观察局部状态；先校验与未跟踪回放逐位相同，再核对实录基线。
分组统计保留原始时间顺序，不把分散的同组采样拼接成相邻控制拍。
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import inspect
import json
from pathlib import Path
import sys
import textwrap

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[6]))

from probe_steering_replay import replay
from maaracing_master.plugins._archive.speedrush.config import load_decision
from maaracing_master.plugins._archive.speedrush.planner import LateralPlanner


def assignment_sites(update):
    lines, first = inspect.getsourcelines(update)
    root = ast.parse(textwrap.dedent(''.join(lines)))
    sites = {}
    markers = {}
    for node in ast.walk(root):
        if not isinstance(node, ast.Assign):
            continue
        names = [ast.unparse(target) for target in node.targets]
        value = ast.unparse(node.value)
        line = first + node.lineno - 1
        if 'steer_raw' in names:
            # 限速只处理既有命令，不重新决定命令的来源。
            if not (isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == 'max'):
                sites[line] = value
        if 'self.state.steer_norm' in names and value == 'u_pv':
            sites[line] = 'short_preview'
        if 'line_pos' in names:
            markers[line] = 'before_position_correction'
        if 'plan_center' in names:
            markers[line] = 'after_position_correction'
    if not sites or set(markers.values()) != {
            'before_position_correction', 'after_position_correction'}:
        raise ValueError('生产控制流程变化，无法建立跟踪位置')
    return sites, markers


def traced_replay(rows, params):
    original = LateralPlanner.update
    sites, markers = assignment_sites(original)
    observations = []

    def wrapped(planner, *args, **kwargs):
        observed = {'visited': [], 'position_path': None}

        def trace(frame, event, arg):
            if frame.f_code is not original.__code__:
                return None
            if event == 'line':
                line = frame.f_lineno
                if line in sites:
                    observed['visited'].append(line)
                    observed['source_line'] = line
                    observed['x_used'] = planner.state.executed_lane
                if line in markers:
                    observed[markers[line]] = planner.state.executed_lane
            elif event == 'return':
                observed['osc_fallback'] = bool(frame.f_locals.get('osc_fallback'))
                observed['position_path'] = planner._branch
                observed['preview'] = bool(planner._preview_active)
                observed['rebase'] = planner.last_rebase
            return trace

        previous = sys.gettrace()
        if previous is not None:
            raise RuntimeError('已有调试跟踪器，拒绝覆盖')
        sys.settrace(trace)
        try:
            command = original(planner, *args, **kwargs)
        finally:
            sys.settrace(previous)
        if 'source_line' not in observed:
            raise AssertionError('本拍没有记录到控制命令来源')
        observed['source'] = sites[observed['source_line']]
        observed['path'] = str(observed['source_line'])
        if observed['osc_fallback']:
            observed['path'] += ':osc_fallback'
        if observed['preview'] != (observed['source'] == 'short_preview'):
            raise AssertionError('最后控制语句与预瞄接管状态不一致')
        before = observed.get('before_position_correction')
        after = observed.get('after_position_correction')
        observed['position_correction'] = (
            None if before is None or after is None else after - before)
        observations.append(observed)
        return command

    LateralPlanner.update = wrapped
    try:
        values = replay(rows, params)
    finally:
        LateralPlanner.update = original
    return values, observations


def summarize(rows, values, paths):
    seconds = Counter()
    counts = Counter()
    reversal_pairs = Counter()
    stable_reversal_positions = Counter()
    same_path_events = Counter()
    entered_path_events = Counter()
    previous = None
    for i, (row, value, record) in enumerate(zip(rows, values, paths)):
        key = record['path']
        counts[key] += 1
        seconds[key] += row['dt']
        if abs(value) <= .25:
            continue
        if previous is not None and value * values[previous] < 0:
            before = paths[previous]['path']
            # 保留过零期间经过的所有路径；不能只比较两个端点。
            homogeneous = all(p['path'] == key for p in paths[previous:i + 1])
            reversal_pairs[(before, key, homogeneous)] += 1
            entered_path_events[key] += 1
            if homogeneous:
                same_path_events[key] += 1
                position_keys = {
                    p['position_path'] or 'unset' for p in paths[previous:i + 1]}
                position_key = (next(iter(position_keys)) if len(position_keys) == 1
                                else 'mixed')
                stable_reversal_positions[(key, position_key)] += 1
        previous = i
    report = []
    for key, n in counts.most_common():
        group = [p for p in paths if p['path'] == key]
        report.append({
            'path': key, 'source': group[0]['source'], 'ticks': n,
            'seconds': round(seconds[key], 3),
            'position_paths': dict(Counter(p['position_path'] or 'unset' for p in group)),
            'reversals_ending_here': entered_path_events[key],
            'reversals_without_any_path_change': same_path_events[key],
        })
    return {
        'paths': report,
        'reversal_pairs': [
            {'from': a, 'to': b, 'same_path_entire_interval': same, 'events': n}
            for (a, b, same), n in reversal_pairs.most_common()],
        'position_paths': dict(Counter(p['position_path'] or 'unset' for p in paths)),
        'stable_control_reversals_by_position_path': [
            {'control_path': control, 'position_path': position, 'events': n}
            for (control, position), n in stable_reversal_positions.most_common()],
        'warning': '路径交叉表与固定实录输入分析，不是闭环因果判决',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--trace', type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.trace.read_text(encoding='utf-8').splitlines()]
    rows = [r for r in rows if r.get('lat_ctrl')]
    groups = []
    for row in rows:
        if (not groups or row['stage_id'] != groups[-1][-1]['stage_id']
                or row['seq'] <= groups[-1][-1]['seq']):
            groups.append([])
        groups[-1].append(row)
    if not groups:
        raise ValueError('记录中没有控制拍')
    params = load_decision().planner
    for segment, selected in enumerate(groups, 1):
        baseline = replay(selected, params)
        values, paths = traced_replay(selected, params)
        assert np.array_equal(values, baseline), '跟踪改变了回放结果'
        actual = np.array([r['steer_x'] / 32767 for r in selected])
        error = float(np.mean(abs(values - actual)))
        assert error < .003, f'实录基线不一致: {error}'
        report = summarize(selected, actual, paths)
        report.update(segment=segment, stage=selected[0]['stage_id'],
                      baseline_mean_absolute_error=round(error, 6),
                      position_path_mismatch_ticks=sum(
                          p['position_path'] != r['lat_ctrl'].get('branch')
                          for p, r in zip(paths, selected)),
                      preview_mismatch_ticks=sum(
                          p['preview'] != bool(r['lat_ctrl'].get('pv'))
                          for p, r in zip(paths, selected)),
                      tracing_identical=True)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
