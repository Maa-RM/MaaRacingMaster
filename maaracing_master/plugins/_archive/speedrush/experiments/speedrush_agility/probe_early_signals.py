"""更早证据盘查（speedrush，离线）。

**问什么**：在 YOLO 第一次检出肇事车之前，trace 里已有的其它观测面有没有任何一项
已经指向「本道前方有东西」？有 → 提前预置还有机会买到时间；没有 → 这套传感器
在当前速度下就是来不及。

逐项检查（全部来自 trace，现有字段，不新增口径）：

- `occ.blocked`：占用栅格是否判定本道被占（源自跟踪对象）。
- `open`：走廊宽 `w`（道）、60 m 内安全车数 `n`、最小到站时间 `ttc`、分级 `lvl`。
- `cor_far`：走廊远带每段的左右净空；看 30~60 m 段是否收窄。
- `map`：持久车道图支撑距离 `sup` 与采样点数。
- `ro_w` / `blk_l` / `blk_r`：路缘与红块内缘读数。

口径：

- **提前量** = 首次检出时刻 − 该指示第一次成立时刻（正=比检测更早）。
- 「成立」用各字段自己的既有分级（blocked / crowded / ttc<1 s / 走廊宽<2 道），
  不另调阈值。
- 首次检出 = 对象表第一次出现该轨道 id 的拍。

用法::

    .venv/Scripts/python.exe -X utf8 probe_early_signals.py <会话>
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
BACK_S = 3.0
LANE_W = 3.5


def load(sess: str):
    p = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    ticks = [json.loads(l) for l in open(p, encoding="utf-8")
             if l.startswith("{") and '"fid"' in l]
    ticks.sort(key=lambda r: r["ts_ns"])
    return ticks


def _obj(r, oid):
    for row in ((r.get("otab") or {}).get("t") or []):
        if len(row) > 10 and row[0] == "car" and row[1] == oid:
            return row
    return None


def _cor_gap(r, lo, hi):
    """走廊在 [lo, hi] 米段的最小左右净空（米）。"""
    best = None
    for band in (r.get("cor_far") or []):
        if len(band) < 4:
            continue
        z0, z1, left, right = band[0], band[1], band[2], band[3]
        if z1 < lo or z0 > hi:
            continue
        if left is None or right is None:
            continue
        if best is None or (right - left) < best:
            best = right - left
    return best


def analyze(sess: str) -> None:
    ticks = load(sess)
    idx = {r["fid"]: i for i, r in enumerate(ticks)}
    print(f"== {sess}  拍 {len(ticks)}")
    print("  碰撞  首次检出  occ.blocked  crowded  ttc<1s  走廊<2道  30-60m最窄")
    for r in ticks:
        for c in (r.get("collisions") or []):
            oid, fid = c["id"], c["fid"]
            if fid not in idx:
                continue
            i = idx[fid]
            first = None
            for j in range(i, -1, -1):
                if _obj(ticks[j], oid) is not None:
                    first = j
                else:
                    break
            # 真正的首次：继续往前找是否有更早的连续段
            for j in range(max(0, i - 200), first if first is not None else i):
                if _obj(ticks[j], oid) is not None:
                    first = j
                    break
            t0 = ticks[first]["ts_ns"] if first is not None else ticks[i]["ts_ns"]
            lo = next((j for j in range(first if first is not None else i, -1, -1)
                       if ticks[j]["ts_ns"] < t0 - BACK_S * 1e9), 0)

            def scan(pred, label):
                """返回最早的成立拍（相对首次检出的秒），没有则 None。"""
                for j in range(lo, (first if first is not None else i) + 1):
                    if pred(ticks[j]):
                        return (ticks[j]["ts_ns"] - t0) / 1e9
                return None

            got = {
                "occ": scan(lambda r: bool((r.get("occ") or {}).get("blocked")),
                            "occ"),
                "crowd": scan(lambda r: (r.get("open") or {}).get("lvl")
                              == "crowded", "crowd"),
                "ttc": scan(lambda r: ((r.get("open") or {}).get("ttc") or 9) < 1.0,
                            "ttc"),
                "narrow": scan(lambda r: ((r.get("open") or {}).get("w") or 99)
                               < 2.0, "narrow"),   # open.w 单位是道（openness.py）
                "cor": scan(lambda r: (_cor_gap(r, 30, 60) or 99) < 3.5 * 2,
                            "cor"),
            }
            fmt = lambda v: "—" if v is None else f"{v:+.1f}s"
            print(f"  id{oid:<4} fid {ticks[first]['fid'] if first is not None else '—':<6}"
                  f" {fmt(got['occ']):>10} {fmt(got['crowd']):>9}"
                  f" {fmt(got['ttc']):>7} {fmt(got['narrow']):>8}"
                  f" {fmt(got['cor']):>9}")

            # 明细：首次检出前 1.5 s 到检出后 0.2 s 的关键列
            for j in range(lo, (first if first is not None else i) + 6):
                rr = ticks[j]
                rel = (rr["ts_ns"] - t0) / 1e9
                if rel < -1.5:
                    continue
                o = _obj(rr, oid)
                op = rr.get("open") or {}
                oc = rr.get("occ") or {}
                gap = _cor_gap(rr, 30, 60)
                print(f"      {rel:+.2f}s 轨{'有' if o else '无'}"
                      f" occ={oc.get('blocked')}/{oc.get('ids')}"
                      f" open w={op.get('w')} n={op.get('n')} ttc={op.get('ttc')}"
                      f" {op.get('lvl')}"
                      f" 30-60m={None if gap is None else round(gap, 1)}"
                      f" ro_w={rr.get('ro_w')}")
            break


if __name__ == "__main__":
    for s in sys.argv[1:]:
        analyze(s)
