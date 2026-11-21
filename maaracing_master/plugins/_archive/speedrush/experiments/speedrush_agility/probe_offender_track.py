"""肇事车接触前轨迹取证（speedrush，离线）。

**问什么**：碰撞前那几秒，肇事车到底是「没被检测到」「跟踪换了编号」，还是
「检到了但没有米制横向解」。这决定下一刀砍在检测连续性还是决策。

逐拍同时看四层：

- 检测层 `car_pos` / `car_h`（YOLO 框：屏幕 x、框心行、框高、置信）；
- 对象表层 `otab.t`（身份键、像素横位、米制横位/纵深、来源、龄、是否本拍见到）；
- 米制链 `mcars`（每个 id 的距离来源与弃权理由）；
- 身份层：肇事 id 第一次出现时，回看 0.6 s 内其他车轨的屏幕位置，判断是不是
  同一辆物理车换了编号。

用法::

    .venv/Scripts/python.exe -X utf8 probe_offender_track.py <会话> [id@fid ...]
    # 不给 id@fid 就跑该会话全部碰撞事件
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
BACK_S = 3.0
FWD_S = 1.0


def load(sess: str):
    path = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    ticks = [json.loads(l) for l in open(path, encoding="utf-8")
             if l.startswith("{") and '"fid"' in l]
    ticks.sort(key=lambda r: (r.get("run_id") or "", r["ts_ns"]))
    return ticks


def _index(rs):
    return {r["fid"]: i for i, r in enumerate(rs)}


def _runs(ticks):
    by = {}
    for r in ticks:
        by.setdefault(r.get("run_id"), []).append(r)
    return by


def _det(r, oid):
    pos = {d["id"]: d for d in (r.get("car_pos") or [])}
    hh = {d["id"]: d for d in (r.get("car_h") or [])}
    return pos.get(oid), hh.get(oid)


def _otab(r, oid):
    for row in ((r.get("otab") or {}).get("t") or []):
        if len(row) > 10 and row[0] == "car" and row[1] == oid:
            return row
    return None


def _mcar(r, oid):
    for m in (r.get("mcars") or []):
        if m.get("id") == oid:
            return m
    return None


def _fmt(r, oid):
    p, h = _det(r, oid)
    o = _otab(r, oid)
    m = _mcar(r, oid)
    det = ("—" if p is None else
           f"x{p['x'] if p['x'] is not None else '—'}"
           f" cy{p['cy']} h{h['h'] if h else '—'}"
           f" c{h['conf'] if h else '—'}")
    if o is None:
        obj = "无轨道"
    else:
        obj = (f"xl {o[2]} xm {o[3]} z {o[4]}"
               f" 龄{o[5]} {'见' if o[6] else '缺'}"
               f" {o[9]}")
        if len(o) > 10:
            obj += f"/{o[10]}"
    mc = ("—" if m is None else
          f"{m.get('src')} z{m.get('z')} x{m.get('x')} {m.get('bwhy')}")
    return f"检测[{det}] 轨道[{obj}] 链[{mc}]"


def _neighbors(rs, idx, oid):
    """肇事 id 首次出现前后各 0.6 s，其他车轨的屏幕位置（找换号）。"""
    t = rs[idx]["ts_ns"]
    rows = []
    for j in range(max(0, idx - 12), min(len(rs), idx + 1)):
        r = rs[j]
        if abs(r["ts_ns"] - t) > 1e9:
            continue
        for p in (r.get("car_pos") or []):
            if p["id"] == oid:
                continue
            h = {d["id"]: d for d in (r.get("car_h") or [])}.get(p["id"]) or {}
            rows.append((r["fid"], p["id"], p["x"], p["cy"], h.get("h"),
                         h.get("conf")))
    return rows


def analyze(sess: str, picks) -> None:
    ticks = load(sess)
    by_run = {}
    for r in ticks:
        by_run.setdefault(r.get("run_id"), []).append(r)
    for run, rs in by_run.items():
        idx = _index(rs)
        todo = []
        if picks:
            for p in picks:
                oid, fid = p.split("@")
                todo.append((int(oid), int(fid)))
        else:
            for r in rs:
                for c in (r.get("collisions") or []):
                    todo.append((c["id"], c["fid"]))
        print(f"\n== {sess} / {run}  目标 {todo}")
        for oid, fid in todo:
            i = idx.get(fid)
            if i is None:
                print(f"   id{oid}@{fid}: 找不到该帧")
                continue
            first = None
            for j in range(i, -1, -1):
                if _otab(rs[j], oid) is not None:
                    first = j
                else:
                    break
            lo = next((j for j in range(i, -1, -1)
                       if rs[j]["ts_ns"] < rs[i]["ts_ns"] - BACK_S * 1e9), 0)
            hi = next((j for j in range(i, len(rs))
                       if rs[j]["ts_ns"] > rs[i]["ts_ns"] + FWD_S * 1e9),
                      len(rs) - 1)
            print(f"\n   ── id{oid} 触发 fid {fid}"
                  f"（首次出现 fid {rs[first]['fid'] if first is not None else '—'}"
                  f"，距触发 {(rs[i]['ts_ns'] - rs[first]['ts_ns'])/1e9:.1f}s）"
                  if first is not None else f"\n   ── id{oid} 触发 fid {fid}（从未出现）")
            for j in range(lo, hi + 1):
                r = rs[j]
                rel = (r["ts_ns"] - rs[i]["ts_ns"]) / 1e9
                mark = " ←触发" if j == i else ""
                print(f"     {rel:+.1f}s fid {r['fid']}"
                      f" ph {(r.get('route') or {}).get('ph')}"
                      f" {_fmt(r, oid)}{mark}")
            if first is not None and first > 0:
                print("     换号候选（首次出现前 1 s 的其他车轨）：")
                for fid2, oid2, x, cy, h, conf in _neighbors(rs, first, oid):
                    print(f"       fid {fid2} id{oid2} x{x} cy{cy}"
                          f" h{h} c{conf}")


def summary(sess: str) -> None:
    """每次碰撞一张表：检测/像素横位/米制纵深/米制横位各自何时首次可用。"""
    ticks = load(sess)
    for run, rs in _runs(ticks).items():
        idx = _index(rs)
        print(f"\n== {sess} / {run}")
        print("   碰撞  触发fid  首检测(距触发)  像素横位  米制纵深  米制横位"
              "  米制横位出现时z  首次检测框")
        for r in rs:
            for c in (r.get("collisions") or []):
                oid, fid = c["id"], c["fid"]
                i = idx.get(fid)
                if i is None:
                    continue
                seen = {"det": None, "xl": None, "zm": None, "xm": None}
                for j in range(max(0, i - 200), i + 1):
                    if seen["det"] is None and _det(rs[j], oid)[0] is not None:
                        seen["det"] = j
                    o = _otab(rs[j], oid)
                    if o is None:
                        continue
                    if seen["xl"] is None and o[2] is not None:
                        seen["xl"] = j
                    if seen["zm"] is None and o[4] is not None:
                        seen["zm"] = j
                    if seen["xm"] is None and o[3] is not None:
                        seen["xm"] = j
                t0 = rs[i]["ts_ns"]

                def ag(k):
                    return ("—" if seen[k] is None else
                            f"{(rs[i]['ts_ns'] - rs[seen[k]]['ts_ns'])/1e9:.1f}s")

                zm_at = "—"
                if seen["xm"] is not None:
                    o = _otab(rs[seen["xm"]], oid)
                    zm_at = o[4]
                p = _det(rs[seen["det"]], oid)[0] if seen["det"] is not None else None
                hh = (_det(rs[seen["det"]], oid)[1] or {}) if seen["det"] else {}
                box = ("—" if p is None else
                       f"cy{p['cy']} h{hh.get('h')} c{hh.get('conf')}")
                print(f"   id{oid:<4} {fid:<7} {ag('det'):>7}"
                      f" {ag('xl'):>9} {ag('zm'):>8} {ag('xm'):>8}"
                      f" {zm_at:>15}  {box}")


if __name__ == "__main__":
    s = sys.argv[1]
    if "--summary" in sys.argv:
        summary(s)
    else:
        analyze(s, sys.argv[2:])
