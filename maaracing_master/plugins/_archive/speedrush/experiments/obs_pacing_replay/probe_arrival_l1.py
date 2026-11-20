#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拾帧对齐（L1）前置验证：真实到达序列录制 + 拾帧策略模拟。

**要回答什么**（方案文档「L1 前置实验」的判据）：
1. 生产采集的**逐帧到达序列**长什么样（相邻接受帧间隔分布：p5/p50/p90/p99）——
   「平均 40fps」不能推出「可预测 25.2ms 时钟」，本探针拿原始序列说话；
2. 三种拾帧策略在真实序列上的收益与代价（离线模拟，逐拍实测口径）：
   - **基线**：50ms 固定网格 + 拍体 B（生产现状）；
   - **L1a 时戳预测**：以最近 K 个间隔的中位预测下一帧到达，提前 lead 醒来
     拾帧；新帧未到时按子策略「等到截止 D」或「立即用旧帧」；
   - **L1b 事件唤醒上界**：到达即拾（预测零误差的理想上界）。
3. 判据表：拾帧龄 p50/p90/p99、提前醒来率（读到旧帧）、拍间隔违约率（>55ms）、
   错过目标帧后的恢复（最大连续拍间隔）。**预测误差压不到 ~2ms 就不谈 L1a
   收益**；L1b 是确定性上界，作为对照锚。

**为什么必须先录真实序列**：消费帧间隔（trace 可见）只是被 50ms 网格采样过的
子序列，且可能有跳帧；到达节奏只有采集回调层有全貌。录制用生产同款
WgcCapture（真实节流与回调路径），轮询 fid 变化记账（ts 来自回调时钟，零拷贝
不干扰节奏）。

**用法**::

    # ① 录制（游戏开着、驾驶页跑起来；~120s；本命令不碰游戏输入）
    .venv/Scripts/python.exe -I maaracing_master/plugins/_archive/speedrush/experiments/obs_pacing_replay/probe_arrival_l1.py record --seconds 120
    # 输出 arrival_seq_<时刻>.jsonl（每行一条接受帧：poll_t_ns / fid / accept_ts_ns）

    # ② 分析（离线，不依赖游戏）
    .venv/Scripts/python.exe -I maaracing_master/plugins/_archive/speedrush/experiments/obs_pacing_replay/probe_arrival_l1.py analyze arrival_seq_xxx.jsonl
    # 首行必须显示「退出节流 成功」，否则录制间隔受 EcoQoS 污染
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import statistics as st
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(REPO))

POLL_S = 0.0007          # fid 轮询间隔（< 60fps 最小间隔 16.7ms 的 1/20）
DEFAULT_BODY_MS = 12.5   # 拍体中位（trace 实测）；p90 敏感性场景用 30.0
GRID_MS = 50.0           # 控制拍网格
PRED_K = 8               # L1a 间隔预测窗（最近 K 个间隔取中位）
WAKE_LEAD_MS = 1.0       # L1a 提前醒的余量（覆盖轮询/调度延迟）
POLL_EPS_MS = 0.7        # L1b 上界的拾帧延迟（=轮询粒度）
VIOLATION_MS = 55.0      # 拍间隔违约线（名义 50 + 5 容差）


def disable_power_throttling() -> str:
    """退出 Windows 进程节能节流（EcoQoS），未退出的间隔数字作废。"""
    try:
        class _State(ctypes.Structure):
            _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG),
                        ("StateMask", wt.ULONG)]

        kernel32 = ctypes.windll.kernel32
        handle = ctypes.c_void_p(kernel32.GetCurrentProcess())
        before = _State(1, 0, 0)
        size = wt.DWORD(0)
        kernel32.GetProcessInformation(handle, 4, ctypes.byref(before),
                                       ctypes.sizeof(before), ctypes.byref(size))
        state = _State(1, 0x1, 0)
        ok = kernel32.SetProcessInformation(
            handle, 4, ctypes.byref(state), ctypes.sizeof(state))
        return (f"节流前 control={before.ControlMask} state={before.StateMask}；"
                f"退出节流 {'成功' if ok else '失败'}")
    except Exception as exc:  # noqa: BLE001
        return f"无法查询/设置节流：{exc!r}"


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * q / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _fmt(xs: list[float]) -> str:
    return (f"p50={pct(xs, 50):6.1f}  p90={pct(xs, 90):6.1f}"
            f"  p95={pct(xs, 95):6.1f}  p99={pct(xs, 99):6.1f}  max={max(xs):7.1f}")


# ── ① 录制 ──────────────────────────────────────────────────────────


def cmd_record(seconds: float, out: Path | None) -> int:
    from maaracing_master.core.game_process import find_window_by_process
    from maaracing_master.core.wgcap import WgcCapture

    print(disable_power_throttling())
    hwnd = find_window_by_process()
    if not hwnd:
        print("[失败] 没找到游戏窗口（游戏要先开起来）", file=sys.stderr)
        return 1
    cap = WgcCapture(hwnd, max_fps=60)
    cap.start()
    print(f"采集已挂到窗口 hwnd={hwnd}，轮询 {POLL_S*1000:.1f}ms，"
          f"录 {seconds:.0f}s——请在驾驶页保持行驶。")
    rows: list[dict] = []
    last_fid = -1
    t_end = time.perf_counter() + seconds
    t_start = time.perf_counter()
    interrupted = False
    try:
        while time.perf_counter() < t_end:
            _f, fid, ts_ns, _age = cap.get_latest()
            if fid != last_fid and fid > 0 and ts_ns:
                last_fid = fid
                rows.append({"fid": fid, "accept_ts_ns": ts_ns})
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        # Ctrl+C 也保存已录部分（缺 120s 全程时分析会提示样本是否够）
        interrupted = True
    finally:
        cap.stop()
    out = out or Path(__file__).resolve().parent / (
        "arrival_seq_" + time.strftime("%H%M%S") + ".jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    ivs = [(b["accept_ts_ns"] - a["accept_ts_ns"]) / 1e6
           for a, b in zip(rows, rows[1:])]
    span = (rows[-1]["accept_ts_ns"] - rows[0]["accept_ts_ns"]) / 1e9 if rows else 0
    print(f"{'被打断，已保存已录部分' if interrupted else '录制完成'}："
          f"{len(rows)} 帧 / 实际 {span:.0f}s → {out.name}")
    if len(rows) < 200 or span < 60:
        print("[提醒] 样本偏短：间隔分布的尾部（p90/p99）在短序列上不稳，"
              "建议游戏驾驶页跑满 120s 重录一遍。")
    if ivs:
        print(f"到达间隔 ms（全序列）: {_fmt(ivs)}")
    return 0


# ── ② 策略模拟 ──────────────────────────────────────────────────────


def load_seq(path: Path) -> list[float]:
    ts: list[float] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            ts.append(d["accept_ts_ns"] / 1e6)   # ms
    return ts


def sim_baseline(arr: list[float], body_ms: float) -> dict:
    """生产现状：50ms 固定网格，拍体 B 后睡到下一格；每格拾最新帧。"""
    ages: list[float] = []
    ivs: list[float] = []
    t = arr[0]                       # 相位从首帧起（非周期序列的网格相位即此）
    i = 0
    n = len(arr)
    while i < n - 2:
        t_next_grid = t + GRID_MS
        while i < n and arr[i] <= t_next_grid:
            i += 1
        i -= 1                       # arr[i] = 最新可用帧（≤拾取时刻）
        ages.append(t_next_grid - arr[i])
        ivs.append(GRID_MS)
        t = t_next_grid
        i += 1
    return {"ages": ages, "ivs": ivs, "early": 0.0}


def sim_predict(arr: list[float], body_ms: float, wait_deadline: bool) -> dict:
    """L1a：按最近 K 个**到达间隔**的中位预测下一帧，提前 lead 醒来拾帧。

    预测锚=最近**消费**的帧的到达时戳；每消费一个未消费过的帧就更新锚与
    间隔史（用旧帧=重复消费已消费帧，不更新）。新帧未到时：
    wait_deadline=True 等到截止 D（=GRID−body−2ms，期间帧到即拾）；
    False 立即重复消费旧帧。拍间隔=相邻拾取时刻差（违约率用）。
    """
    ages: list[float] = []
    ivs: list[float] = []
    early = 0
    n = len(arr)
    deadline_ms = GRID_MS - body_ms - 2.0
    t_prev_pick = arr[0] + POLL_EPS_MS
    last_picked = 0           # 最近消费帧的下标（预测锚）
    gaps: list[float] = []    # 相邻接受帧的到达间隔（预测史）
    while True:
        gap_pred = st.median(gaps[-PRED_K:]) if len(gaps) >= 3 else 25.2
        t_wake = arr[last_picked] + gap_pred - WAKE_LEAD_MS
        if t_wake < t_prev_pick + body_ms:
            t_wake = t_prev_pick + body_ms      # 拍体没跑完不起床
        # 醒来时刻最新可用帧 = 最后一个到达 ≤ t_wake 的帧
        m = last_picked + 1
        while m < n and arr[m] <= t_wake:
            m += 1
        m -= 1
        if m > last_picked:
            # 醒来即有新帧（预测命中提前=帧在睡眠期已到；命中延迟=晚醒）
            t_pick = t_wake + POLL_EPS_MS
            ages.append(t_pick - arr[m])
            ivs.append(t_pick - t_prev_pick)
            t_prev_pick = t_pick
            gaps.append(arr[m] - arr[last_picked])
            last_picked = m
        else:
            early += 1
            if wait_deadline:
                t_dead = t_wake + deadline_ms
                k = last_picked + 1
                while k < n and arr[k] <= t_dead:
                    k += 1
                k -= 1
                if k > last_picked:
                    # 截止前新帧到了：到帧即拾（轮询粒度延迟）
                    t_pick = arr[k] + POLL_EPS_MS
                    ages.append(t_pick - arr[k])
                    ivs.append(t_pick - t_prev_pick)
                    t_prev_pick = t_pick
                    gaps.append(arr[k] - arr[last_picked])
                    last_picked = k
                else:
                    # 截止仍无新帧：用旧帧收拍
                    t_pick = t_dead
                    ages.append(t_pick - arr[m])
                    ivs.append(t_pick - t_prev_pick)
                    t_prev_pick = t_pick
            else:
                # 立即用旧帧收拍（重复消费，锚不更新）
                t_pick = t_wake
                ages.append(t_pick - arr[m])
                ivs.append(t_pick - t_prev_pick)
                t_prev_pick = t_pick
        if last_picked >= n - 2:
            break
    return {"ages": ages, "ivs": ivs,
            "early": early / max(1, len(ages)) * 100.0}


def sim_event(arr: list[float]) -> dict:
    """L1b 上界：到达即拾（轮询粒度延迟）。"""
    ages = [POLL_EPS_MS] * (len(arr) - 2)
    ivs = [(b - a) for a, b in zip(arr, arr[1:len(arr) - 1])]
    return {"ages": ages, "ivs": ivs, "early": 0.0}


def report(name: str, r: dict) -> None:
    ages, ivs = r["ages"], r["ivs"]
    viol = sum(1 for v in ivs if v > VIOLATION_MS) / max(1, len(ivs)) * 100.0
    print(f"  {name:28s} 拾帧龄: {_fmt(ages)}")
    print(f"  {'':28s} 拍间隔: p50={pct(ivs, 50):5.1f} p90={pct(ivs, 90):5.1f}"
          f" p95={pct(ivs, 95):5.1f} max={max(ivs):6.1f}"
          f"  违约(>{VIOLATION_MS:.0f}ms)={viol:.2f}%  提前醒={r['early']:.1f}%")


def cmd_analyze(path: Path) -> int:
    arr = load_seq(path)
    if len(arr) < 100:
        print(f"[失败] 序列太短（{len(arr)} 帧），先跑 record", file=sys.stderr)
        return 1
    gaps = [(b - a) for a, b in zip(arr, arr[1:])]
    print(f"序列 {path.name}: {len(arr)} 帧 / {arr[-1]-arr[0]:.0f}ms")
    print(f"到达间隔 ms: {_fmt(gaps)}")
    print()
    print("三策略对照（拍体 B 用两个场景）:")
    for body in (DEFAULT_BODY_MS, 30.0):
        print(f" B={body:.1f}ms:")
        report("基线(50ms网格)", sim_baseline(arr, body))
        report("L1a预测+等截止", sim_predict(arr, body, True))
        report("L1a预测+用旧帧", sim_predict(arr, body, False))
        report("L1b事件唤醒上界", sim_event(arr))
    print()
    print("判据：L1a 提前醒率与拾帧龄 p90 显著劣于 L1b → 预测压不进 ~2ms，")
    print("L1a 收益不成立；L1b 是采集层事件唤醒的确定性上界。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--seconds", type=float, default=120.0)
    r.add_argument("--out", type=Path, default=None)
    a = sub.add_parser("analyze")
    a.add_argument("seq", type=Path)
    args = ap.parse_args()
    if args.cmd == "record":
        return cmd_record(args.seconds, args.out)
    return cmd_analyze(args.seq)


if __name__ == "__main__":
    sys.exit(main())
