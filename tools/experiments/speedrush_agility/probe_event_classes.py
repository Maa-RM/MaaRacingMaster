"""事件分型探针（speedrush，离线）。

**问什么**：一局里的「擦边得分 / 正面接触 / 侧面碾入 / 偏置贴近 / 躲避过早」
各有多少？混在一个「近距事件」计数里没法判断改动好坏。

口径（几何代理，不是真值；游戏日志没有碰撞事件）：

- **接触候选**：某拍该车接地距离 zg ≤ 5 m 且横向 |x−ego| ≤ 0.54 道（两车
  半宽和）。按 oid 取最近的一拍。
- **横向位置**：正面 ≤0.28 道（几乎对中）/ 偏置 0.28~0.54 道（车心错开、车身仍
  可能重叠）。
- **相对运动**：最近 0.15 s 的横向接近速度——逼近 >0.15 道/s（自车正在碾向它）、
  平行 ±0.15、离开 <-0.15（已经在把它甩开）。
- **危险类**：逼近，或「正面且未在离开」。离开型的接触候选是贴过之后正在拉开，
  不作为问题事件。
- **有得分横幅**：该拍 ±2 s 内 HUD 出现「极限超车 / 极限变道 / 木桶旋转」
  （游戏自己的计分横幅，OCR 0.5 s 采样，可能漏读）。
- **并线威胁真伪**：并线期间源道的发布到站时间 t 最小值为准；全部 >1.5 s
  记「源道从未进 1.5 s」= 躲避过早的候选。发布 z 在近场会缺样，不能用来
  判「有没有车」，所以不用 zg。

用法::

    .venv/Scripts/python.exe -X utf8 probe_event_classes.py <会话 ...>
"""

from __future__ import annotations

import collections
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_distance_lead as P  # noqa: E402

BANNER_KINDS = ("极限超车", "极限变道", "木桶旋转")
NEAR_M = 5.0
HALF_LANE = 0.54
SIDE_CLOSING = 0.15          # 道/秒
FRONTAL_LANE = 0.28


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
            txt = ((rec.get("fields") or {}).get("event_banner") or {}).get("text")
            txt = (txt or "").replace(" ", "")
            kind = next((k for k in BANNER_KINDS if k in txt), None)
            if kind is not None:
                raw.append((rec["ts_ns"], kind))
    out: list[tuple[int, str]] = []
    for ts, kind in raw:                     # 同一横幅持续显示：2 s 内合并
        if out and out[-1][1] == kind and ts - out[-1][0] < 2_000_000_000:
            continue
        out.append((ts, kind))
    return out


def _contacts(ticks) -> list[dict]:
    per = collections.defaultdict(list)
    for r in ticks:
        ego = (r.get("scene") or {}).get("ego_m")
        if ego is None:
            continue
        for oid, x, t, z, src, zg, h, clip, cy in P._cars(r):
            if x is None:
                continue
            per[(r.get("run_id"), oid)].append(
                (r["fid"], r["ts_ns"], abs(x - ego) / 3.5,
                 None if zg is None else zg))
    out = []
    for (_, oid), seq in per.items():
        cc = [s for s in seq if s[2] <= HALF_LANE and s[3] is not None
              and s[3] <= NEAR_M]
        if not cc:
            continue
        best = min(cc, key=lambda s: s[3])
        prev = [s for s in seq if s[1] <= best[1]
                and best[1] - s[1] <= 2.5e8]
        closing = None
        if len(prev) >= 2:
            a, b = prev[0], best
            dt = (b[1] - a[1]) / 1e9
            if dt > 0:
                closing = (a[2] - b[2]) / dt
        out.append({"oid": oid, "fid": best[0], "ts": best[1],
                    "gap": best[2], "zg": best[3], "closing": closing})
    return out


def _merges(ticks) -> list[dict]:
    per = []
    for r in ticks:
        st = collections.defaultdict(lambda: [None, None])
        for o in (r.get("scene") or {}).get("objs") or []:
            if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
                continue
            k = int(round(o[1] / 3.5))
            rec = st[k]
            if o[2] is not None and (rec[0] is None or o[2] < rec[0]):
                rec[0] = o[2]
            if o[7] is not None and (rec[1] is None or o[7] < rec[1]):
                rec[1] = o[7]
        per.append(st)
    out, cur, prev_ref = [], None, None
    for i, r in enumerate(ticks):
        rt = r.get("route") or {}
        if rt.get("vd") == "merge_start":
            cur = {"fid": r["fid"], "src": prev_ref, "why": rt.get("why"),
                   "min_t": None, "end": None}
        if cur is not None and cur["src"] is not None:
            rec = per[i].get(int(round(cur["src"])))
            if rec and rec[0] is not None \
                    and (cur["min_t"] is None or rec[0] < cur["min_t"]):
                cur["min_t"] = rec[0]
        if rt.get("vd") in ("finish", "cancel", "timeout") and cur is not None:
            cur["end"] = rt.get("vd")
            out.append(cur)
            cur = None
        if rt.get("ref") is not None:
            prev_ref = float(rt["ref"])
    return out


def analyze(sess: str) -> None:
    ticks, stage_s = P.load(sess)
    banners = _banners(sess)
    kinds = collections.Counter(k for _, k in banners)
    contacts = _contacts(ticks)
    pos = collections.Counter()
    mot = collections.Counter()
    danger = 0
    scored = 0
    for c in contacts:
        c["pos"] = "正面" if c["gap"] <= FRONTAL_LANE else "偏置"
        cl = c["closing"]
        c["mot"] = ("离开" if cl is not None and cl < -SIDE_CLOSING
                    else "逼近" if cl is not None and cl > SIDE_CLOSING
                    else "平行")
        risky = c["mot"] == "逼近" or (c["pos"] == "正面"
                                       and c["mot"] != "离开")
        c["risky"] = risky
        bs = [b for b in banners if abs(b[0] - c["ts"]) <= 2e9]
        c["banner"] = "+".join(x[1] for x in bs) or None
        pos[c["pos"]] += 1
        mot[c["mot"]] += 1
        danger += int(risky)
        if bs:
            scored += 1
    merges = _merges(ticks)
    mc = collections.Counter(m["end"] for m in merges)
    bad_m = [m for m in merges
             if m["min_t"] is not None and m["min_t"] > 1.5]
    mins = stage_s / 60.0
    print(f"== {sess}  stage {stage_s:.0f}s")
    print(f"   得分横幅 {dict(kinds)}")
    print(f"   接触候选 {len(contacts)} ({len(contacts)/mins:.2f}/分)"
          f"  位置 {dict(pos)}  运动 {dict(mot)}")
    print(f"     危险类 {danger} ({danger/mins:.2f}/分)"
          f"  贴过 {len(contacts)-danger}"
          f"  有得分横幅 {scored} / 无横幅 {len(contacts)-scored}")
    for c in sorted(contacts, key=lambda a: a["zg"]):
        closing = "—" if c["closing"] is None else f"{c['closing']:.2f}"
        print(f"     oid {c['oid']:>4} fid {c['fid']:>6}"
              f" {'!' if c['risky'] else ' '} {c['pos']}/{c['mot']}"
              f" gap {c['gap']:.2f} zg {c['zg']:.1f}"
              f" closing {closing} banner {c['banner'] or '—'}")
    print(f"   并线 {len(merges)} 起  {dict(mc)}"
          f"  源道威胁从未进 1.5s {len(bad_m)}")


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
