"""横幅事件轨迹分型探针（speedrush，离线）。

**问什么**：游戏判定的「极限超车 / 极限变道」事件，在判定那一刻自车与被判车
的车心横距是多少（近了还是远了）？这段接近是**自车主动**贴上去的，还是
**对方车**主动靠过来的（主动/被动）？

判定口径按 `RULES.md` §4.2.1：

- 超车判定在被超车从车头前方 20 m 触发框里掉出去（到站时间过零）那一刻；
  横向中心距 < 两车半宽和 + 0.8 m（≈0.77 道）判极限超车，否则普通超车。
- 极限变道判的是「车头前方 0~10 m、横向半车宽」的候选，自车从它旁边让开
  （候选车道 = 出发车道），不是切进去。

探针在横幅出现时刻附近找「刚被甩到身后」的那台车（贴到鼻尖的最后一拍），
取判定拍横距、接近速度与自车横速：

- **主动/被动（纵向）**：接近速度（`mcars.v`，正=本车在追上它）>2 m/s 主动，
  <-2 被动。**不用对方车的横位变化判主动/被动**——街车不变道，而 `scene`
  里同一台车在 10~15 m 的横位几拍内会跳 1~7 道（身份/锚点错挂）。
- 自车横速只用来描述本车自己的打舵方向，不参与「谁动的」归因。

横幅是 HUD 0.5 s 采样，判定时刻可能晚 0.3~0.5 s；探针在 ±0.8 s 内取最近的一拍。

用法::

    .venv/Scripts/python.exe -X utf8 probe_banner_events.py <会话 ...>
"""

from __future__ import annotations

import bisect
import collections
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402

EXTREME_GAP = 0.77          # 两车半宽和 0.54 + 0.8 m ≈ 0.77 道
ACTIVE_DV = 2.0             # m/s：接近速超过它才算主动/被动


def _banners(sess: str) -> list[tuple[int, str]]:
    root = os.path.join(os.environ["APPDATA"], "MaaRacingMaster", "debug",
                        sess, "speedrush", "drive")
    raw = []
    for d in sorted(glob.glob(os.path.join(root, "*_p?"))):
        path = os.path.join(d, "hud", "hud.jsonl")
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            txt = ((rec.get("fields") or {}).get("event_banner")
                   or {}).get("text") or ""
            txt = txt.replace(" ", "")
            for kind in ("极限超车", "极限变道", "木桶旋转"):
                if kind in txt:
                    raw.append((rec["ts_ns"], kind))
                    break
    out = []
    for ts, kind in raw:
        if out and out[-1][1] == kind and ts - out[-1][0] < 2_000_000_000:
            continue
        out.append((ts, kind))
    return out


def _ticks(sess: str):
    path = os.path.join(os.environ["APPDATA"], "MaaRacingMaster", "logs",
                        sess, "trace", "speedrush.jsonl")
    out = []
    for line in open(path, encoding="utf-8"):
        if not line.startswith("{"):
            continue
        rec = json.loads(line)
        if rec.get("fid") is None:
            continue
        out.append(rec)
    out.sort(key=lambda r: r["ts_ns"])
    return out


def _cars(rec):
    """[(oid, x_m, t, z_m, src)]，跳过非车行。"""
    out = []
    for o in (rec.get("scene") or {}).get("objs") or []:
        if not o or o[0] != "car" or len(o) < 16:
            continue
        out.append((o[14], o[1], o[2], o[7], o[6]))
    return out


def _judged(ticks, idx, kind):
    """在横幅附近找判定车，返回 (tick 下标, oid, gap 道) 或 None。

    超车的判定拍取「该车还在车头前方（t>0 或 z>0）的最后一拍」——近场到站
    时间会冻结在 0.1~0.2 s，早拍取最小 t 会退到零点几秒之前，横距就报大了。
    极限变道取横幅前 2.5 s 内本道正前方最近的候选（t 最小那一拍）。
    """
    base = ticks[idx]
    lo = base["ts_ns"] - 2.5e9
    hi = base["ts_ns"] + 0.6e9
    last = {}                     # oid -> (ts, j, gap)
    nearest = {}                  # oid -> (t, j, gap)
    for j, rec in enumerate(ticks):
        if not (lo <= rec["ts_ns"] <= hi):
            continue
        ego = (rec.get("scene") or {}).get("ego_m")
        if ego is None:
            continue
        for oid, x, t, z, src in _cars(rec):
            if x is None or oid is None or oid <= 0 or src == "grid":
                continue
            gap = abs(x - ego) / 3.5
            if gap > 1.5:
                continue
            # 判定车必须真的贴到鼻尖（t≤0.25 s 或 z≤6 m）——t=0.7 s/16 m 的车
            # 只是「在接近」，横幅时刻它还没被超越
            ahead = (t is not None and 0.0 < t <= 0.25) or \
                    (z is not None and 0.0 < z <= 6.0)
            if kind == "极限超车":
                if ahead and gap <= 1.5 and rec["ts_ns"] <= base["ts_ns"] + 0.6e9:
                    last[oid] = (rec["ts_ns"], j, gap)
            else:
                if t is not None and 0.0 < t <= 2.5 and gap <= 0.55:
                    cur = nearest.get(oid)
                    if cur is None or t < cur[0]:
                        nearest[oid] = (t, j, gap)
    if kind == "极限超车":
        # 排除横幅后 0.6 s 仍在车头前方的车：判定对象是「已经掉到身后」那台
        end = base["ts_ns"] + 0.6e9
        still_ahead = set()
        for rec in ticks:
            if abs(rec["ts_ns"] - end) > 0.15e9:
                continue
            for oid, x, t, z, src in _cars(rec):
                if oid in last and ((t is not None and t > 0.8)
                                    or (z is not None and z > 12.0)):
                    still_ahead.add(oid)
        for oid in still_ahead:
            last.pop(oid, None)
    pool = last if kind == "极限超车" else nearest
    if not pool:
        return None
    if kind == "极限超车":
        # 最近一次「还在前方」的拍离横幅时间最近的那台车
        oid = min(pool, key=lambda k: abs(pool[k][0] - base["ts_ns"]))
    else:
        oid = min(pool, key=lambda k: pool[k][0])
    _, j, gap = pool[oid]
    return j, oid, gap


def _motion(ticks, j, oid):
    """判定拍的自车横速（道/秒，自车打舵）与该车接近速度（m/s）。

    接近速度取 mcars 的 v_close：正=本车在追上它（主动超越/追尾方向），
    负=它在追上本车（被动）。横向速度只用自车的一侧——对方车的横位在
    10~15 m 有 1~7 道级的身份/锚点跳变，不能当「对方变道」的证据。
    """
    t0 = ticks[j]["ts_ns"]
    ego0 = (ticks[j].get("scene") or {}).get("ego_m")
    vclose = None
    for m in ticks[j].get("mcars") or []:
        if m.get("id") == oid:
            vclose = m.get("v")
            break
    if ego0 is None:
        return None, vclose
    for k in range(j - 1, max(-1, j - 20), -1):
        rec = ticks[k]
        dt = (t0 - rec["ts_ns"]) / 1e9
        if dt < 0.25:
            continue
        if dt > 0.6:
            break
        ego1 = (rec.get("scene") or {}).get("ego_m")
        if ego1 is None:
            continue
        return ((ego0 - ego1) / dt / 3.5, vclose)
    return None, vclose


def analyze(sess: str) -> None:
    ticks = _ticks(sess)
    ts = [r["ts_ns"] for r in ticks]
    banners = _banners(sess)
    summary = collections.Counter()
    by_kind = collections.Counter(k for _, k in banners)
    print(f"== {sess}  ticks {len(ticks)}  banners {dict(by_kind)}")
    for bts, kind in banners:
        if kind == "木桶旋转":
            continue
        i = bisect.bisect_left(ts, bts)
        cands = [x for x in (i - 1, i, i + 1) if 0 <= x < len(ticks)]
        if not cands:
            continue
        j0 = min(cands, key=lambda x: abs(ticks[x]["ts_ns"] - bts))
        if abs(ticks[j0]["ts_ns"] - bts) > 0.8e9:
            summary["横幅无对应拍"] += 1
            continue
        got = _judged(ticks, j0, kind)
        if got is None:
            summary["未找到判定车"] += 1
            continue
        j, oid, gap = got
        veg, vclose = _motion(ticks, j, oid)
        near = gap < EXTREME_GAP
        if vclose is None:
            who = "—"
        elif vclose > ACTIVE_DV:
            who = "主动"
        elif vclose < -ACTIVE_DV:
            who = "被动"
        else:
            who = "平"
        summary[f"{kind}:{'近' if near else '远'}"] += 1
        summary[f"{kind}:{who}"] += 1
        print(f"  {kind} fid {ticks[j]['fid']:>6} oid {oid:>5}"
              f" gap {gap:4.2f} 道 自车横速 "
              f"{'—' if veg is None else f'{veg:5.2f}'} 接近速 "
              f"{'—' if vclose is None else f'{vclose:6.2f}'}  {who}")
    print(f"   汇总 {dict(summary)}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
