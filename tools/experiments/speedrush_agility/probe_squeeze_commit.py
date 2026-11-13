"""灾难链两条行为修复的离线复算（speedrush，离线）。

**问什么**（`docs/plan/speedrush_提分_机动尺度与躲避语义.md`「压缩后必读」
剩余杠杆 2/3，帧证据 fid 8663~8722）：

1. **贴边最小间距**：夹取后贴边目标只剩 0.6~0.68 道（设计偏移 0.68、
   最小间距 0.6）时，当前按「最小间距也走」放行；交付误差 0.3~0.5 道把
   实际横距压到贴脸。拟改：偏移 <0.68 不放行（换侧/弃车）。本节回答：
   挤压贴边拍有多少、它们之后 1.5 s 内进可见框近距事件的比例是否显著
   高于设计偏移贴边拍——不显著则这刀没有依据。
2. **路线承诺校验**：并线承诺了规划器没在走的道（选择目标 −1.04 道、
   计划终点只到约 0.3 道），1.2 s 后临门撤销、再以 0.02 s TTC 反打。
   拟改：承诺前确认计划终点贴住承诺道（|plan 终点−承诺道| ≤0.5 道），
   否则不承诺。本节回答：计划终点落后的承诺段，其撤销/反打率是否高于
   贴住的承诺段。

口径：

- 贴边拍 = sel_why ∈ (pass, graze) 且 sel_dbg.target / hit_oid 在场；
  偏移 = |target − 车心横位|（场景表同 oid）。车心读数跳 >0.3 道的拍
  不采信（近场横位噪声，第九轮口径）。
- 交付结果 = 起拍后 1.5 s 内、同 oid 且车心读数连续（±0.3 道）的最小
  |执行横位 − 车心横位|；<0.54 道（两车半宽和）记贴脸，<0.44 记严重。
- 承诺段 = route.vd==merge_start 到 finish/cancel/timeout；计划终点落后
  = 并线拍上 |plan.x/3.5 − route.tgt| 的中位 >0.5 道（≥3 个有效拍）。
- 临门撤销 = 撤销拍 route.tc < 0.3 s；反打 = 撤销后 1 s 内反向 merge_start。

只作筛选，不作验收（自车轨迹没闭环）。

用法::

    .venv/Scripts/python.exe -X utf8 probe_squeeze_commit.py <会话 ...>
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402

LANE_W = 3.5
DESIGN_LANE = 0.68      # 贴边设计偏移（graze_gap_lane）
MIN_GAP_LANE = 0.60     # 当前最小间距门（pass_min_gap_lane）
HALF_LANE = 0.54        # 两车半宽和 = 贴脸边界
SEVERE_LANE = 0.44      # 真机 0.04 道正面灾难量级
X_NOISE = 0.3           # 车心读数连续门（第九轮：近场横位噪声）
DELIVER_S = 1.5
COMMIT_GAP = 0.5        # 计划终点落后承诺道的判据
NEAR_M = 5.0
LATE_TC = 0.3           # 临门撤销：撤销拍源道到站 <0.3 s


def _car_x_lane(r, oid):
    for o in (r.get("scene") or {}).get("objs") or []:
        if o and len(o) > 14 and o[14] == oid and o[1] is not None:
            return o[1] / LANE_W
    return None


def _min_delivery(ticks, idx, run, oid, x0):
    """起拍后 DELIVER_S 内、同 oid 读数连续的最小 |exec − 车心|（道）。"""
    best = None
    t0 = ticks[idx]["ts_ns"]
    for j in range(idx + 1, min(idx + 200, len(ticks))):
        r = ticks[j]
        if r.get("run_id") != run:
            break
        if (r["ts_ns"] - t0) / 1e9 > DELIVER_S:
            break
        if r.get("sel_why") not in ("pass", "graze"):
            continue
        x = _car_x_lane(r, oid)
        if x is None or abs(x - x0) > X_NOISE:
            continue
        ex = (r.get("lat_ctrl") or {}).get("exec")
        if ex is None:
            continue
        d = abs(ex - x)
        if best is None or d < best:
            best = d
    return best


def analyze_squeeze(ticks) -> dict:
    """贴边拍按偏移分档，量交付贴脸率。"""
    classes = ("design", "squeezed", "under")
    stats = {c: {"n": 0, "deliv": [], "near": 0} for c in classes}
    for i, r in enumerate(ticks):
        sd = r.get("sel_dbg") or {}
        if r.get("sel_why") not in ("pass", "graze") \
                or sd.get("target") is None or sd.get("hit_oid") is None:
            continue
        x = _car_x_lane(r, sd["hit_oid"])
        if x is None:
            continue
        off = abs(sd["target"] - x)
        if off >= DESIGN_LANE - 0.01:
            cls = "design"
        elif off >= MIN_GAP_LANE - 0.01:
            cls = "squeezed"
        else:
            cls = "under"
        ego = (r.get("scene") or {}).get("ego_m")
        s = stats[cls]
        s["n"] += 1
        # 当拍就贴身的候选（选择层已经把目标放到了贴脸位）
        if ego is not None and abs(x - ego / LANE_W) <= HALF_LANE:
            s["near"] += 1
        d = _min_delivery(ticks, i, r.get("run_id"), sd["hit_oid"], x)
        if d is not None:
            s["deliv"].append(d)
    return stats


def _episodes(ticks):
    """承诺段 + 每段的计划终点落后中位、撤销拍 tc、反打标记。"""
    eps, cur = [], None
    recs = list(enumerate(ticks))
    for i, r in recs:
        rt = r.get("route") or {}
        vd = rt.get("vd")
        if vd == "merge_start":
            if cur is not None:
                cur["end"] = "restart"
                eps.append(cur)
            cur = {"run": r.get("run_id"), "i0": i, "r0": r,
                   "gaps": [], "end": None, "i_end": None}
        elif vd in ("finish", "cancel", "timeout") and cur is not None:
            cur["end"] = vd
            cur["i_end"] = i
            eps.append(cur)
            cur = None
        if cur is not None and vd != "merge_start":
            px = (r.get("plan") or {}).get("x") \
                if isinstance(r.get("plan"), dict) else None
            tgt = rt.get("tgt")
            if px is not None and tgt is not None:
                cur["gaps"].append(abs(px / LANE_W - tgt))
    if cur is not None:
        cur["end"] = "eof"
        eps.append(cur)
    # 反打：撤销后 1 s 内反向 merge_start
    for k, ep in enumerate(eps):
        ep["tc_end"] = None
        if ep["i_end"] is not None:
            ep["tc_end"] = (ticks[ep["i_end"]].get("route") or {}).get("tc")
        ep["reverse"] = False
        if ep["end"] in ("cancel", "timeout") and k + 1 < len(eps):
            nxt = eps[k + 1]
            if nxt["run"] == ep["run"] and nxt["r0"] is not None:
                dt = (nxt["r0"]["ts_ns"]
                      - ticks[ep["i_end"]]["ts_ns"]) / 1e9 \
                    if ep["i_end"] is not None else 99.0
                t0 = (ep["r0"].get("route") or {}).get("tgt")
                t1 = (nxt["r0"].get("route") or {}).get("tgt")
                if dt <= 1.0 and t0 is not None and t1 is not None \
                        and t0 * t1 < 0:
                    ep["reverse"] = True
        g = sorted(ep["gaps"])
        ep["lag"] = g[len(g) // 2] if len(g) >= 3 else None
    return eps


def analyze_commit(eps) -> dict:
    """承诺段按计划终点落后分两臂，量撤销/反打/临门。"""
    out = {"lag": {"n": 0, "cancel": 0, "late": 0, "rev": 0, "finish": 0},
           "ok": {"n": 0, "cancel": 0, "late": 0, "rev": 0, "finish": 0},
           "noc": 0}
    for ep in eps:
        if ep["lag"] is None:
            out["noc"] += 1
            continue
        arm = out["lag"] if ep["lag"] > COMMIT_GAP else out["ok"]
        arm["n"] += 1
        if ep["end"] == "finish":
            arm["finish"] += 1
        elif ep["end"] in ("cancel", "timeout"):
            arm["cancel"] += 1
            if ep["tc_end"] is not None and ep["tc_end"] < LATE_TC:
                arm["late"] += 1
        if ep["reverse"]:
            arm["rev"] += 1
    return out


def _pct(a, b):
    return f"{a / b * 100:.0f}%" if b else "—"


def _med(v):
    if not v:
        return float("nan")
    v = sorted(v)
    return v[len(v) // 2]


def analyze(sess: str) -> None:
    ticks, stage_s = P.load(sess)
    print(f"== {sess}  阶段内 {stage_s:.0f}s  拍 {len(ticks)}")
    sq = analyze_squeeze(ticks)
    print("   [贴边最小间距] 偏移分档 → 拍数 / 当拍贴身 / 交付中位 /"
          " 贴脸<0.54 / 严重<0.44")
    for cls, label in (("design", f"≥{DESIGN_LANE}（设计）"),
                       ("squeezed", f"{MIN_GAP_LANE}~{DESIGN_LANE}（挤压）"),
                       ("under", f"<{MIN_GAP_LANE}（越门）")):
        s = sq[cls]
        if not s["n"] and not s["deliv"]:
            print(f"     {label:16s} 0 拍")
            continue
        dv = s["deliv"]
        print(f"     {label:16s} {s['n']:4d} 拍  当拍贴身 {s['near']:3d}"
              f"  交付 n={len(dv):4d} 中位 {_med(dv):.2f}"
              f"  贴脸 {sum(1 for d in dv if d < HALF_LANE)}"
              f"（{_pct(sum(1 for d in dv if d < HALF_LANE), len(dv))}）"
              f"  严重 {sum(1 for d in dv if d < SEVERE_LANE)}")
    eps = _episodes(ticks)
    cm = analyze_commit(eps)
    print("   [路线承诺校验] 计划终点落后 → 段数 / 完成 / 撤销 /"
          " 临门撤销(tc<0.3) / 反打")
    for arm, label in (("lag", f"落后>{COMMIT_GAP} 道"),
                       ("ok", f"贴住≤{COMMIT_GAP} 道")):
        a = cm[arm]
        print(f"     {label:12s} {a['n']:3d} 段  完成 {a['finish']:3d}"
              f"  撤销 {a['cancel']:3d}  临门 {a['late']:3d}"
              f"  反打 {a['rev']:3d}")
    if cm["noc"]:
        print(f"     （{cm['noc']} 段计划终点读数不足 3 拍，未分臂）")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
