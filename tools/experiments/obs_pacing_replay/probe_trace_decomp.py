#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""观测链消费时序分解（纯 trace 解析，不跑模型、不依赖实机）。

**要回答什么**
1. 复算「观测结果被规划器读取时的内容龄」两个口径，按会话×阶段分桶（不混桶）：
   - 口径一（新结果首次消费龄）：每个新观测结果第一次被读到的龄；
   - 口径二（每拍实际使用龄）：每一拍控制链实际吃到的观测内容龄（含复用）。
2. 把线观测与深度的内容龄拆成段（采集→提交 / 排队 / 处理本体 / 发布→读取），
   定位「多等一拍」发生在哪一段。

**口径与数据面**
- 口径一/二都直接取控制快照的 ``obs_age``（控制链同拍记录的各源 fid/采集戳/龄）：
  线观测按检测输入源戳去重（``obs_age.lane.ts_ns`` 变新即一次新消费），
  深度新结果用 ``obs_age.depth.ts_ns`` 去重、另用 ``dgeo_new`` 交叉核对。
- 线观测段账来自线快照自带计时（语义见 lane_obs._snapshot）：
  ``cap_ms``=采集→提交、``queue_ms``=排队→开工、``ms``=处理本体；
  发布龄=三者之和，发布→读取 = 读取龄 − 发布龄。
- 深度段账来自 ``dgeo_seg``：``cap_ms``（含 det 段）=采集→提交、``queue_ms``
  =排队→开工、``stages`` 合计=处理本体（含内部槽位等待）、``pub_ms``=发布段。
- 达标线：YOLO/线观测 ≤50ms（一拍），深度 ≤100ms（两拍）。缺席拍不计入
  达标分母——比例不代表供数覆盖率。
- 帧间隔分位数附在最后，作为拍长的实测参照。

**为什么只读 trace**：这些字段全部由生产代码在生产节拍下写入，比任何离线
复刻更接近真相；本脚本是回放探针的标尺来源——回放的基线变体必须先复现
本脚本的数字，回放结论才算有效。

用法::

    .venv/Scripts/python.exe tools/experiments/obs_pacing_replay/probe_trace_decomp.py \
        "%APPDATA%/MaaRacingMaster/logs/<会话编号>" \
        "%APPDATA%/MaaRacingMaster/logs/<会话编号>" \
        "%APPDATA%/MaaRacingMaster/logs/<会话编号>"
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

# ── 分位数与直方 ────────────────────────────────────────────────────


def pct(xs: list[float], q: float) -> float | None:
    """线性插值分位数；空序列返回 None。"""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def fmt3(xs: list[float]) -> str:
    if not xs:
        return "  n=0"
    return (f"n={len(xs):5d}  p50={pct(xs, 50):6.1f}  p90={pct(xs, 90):6.1f}"
            f"  p95={pct(xs, 95):6.1f}  p99={pct(xs, 99):6.1f}"
            f"  max={max(xs):6.1f}")


def ratio(xs: list[float], cap: float) -> str:
    if not xs:
        return "  n=0"
    ok = sum(1 for v in xs if v <= cap)
    return f"{ok / len(xs) * 100:5.1f}% ({ok}/{len(xs)})"


# ── trace 解析 ──────────────────────────────────────────────────────


def parse_stage(path: Path, out: dict) -> None:
    """把一个会话的 speedrush.jsonl 按阶段分组解析进 out[(session, stage)]。

    每阶段收集：obs_age 三源的口径一/二样本、线快照段账、深度分段账、帧间隔。
    """
    session = path.parent.parent.name
    stage = None
    last_ts_ns = None
    seen_lane_ts = None      # 线观测：最近一次消费的检测输入源戳（去重键）
    seen_depth_ts = None     # 深度：最近一次消费的结果源戳（去重键）
    seen_yolo_ts = None
    n_lines = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            typ = d.get("type")
            if typ == "stage_begin":
                stage = d.get("stage_id")
                last_ts_ns = None
                seen_lane_ts = seen_depth_ts = seen_yolo_ts = None
                continue
            if typ is not None:      # session_begin / stage_end / 其他事件行
                continue
            if "fid" not in d:       # 每拍流水行必有 fid
                continue
            n_lines += 1
            g = out.setdefault((session, stage), {
                "beats": 0,
                "yolo_1": [], "yolo_2": [],
                "lane_1": [], "lane_2": [],
                "depth_1": [], "depth_2": [],
                "depth_1_dnew": [],   # dgeo_new 交叉核对
                "lane_seg": [],       # (cap, queue, ms, pub_age, gap, read_age)
                "depth_seg": [],      # (cap, queue, body残差, pub_ms, read_age)
                "frame_iv": [],       # 帧间隔 ms（拍长参照）
                **{f"dgeo_stage_{k}": [] for k in
                   ("pre", "slot_wait", "forward", "points", "plane",
                    "edges", "grid")},
            })
            g["beats"] += 1

            ts_ns = d.get("ts_ns")
            if ts_ns and last_ts_ns:
                iv = (ts_ns - last_ts_ns) / 1e6
                if 0 < iv < 2000:
                    g["frame_iv"].append(iv)
            if ts_ns:
                last_ts_ns = ts_ns

            oa = d.get("obs_age") or {}
            for src, l1, l2 in (("yolo", "yolo_1", "yolo_2"),
                                ("lane", "lane_1", "lane_2"),
                                ("depth", "depth_1", "depth_2")):
                o = oa.get(src) or {}
                a = o.get("age_ms")
                if a is not None:
                    g[l2].append(a)
            seen_yolo_ts = _dedup(oa, "yolo", seen_yolo_ts, "yolo_1", g)
            seen_lane_ts = _dedup(oa, "lane", seen_lane_ts, "lane_1", g)
            seen_depth_ts = _dedup(oa, "depth", seen_depth_ts, "depth_1", g)

            lo = d.get("lane_obs")
            # 段账只收「本拍读到的源戳=本拍快照的检测输入」的行——这样
            # 发布龄与读取龄同帧对齐，发布→读取 = 读取龄 − 发布龄。
            oa_lane = (oa.get("lane") or {})
            if (lo and lo.get("cap_ms") is not None and lo.get("ms") is not None
                    and oa_lane.get("ts_ns")
                    and oa_lane["ts_ns"] == lo.get("det_ts_ns")
                    and oa_lane.get("age_ms") is not None):
                pub = (lo["cap_ms"] + (lo.get("queue_ms") or 0.0) + lo["ms"])
                g["lane_seg"].append((
                    lo["cap_ms"], lo.get("queue_ms") or 0.0, lo["ms"],
                    pub, oa_lane["age_ms"] - pub, oa_lane["age_ms"]))

            ds = d.get("dgeo_seg")
            oa_d = oa.get("depth") or {}
            if ds and d.get("dgeo_new") and oa_d.get("age_ms") is not None:
                stages = ds.get("stages") or {}
                # 栅格与找边并行（depth_geo 找边/栅格双线程），stages 求和
                # 虚增墙钟；本体墙钟用残差：读取龄 − cap − queue − pub。
                cap = ds.get("cap_ms") or 0.0
                que = ds.get("queue_ms") or 0.0
                pub = ds.get("pub_ms") or 0.0
                body = max(0.0, oa_d["age_ms"] - cap - que - pub)
                g["depth_seg"].append((cap, que, body, pub, oa_d["age_ms"]))
                for k in ("pre", "slot_wait", "forward", "points",
                          "plane", "edges", "grid"):
                    v = stages.get(k)
                    if isinstance(v, (int, float)):
                        g["dgeo_stage_" + k].append(v)
                if d.get("dgeo_age_ms") is not None:
                    g["depth_1_dnew"].append(d["dgeo_age_ms"])


def _dedup(oa: dict, src: str, seen, key_1: str, g: dict):
    """obs_age 源戳去重：源戳变新 = 该结果第一次被读到；返回新 seen。"""
    o = oa.get(src) or {}
    t = o.get("ts_ns")
    a = o.get("age_ms")
    if a is None:
        return seen
    if t is not None and t != seen:
        g[key_1].append(a)
        return t
    return seen


# ── 报表 ────────────────────────────────────────────────────────────

HEADER = (
    "口径一=新结果首次消费龄（按源戳去重）；口径二=每拍实际使用龄（含复用）。\n"
    "达标线：YOLO/线 ≤50ms；深度 ≤100ms。缺席拍不计入达标分母。")


def report(out: dict) -> None:
    print(HEADER)
    print()
    # 按 (阶段内会话序, stage) 排序输出
    stages = sorted(out.keys(), key=lambda k: (k[0], k[1] if k[1] is not None else -1))
    for key in stages:
        g = out[key]
        sess, st = key
        print(f"── {sess} / 阶段{st}（{g['beats']} 拍，帧间隔 "
              f"p50={pct(g['frame_iv'], 50) and round(pct(g['frame_iv'], 50), 1)}"
              f" p95={pct(g['frame_iv'], 95) and round(pct(g['frame_iv'], 95), 1)}ms）")
        print(f"  YOLO   口径一: {fmt3(g['yolo_1'])}  ≤50ms {ratio(g['yolo_1'], 50)}")
        print(f"         口径二: {fmt3(g['yolo_2'])}")
        print(f"  线观测 口径一: {fmt3(g['lane_1'])}  ≤50ms {ratio(g['lane_1'], 50)}")
        print(f"         口径二: {fmt3(g['lane_2'])}")
        print(f"  深度   口径一: {fmt3(g['depth_1'])}  ≤100ms {ratio(g['depth_1'], 100)}"
              f"   (dgeo_new 核对: {fmt3(g['depth_1_dnew'])})")
        print(f"         口径二: {fmt3(g['depth_2'])}  ≤100ms {ratio(g['depth_2'], 100)}")
        # 线观测段账（同帧对齐行：cap+queue+ms=发布龄，gap=读取龄−发布龄）
        segs = g["lane_seg"]
        if segs:
            cap = [s[0] for s in segs]
            que = [s[1] for s in segs]
            ms = [s[2] for s in segs]
            pub = [s[3] for s in segs]
            gap = [s[4] for s in segs]
            print(f"  线段账 cap(采集→提交): {fmt3(cap)}")
            print(f"         queue(排队→开工): {fmt3(que)}")
            print(f"         ms(处理本体): {fmt3(ms)}")
            print(f"         发布龄(cap+queue+ms): {fmt3(pub)}")
            print(f"         发布→读取(gap): {fmt3(gap)}")
        dsegs = g["depth_seg"]
        if dsegs:
            cap = [s[0] for s in dsegs]
            que = [s[1] for s in dsegs]
            body = [s[2] for s in dsegs]
            pub = [s[3] for s in dsegs]
            print(f"  深段账 cap(采集→提交): {fmt3(cap)}")
            print(f"         queue(排队→开工): {fmt3(que)}")
            print(f"         本体墙钟(读取龄-cap-queue-pub): {fmt3(body)}")
            print(f"         pub(发布→读取): {fmt3(pub)}")
            det = "  ".join(
                f"{k} p50={pct(g['dgeo_stage_' + k], 50):.1f}/p95={pct(g['dgeo_stage_' + k], 95):.1f}"
                for k in ("slot_wait", "forward", "points", "plane", "edges", "grid")
                if g.get("dgeo_stage_" + k))
            if det:
                print(f"         分段(ms p50/p95): {det}")
        print()


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    out: dict = {}
    for a in args:
        p = Path(a) / "trace" / "speedrush.jsonl"
        if not p.exists():
            print(f"[跳过] {p} 不存在", file=sys.stderr)
            continue
        parse_stage(p, out)
    report(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
