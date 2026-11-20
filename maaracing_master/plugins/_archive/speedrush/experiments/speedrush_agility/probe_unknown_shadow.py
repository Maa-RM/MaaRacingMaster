"""横位未知态影子复算（speedrush，离线）。

**问什么**：真值小集（`probe_identity_truth.py`）证了「同号里一半链内横位
跳 >0.8 道」——真实横移单拍上限 ~0.43 道，这些拍读数必是并号/锚错挂，不是
车在动。给选择层加一道「横位未知」判据：命中车相邻拍横位跳超限（或纵深
跳/缺读数）时，该拍**弃车**（不作贴边目标，也不保持旧目标继续贴），下一拍
读数回到连续才重新有效。目标保持（已回退）在坏拍继续贴、把车看旧；未知态
在坏拍直接不贴——两者保守方向相反。

影子复算口径（与 `probe_x_freeze_shadow.py` 同尺子）：

- 只改「选择层看到的目标序列」，自车轨迹仍取录制值；只作筛选不作验收。
- unknown 判据：相邻拍 |Δx|>门（0.8 道，真实横移上限的 ~2 倍容差）或
  |Δz|>5 m 或本拍缺读数。unknown 拍从有效序列剔除：不计翻面、不进近距
  候选、不产生贴边拍。
- live 对照：无 unknown，全拍有效。

产出：贴边拍数、同车贴边目标翻面（相邻有效 pass/graze 拍 |Δtarget|>0.5）、
改侧拍、近距候选（可见框 zg≤5 m 横向≤0.54 道）最小横距、unknown 拍数与
其中 t<1 s 的拍数（机会代价面）。

用法::

    .venv/Scripts/python.exe -X utf8 probe_unknown_shadow.py <会话 ...>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402

LANE_W = 3.5
UNKNOWN_LANE = 0.8   # 相邻拍横位跳超过它 → 横位未知
UNKNOWN_Z = 5.0      # 相邻拍纵深跳超过它 → 纵深不可信，横位连带存疑


def _run(ticks, unknown_enabled: bool, unknown_lane: float = UNKNOWN_LANE) -> dict:
    prev_read: dict = {}   # (run, oid) -> (x, z)
    prev_eff: dict = {}    # (run, oid) -> 上一个有效目标
    flips = flips_u = side = 0
    active = skip = skip_near = 0
    near = []
    cut_near = []          # unknown 拍截掉的近距候选（本会进候选的被截）
    flips_cut = 0          # 翻面对里被 unknown 拍打断的个数
    cases = []             # 被截翻面案例（live 会翻面、unknown 拍截住）
    live_prev_eff: dict = {}
    for r in ticks:
        run = r.get("run_id")
        sd = r.get("sel_dbg") or {}
        if r.get("sel_why") not in ("pass", "graze") \
                or sd.get("target") is None or sd.get("hit_oid") is None:
            continue
        ego_l = ((r.get("scene") or {}).get("ego_m") or 0) / LANE_W
        oid = sd["hit_oid"]
        car = next((o for o in (r.get("scene") or {}).get("objs") or []
                    if o and len(o) > 14 and o[14] == oid and o[1] is not None),
                   None)
        x = None if car is None else car[1] / LANE_W
        z = None if car is None else car[7]
        t = None if car is None else car[2]
        tgt = sd["target"]
        pr = prev_read.get((run, oid))
        unknown = car is None or (pr is not None and (
            (x is not None and pr[0] is not None and abs(x - pr[0]) > unknown_lane)
            or (pr[1] is not None and z is not None and abs(z - pr[1]) > UNKNOWN_Z)
            or (pr[1] is not None and z is None)))
        prev_read[(run, oid)] = (x, z)
        # 近距候选（t<1 s 贴身窗内且 |x−ego|≤0.54 道）——两臂都量，横距取绝对值
        gap = None if x is None else abs(x - ego_l)
        is_near = t is not None and t < 1.0 and gap is not None and gap <= 0.54
        if unknown and unknown_enabled:
            skip += 1
            if t is not None and t < 1.0:
                skip_near += 1
            if is_near:
                cut_near.append(gap)
            # live 视角会翻面、被本 unknown 拍截住 → 记案例
            llast = live_prev_eff.get((run, oid))
            if llast is not None and abs(tgt - llast) > 0.5:
                flips_cut += 1
                cases.append({"fid": r.get("fid"), "oid": oid,
                              "x_prev": pr[0] if pr else None,
                              "x_cur": x,
                              "dx": None if (pr is None or x is None)
                              else round(x - pr[0], 2),
                              "tgt_prev": round(llast, 2),
                              "tgt": round(tgt, 2), "t": t,
                              "z_jump": None if (pr is None or pr[1] is None
                                                 or z is None)
                              else round(z - pr[1], 1)})
            live_prev_eff[(run, oid)] = tgt
            prev_eff.pop((run, oid), None)
            continue
        active += 1
        last = prev_eff.get((run, oid))
        if last is not None and abs(tgt - last) > 0.5:
            flips_u += 1
        prev_eff[(run, oid)] = tgt
        live_prev_eff[(run, oid)] = tgt
        if is_near:
            near.append(gap)
    return {"active": active, "skip": skip, "skip_near": skip_near,
            "flips": flips_u, "near": near, "cut_near": cut_near,
            "flips_cut": flips_cut, "cases": cases}


def _q(v, q):
    if not v:
        return float("nan")
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))]


def analyze(sess: str) -> None:
    ticks, stage_s = P.load(sess)
    print(f"== {sess}  阶段内 {stage_s:.0f}s")
    lv = _run(ticks, False)
    print(f"   live     有效贴边拍 {lv['active']:4d}  翻面 {lv['flips']:3d}"
          f"  近距候选 {len(lv['near']):3d}（P10 {_q(lv['near'], 0.1):.2f}"
          f" 最小 {min(lv['near'], default=float('nan')):.2f} 道）")
    for gate in (0.5, 0.6, 0.8, 1.0):
        un = _run(ticks, True, gate)
        print(f"   门{gate:.1f}   有效贴边拍 {un['active']:4d}（−{lv['active'] - un['active']})"
              f"  翻面 {un['flips']:3d}（被截对 {un['flips_cut']}）"
              f"  弃车 {un['skip']}（t<1s {un['skip_near']}）"
              f"  近距候选 {len(un['near']):3d}"
              f"（最小 {min(un['near'], default=float('nan')):.2f}）"
              f"  截掉近距 {len(un['cut_near'])}"
              f"（其横距 P10 {_q(un['cut_near'], 0.1):.2f}"
              f" 最小 {min(un['cut_near'], default=float('nan')):.2f}）")
    un = _run(ticks, True)
    print("   被截翻面案例（前 12 个；dx=相邻拍横位差，z_jump=纵深差）:")
    for c in un["cases"][:12]:
        print(f"     fid{c['fid']} oid{c['oid']} x {c['x_prev']}→{c['x_cur']}"
              f" (dx {c['dx']}) z跳 {c['z_jump']} t{c['t']}"
              f" 目标 {c['tgt_prev']}→{c['tgt']}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
