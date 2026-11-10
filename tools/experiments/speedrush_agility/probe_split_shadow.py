"""并号拆分的离线复算（speedrush，离线）。

**问什么**：真值小集（probe_identity_truth.py）发现 same 事件里一半链内
横位跳 >0.8 道（并号：一个编号先后挂两辆物理车）。拆号——横跳超限时把
一个轨道拆成两个稳定位置——能救回多少？拆完之后各簇是否各自稳定、判定
前 1.2 s 干净可用率能否从 44% 提上去。只复算不落地；数立得住才进生产。

拆号语义（复算口径）：

- 相邻读数（拍距 ≤ GAP_TICKS，超出是断号重挂的领域不是拆号）横位跳
  > SPLIT_X 道即拆点；轨道拆成两簇。
- 拆点按并存证据分型：拆点 ±TYPE_SPAN 拍内、新位置 COLOC 道内有**另一
  oid** 的读数（与旧位置可分）→ merge（两辆物理车，拆对了）；否则
  artifact（读数伪跳/换锚，拆了会把同一辆车改名——生产不应在 artifact
  拆，这类跳是未知态的领域）。注：被并的第二辆车若自身无编号，merge
  会漏判成 artifact（3 个目视并号样本可校准漏判率）。
- 拆后簇按构造无 >SPLIT_X 内跳；有信息量的是**每事件簇数**（2 = 干净
  二分；≥4 = 门太噪、链被打碎）。

1.2 s 干净可用率（拆后语义）：判定车身份链在 1.2 s 拍上有读数、且
1.2 s→判定之间没有 merge 边界（artifact 边界不换车、只是读数不可信；
merge 边界落在窗内 = 1.2 s 时编号挂的是另一辆车）。merge 边界在 1.2 s
**之前**的事件，拆后判定簇覆盖 1.2 s、读数干净 → 计 restored。

用法::

    .venv/Scripts/python.exe -X utf8 probe_split_shadow.py \
        --trace <speedrush.jsonl> [--trace ...] [--log <g112.log>]

--log 只在事件分析（需 120 事件对齐）时必填；scale 触发率统计不需要。
"""

from __future__ import annotations

import argparse
import bisect
import json
import os

import probe_identity_truth as pit

SPLIT_X = 0.8      # 拆号门（道），与真值小集的链内读数连续门一致
GAP_TICKS = 4      # 相邻读数最大拍距（同闪烁容忍）；超出按断号处理不拆
COLOC = 0.35       # 并存证据：另一 oid 读数与新位置的接近门（道）
TYPE_SPAN = 2      # 并存证据向前/后看的拍数
J12_S = 1.2        # 判定前回看秒数
J12_TOL_S = 0.15   # 1.2 s 拍对齐容差


def _seqs(cars_cache):
    """oid → [(tick, ent)] 时间升序。"""
    out = {}
    for i, cars in enumerate(cars_cache):
        for oid, ent in cars.items():
            out.setdefault(oid, []).append((i, ent))
    return out


def _split_seq(seq, thr=SPLIT_X):
    """读数序列按横位跳拆簇。返回 (clusters, boundaries)。

    拍距 > GAP_TICKS 的相邻读数对不比跳（断号重挂领域）；boundary 的
    i 为跳变读数所在拍。
    """
    clusters, boundaries = [], []
    cur = [seq[0]]
    for i, ent in seq[1:]:
        pi, pent = cur[-1]
        if i - pi > GAP_TICKS:
            clusters.append(cur)
            cur = [(i, ent)]
            continue
        dx = abs(ent["x"] - pent["x"])
        if dx > thr:
            boundaries.append({"i": i, "i_prev": pi,
                               "x_old": pent["x"], "x_new": ent["x"],
                               "dx": dx})
            clusters.append(cur)
            cur = [(i, ent)]
        else:
            cur.append((i, ent))
    clusters.append(cur)
    return clusters, boundaries


def _type_boundary(cars_cache, oid, b):
    """拆点分型：新位置近旁有另一 oid 的读数（与旧位置可分）→ merge。"""
    for k in range(max(b["i"] - TYPE_SPAN, 0),
                   min(b["i"] + TYPE_SPAN + 1, len(cars_cache))):
        for o2, ent2 in cars_cache[k].items():
            if o2 == oid:
                continue
            if (abs(ent2["x"] - b["x_new"]) <= COLOC
                    and abs(ent2["x"] - b["x_old"]) > COLOC):
                return "merge", o2, k
    return "artifact", None, None


def _max_jump(cluster):
    best = 0.0
    for a, b2 in zip(cluster, cluster[1:]):
        best = max(best, abs(b2[1]["x"] - a[1]["x"]))
    return best


def _chain_windows(row, ticks, ts_list):
    """事件窗 [j12, j] 与两界拍索引；对不上返回 None。"""
    j = bisect.bisect_left(ts_list, row["judge_ts"])
    t12 = ts_list[j] - int(J12_S * 1e9)
    j12 = pit._nearest(ticks, ts_list, t12)
    if j12 is None or abs(ts_list[j12] - t12) > int(J12_TOL_S * 1e9):
        return None
    return j, j12


def analyze_events(rows, res_by_session):
    """same 事件的拆后复算。res_by_session: session → (ticks, ts, cache, seqs)。"""
    out = []
    for r in rows:
        if r.get("cls") != "same":
            continue
        res = res_by_session.get(r["session"])
        if res is None:
            continue
        ticks, ts_list, cars_cache, seqs = res
        win = _chain_windows(r, ticks, ts_list)
        if win is None:
            out.append(dict(r, verdict="no_j12"))
            continue
        j, j12 = win
        chain = list(reversed(r["segs"]))  # 时间升序
        merges, artifacts, n_clusters = [], [], 0
        for s in chain:
            seq = [(i, e) for i, e in seqs.get(s["oid"], [])
                   if j12 - TYPE_SPAN <= i <= j]
            if len(seq) < 2:
                continue
            clusters, bounds = _split_seq(seq)
            n_clusters += len(clusters)
            for b in bounds:
                typ, co, _ = _type_boundary(cars_cache, s["oid"], b)
                b2 = dict(b, type=typ, co_oid=co, seg_oid=s["oid"])
                (merges if typ == "merge" else artifacts).append(b2)
        covered = any(abs(i - j12) <= TYPE_SPAN
                      for s in chain for i, _ in seqs.get(s["oid"], []))
        if not covered:
            out.append(dict(r, verdict="no_cover", merges=merges,
                            artifacts=artifacts))
            continue
        if not merges:
            # 窗内无 merge 边界：身份在 1.2 s 处是判定车（artifact 伪跳
            # 只是读数不可信，拆号不管）
            out.append(dict(r, verdict="identity_ok", merges=merges,
                            artifacts=artifacts, n_clusters=n_clusters))
            continue
        # merge 边界在窗内：判定车 1.2 s 处是否经并存编号（第二辆车的
        # 原号）干净可用——拆号+去重的真实恢复路径
        via = []
        for m in merges:
            if m["i"] <= j12:
                continue          # 边界在 1.2 s 前：判定簇直接覆盖 1.2 s
            co = m.get("co_oid")
            if co is None:
                continue
            seq_co = [(i, e) for i, e in seqs.get(co, [])
                      if j12 <= i <= m["i"]]
            if not any(abs(i - j12) <= TYPE_SPAN for i, _ in seq_co):
                continue
            if len(seq_co) >= 2 and _max_jump(seq_co) > SPLIT_X:
                continue
            via.append((m, co))
        if via or any(m["i"] <= j12 for m in merges):
            out.append(dict(r, verdict="identity_ok_via_co", merges=merges,
                            artifacts=artifacts, via_co=via,
                            n_clusters=n_clusters))
        else:
            out.append(dict(r, verdict="merge_in_window", merges=merges,
                            artifacts=artifacts, n_clusters=n_clusters))
    return out


def _chain_max_xjump_row(r, seqs):
    """链内最大相邻横位跳（道），全链口径（真值小集 44% 的门）。"""
    best = 0.0
    for s in r["segs"]:
        seq = seqs.get(s["oid"], [])
        vals = [e["x"] for i, e in seq
                if s["i_from"] <= i <= s["i_to"]]
        for a, b2 in zip(vals, vals[1:]):
            best = max(best, abs(b2 - a))
    return best


def scale_pass(trace_path):
    """单 trace 全量拆号触发率与分型（不裁头尾，速率是粗估）。"""
    ticks = pit._load(trace_path)
    if not ticks:
        return None
    ts = [r["ts_wall_ns"] for r in ticks]
    cars_cache = [pit._cars(r) for r in ticks]
    seqs = _seqs(cars_cache)
    dur_min = (ts[-1] - ts[0]) / 1e9 / 60
    n_split = n_merge = n_art = 0
    frag = []          # 每条序列簇数（≥2 次拆点的）
    for oid, seq in seqs.items():
        if len(seq) < 3:
            continue
        clusters, bounds = _split_seq(seq)
        if bounds:
            frag.append(len(clusters))
        for b in bounds:
            typ, _, _ = _type_boundary(cars_cache, oid, b)
            n_split += 1
            n_merge += typ == "merge"
            n_art += typ == "artifact"
    return {"trace": os.path.basename(trace_path), "dur_min": dur_min,
            "splits": n_split, "per_min": n_split / dur_min if dur_min else 0,
            "merge": n_merge, "artifact": n_art,
            "frag_p50": _p50(frag), "frag_max": max(frag) if frag else 0}


def _p50(vals):
    if not vals:
        return 0
    s = sorted(vals)
    return s[len(s) // 2]


def _print_event_report(evs, res):
    """res: session → (ticks, ts, cache, seqs)，逐事件按会话取读数表。"""
    same_all = [e for e in evs if e.get("cls") == "same"]
    base_whole = [e for e in same_all
                  if _chain_max_xjump_row(e, res[e["session"]][3]) <= SPLIT_X]
    iok = [e for e in evs if e.get("verdict") == "identity_ok"]
    iok_co = [e for e in evs if e.get("verdict") == "identity_ok_via_co"]
    miw = [e for e in evs if e.get("verdict") == "merge_in_window"]
    nocov = [e for e in evs if e.get("verdict") == "no_cover"]
    noj12 = [e for e in evs if e.get("verdict") == "no_j12"]
    n = len(same_all)
    print(f"== same 事件 {n} 个")
    print(f"   基线（全链横跳≤{SPLIT_X}，真值小集口径）: "
          f"{len(base_whole)}/{n} = {len(base_whole)/n*100:.0f}%")
    print(f"   拆后身份可用（窗内无 merge 边界 或 经并存编号接回）: "
          f"{len(iok) + len(iok_co)}/{n} = {(len(iok)+len(iok_co))/n*100:.0f}%")
    art_only = [e for e in iok
                if _chain_max_xjump_row(e, res[e["session"]][3]) > SPLIT_X]
    print(f"     其中身份对但读数伪跳（拆号不管，未知态管）: {len(art_only)}")
    print(f"   merge 在窗内且并存编号不可用（1.2s 挂的是另一辆车）: {len(miw)}")
    print(f"   1.2s 无读数: {len(nocov)}   窗对不上: {len(noj12)}")
    print()
    print("== 脏事件（基线不干净）拆点分型")
    dirty = [e for e in same_all
             if _chain_max_xjump_row(e, res[e["session"]][3]) > SPLIT_X]
    n_m = sum(len(e.get("merges") or []) for e in dirty)
    n_a = sum(len(e.get("artifacts") or []) for e in dirty)
    print(f"   脏事件 {len(dirty)} 个；窗内拆点 merge {n_m} / artifact {n_a}")
    frag = [e.get("n_clusters") for e in dirty if e.get("n_clusters")]
    if frag:
        print(f"   拆后每事件簇数: P50 {_p50(frag)}  max {max(frag)}"
              f"  （2=干净二分，≥4=门太噪）")
    for e in dirty:
        ms = ",".join(f"{m['type']}@fid_idx{m['i']}(dx{m['dx']:.2f}"
                      f"{',co' + str(m['co_oid']) if m.get('co_oid') else ''})"
                      for m in (e.get("merges") or []))
        as_ = ",".join(f"a@{a['i']}(dx{a['dx']:.2f})"
                       for a in (e.get("artifacts") or []))
        vc = ",".join(f"co{co}@{m['i']}" for m, co in (e.get("via_co") or []))
        print(f"   {e['win']} fid{e['judge_fid']} oid{e['oid']}"
              f"  {e['verdict']}  簇{e.get('n_clusters', '-')}"
              f"  {ms}{'  ' + as_ if as_ else ''}"
              f"{'  经并存:' + vc if vc else ''}")


def _print_scale_report(rs):
    print("== 拆号触发率（全量粗估，不裁头尾）")
    for r in rs:
        if r is None:
            continue
        print(f"   {r['trace']}: {r['dur_min']:.0f}min  拆点 {r['splits']}"
              f"（{r['per_min']:.1f}/min）  merge {r['merge']}"
              f"  artifact {r['artifact']}"
              f"  序列簇数 P50 {r['frag_p50']} max {r['frag_max']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", required=True)
    ap.add_argument("--log", default=os.environ.get("MAAR_G112_LOG"))
    ap.add_argument("--debug-root",
                    default=os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug"))
    ap.add_argument("--json")
    a = ap.parse_args()
    scale = [scale_pass(tp) for tp in a.trace]
    _print_scale_report(scale)
    evs = []
    if a.log:
        rows = pit.analyze(a.trace, a.log, a.debug_root)
        res = {}
        for tp in a.trace:
            ticks = pit._load(tp)
            session = ticks[0].get("session")
            cache = [pit._cars(r) for r in ticks]
            res[session] = (ticks, [r["ts_wall_ns"] for r in ticks],
                            cache, _seqs(cache))
        evs = analyze_events(rows, res)
        print()
        _print_event_report(evs, res)
    else:
        print("\n[未传 --log] 只跑 scale 触发率，事件分析跳过")
    if a.json:
        os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"scale": scale, "events": evs}, fh,
                      ensure_ascii=False, indent=1)
        print(f"\n[json] {a.json}")
