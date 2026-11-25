# -*- coding: utf-8 -*-
"""线观测可用率离线分析：把控制流水拆成「没出杆」的原因与线证据质量。

用途：回答「方向杆为什么大半拍为 0」「提检测频率能救多少」这类问题，不靠
现场目视。读的是控制 trace（任意布局，走 infinite_trace_reader）：

    .venv\\Scripts\\python.exe maaracing_master\\plugins\\infinite_drive\\tools\\analyze_lane_obs.py <trace.jsonl>

输出六块：
  1. 总览：拍数、可用率、出杆率、按阶段（驾驶段）切分
  2. 不可用原因：usable_reason 分布（pred / stale_det / hold / lock / sides /
     theta / no_x 等，判定顺序见 driving.py）
  3. 检测节拍：检测拍占比、检测间隔、生成→消费滞后
  4. 线证据：每拍线数、被拒原因、相邻线间距（k 差 1 时 L 差应≈1 道）
  5. 横位跳变：相邻检测帧 |Δego|，>0.5 道逐条列出
  6. 街车同拍证据：有车账的拍数与车上限

旧 trace 没有后用字段时逐块提示「字段缺失，需新一次实机采样」，不报错。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infinite_trace_reader import iter_rows, sessions  # noqa: E402

_JUMP = 0.5                 # 横位跳变门（道），与 lane_obs.LINE_JUMP_RESET 同口径
_BUCKETS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5)


def _segments(rows: list[dict], gap_s: float = 0.6) -> list[list[dict]]:
    segs: list[list[dict]] = []
    cur: list[dict] = []
    prev = None
    for r in rows:
        ts = r.get("ts_ns")
        if prev is not None and ts is not None and (ts - prev) / 1e9 > gap_s:
            if cur:
                segs.append(cur)
            cur = []
        cur.append(r)
        prev = ts
    if cur:
        segs.append(cur)
    return segs


def _pct(n: int, total: int) -> str:
    return f"{n} ({n / total:.0%})" if total else "0"


def _quantiles(values: list[float]) -> str:
    if not values:
        return "n=0"
    v = sorted(values)
    return (f"n={len(v)} mean={sum(v) / len(v):.3f} "
            f"p50={v[len(v) // 2]:.3f} p90={v[int(len(v) * 0.9)]:.3f} max={v[-1]:.3f}")


def _count_det(row: dict, prev_fid: int | None) -> bool:
    fid = row.get("lane_fid")
    return fid is not None and fid != prev_fid


def analyze(path: Path) -> None:
    rows = list(iter_rows(path))
    if not rows:
        print(f"没有数据行：{path}")
        return
    segs = _segments(rows)
    total = len(rows)
    print(f"== {path.name}：{total} 拍，{len(segs)} 个驾驶段 ==")

    # 1. 总览（按段）
    for i, seg in enumerate(segs, 1):
        n = len(seg)
        usable = sum(1 for r in seg if r.get("usable"))
        steered = sum(1 for r in seg if (r.get("steer") or 0) != 0)
        dur = (seg[-1].get("ts_ns", 0) - seg[0].get("ts_ns", 0)) / 1e9
        print(f"  段{i}: {dur:5.1f}s {n:5d}拍 可用 {_pct(usable, n):>12} "
              f"出杆 {_pct(steered, n):>12}")

    # 2. 不可用原因
    reasons = Counter(r.get("usable_reason") or "<旧trace无此字段>" for r in rows
                      if not r.get("usable"))
    print("\n-- 不可用原因 --")
    for name, cnt in reasons.most_common():
        print(f"  {name:<18} {cnt:5d}  {cnt / max(1, total - sum(1 for r in rows if r.get('usable'))):.0%}")

    # 3. 检测节拍
    det_rows: list[dict] = []
    prev_fid = None
    for r in rows:
        if _count_det(r, prev_fid):
            det_rows.append(r)
            prev_fid = r.get("lane_fid")
        elif r.get("lane_fid") is not None:
            prev_fid = r.get("lane_fid")
    print("\n-- 检测节拍 --")
    pred_n = sum(1 for r in rows if r.get("pred"))
    print(f"  检测拍 {len(det_rows)} / {total} = {len(det_rows) / total:.0%}"
          f"；预测拍 {_pct(pred_n, total)}")
    if any("det_age_ms" in r for r in rows):
        ages = [r["det_age_ms"] for r in rows if r.get("det_age_ms") is not None]
        print(f"  生成→消费滞后 ms：{_quantiles(ages)}")
        for key, label in (("queue_ms", "排队"), ("cap_ms", "采集偏移"),
                           ("slot_ms", "等槽位")):
            vals = [r[key] for r in rows if r.get(key) is not None]
            if vals:
                print(f"  {label} ms：{_quantiles(vals)}")
    else:
        print("  滞后/排队字段缺失（需新一次实机采样）")
    gaps = [(det_rows[i]["ts_ns"] - det_rows[i - 1]["ts_ns"]) / 1e9
            for i in range(1, len(det_rows))
            if det_rows[i - 1].get("ts_ns") and det_rows[i].get("ts_ns")]
    gaps = [g for g in gaps if 0 < g < 1.0]
    print(f"  检测间隔 s：{_quantiles(gaps)}")
    run = [r["run_ms"] for r in det_rows if r.get("run_ms") is not None]
    if run:
        print(f"  线推理本体 ms：{_quantiles(run)}")
    for key, label in (("busy_skips", "抢锁失败弃帧"), ("slot_skips", "超槽窗弃帧")):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if vals:
            print(f"  {label}（累计）：起 {vals[0]} 末 {vals[-1]} 共 {vals[-1] - vals[0]}")

    # 4. 线证据
    print("\n-- 线证据 --")
    if not any("line_meta" in r for r in rows):
        print("  line_meta 缺失（需新一次实机采样）")
    else:
        line_cnt = Counter(len(r.get("line_meta") or []) for r in det_rows)
        print("  每检测拍线数：" + "，".join(f"{k}条×{v}" for k, v in sorted(line_cnt.items())))
        rejects = Counter()
        accepted = 0
        spacings: list[float] = []
        for r in det_rows:
            lines = [ln for ln in (r.get("line_meta") or [])
                     if isinstance(ln, dict) and ln.get("k") is not None
                     and ln.get("L") is not None]
            for ln in lines:
                if ln.get("accepted"):
                    accepted += 1
                elif ln.get("reject"):
                    rejects[str(ln["reject"])] += 1
            lines.sort(key=lambda ln: ln["k"])
            for a, b in zip(lines, lines[1:]):
                if b["k"] - a["k"] == 1:
                    spacings.append(abs(b["L"] - a["L"]))
        print(f"  接受线 {accepted} 条；被拒原因："
              + ("，".join(f"{k}×{v}" for k, v in rejects.most_common()) or "无"))
        bucket = Counter()
        for s in spacings:
            key = next((b for b in _BUCKETS if s <= b), f">{_BUCKETS[-1]}")
            bucket[key] += 1
        print("  相邻线（k 差 1）横向间距分布（应≈1.0 道）：")
        for key in list(_BUCKETS) + [f">{_BUCKETS[-1]}"]:
            if bucket.get(key):
                print(f"    ≤{key}道: {bucket[key]}")
        print(f"  间距 {_quantiles(spacings)}")

    # 5. 横位跳变
    print("\n-- 横位跳变（相邻检测帧） --")
    jumps = []
    for i in range(1, len(det_rows)):
        a, b = det_rows[i - 1].get("ego_lane"), det_rows[i].get("ego_lane")
        if a is None or b is None:
            continue
        d = b - a
        if abs(d) > _JUMP:
            jumps.append((det_rows[i].get("ts_ns"), a, b, d))
    base = rows[0].get("ts_ns") or 0
    print(f"  |Δego|>0.5 道：{len(jumps)} 次 / {len(det_rows)} 检测拍")
    for ts, a, b, d in jumps[:20]:
        print(f"    {(ts - base) / 1e9:7.2f}s  {a:+.2f} → {b:+.2f}  (Δ{d:+.2f})")

    # 6. 街车同拍证据
    print("\n-- 街车同拍证据 --")
    have = [len(r["road_cars"]) for r in rows if r.get("road_cars") is not None]
    print(f"  有车账 {len(have)} / {total} = {len(have) / total:.0%}；"
          f"每拍车数 mean={sum(have) / len(have):.2f}" if have else "  全拍无车账")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        targets = [Path(argv[1])]
    else:
        targets = [s["trace"] for s in sessions() if s.get("layout") == "new"]
        targets = targets[-2:]
        if not targets:
            print("没找到可分析的 trace；请显式传 .jsonl 路径")
            return 1
    for p in targets:
        analyze(p)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
