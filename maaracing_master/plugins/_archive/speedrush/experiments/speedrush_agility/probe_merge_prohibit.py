"""并线禁令离线复算（speedrush）。

**问什么**：「最后一次可信米制位置在 6 m 内、横向 ±2.5 m 内 → 禁止并线」这条
判据，在既有实机轨迹上能拦住几次真碰撞、误拦多少正常并线，以及碰撞前多久
开始成立。先出数字，再决定是否进生产。

口径（全部取自 trace，不新增阈值）：

- **可信米制位置** = 对象表里 `src=="metric"`、`safety_ok`（横位所用测量的
  年龄在生产 safety_age 内）、`x_m/z_m` 非空。这就是生产自己认「可信」的口径。
- **禁令条件** = 任一车 `0 ≤ z ≤ 6` 且 `|x| ≤ 2.5`（米，自车系，右正）。
- **并线段** = `route.ph == "merging"` 的连续拍。`route.vd` 只是判决事件
  （merge_start/finish/cancel/timeout），不表示持续状态。
- **碰撞真值** = trace 的 `collisions` 列（落定拍），原样印出供人工判伪。
- **记忆变体**：本拍该轨道不是米制时，沿用最近一次合格米制样本，最多
  HOLD_S 秒（默认 0.5）。回答「读数退化到像素后，先前那份米制读数还能不能
  支撑一次否决」。

用法::

    .venv/Scripts/python.exe -X utf8 probe_merge_prohibit.py <会话 ...>
    .venv/Scripts/python.exe -X utf8 probe_merge_prohibit.py --coll 20261117_211759
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
NEAR_M = 6.0
HALF_M = 2.5
HOLD_S = 0.5
CRUDE_V = 50.0     # |闭速| 超这个数说明读数本身荒，碰撞判伪线索


def load(sess: str):
    path = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    ticks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            r = json.loads(line)
            if r.get("fid") is not None and r.get("ts_ns"):
                ticks.append(r)
    ticks.sort(key=lambda r: (r.get("run_id") or "", r["ts_ns"]))
    return ticks


def _objs(r):
    """对象表车行 → [(id, x_m, z_m, src, safety_ok)]。"""
    out = []
    for row in ((r.get("otab") or {}).get("t") or []):
        if len(row) < 11 or row[0] != "car":
            continue
        out.append((row[1], row[3], row[4], row[9], bool(row[7])))
    return out


def _hit(x, z):
    return (x is not None and z is not None
            and 0.0 <= z <= NEAR_M and abs(x) <= HALF_M)


def _phase(r):
    return (r.get("route") or {}).get("ph") or "—"


def _runs(ticks):
    by = {}
    for r in ticks:
        by.setdefault(r.get("run_id"), []).append(r)
    return by


def _episodes(rs):
    eps, cur = [], None
    for i, r in enumerate(rs):
        merging = _phase(r) == "merging"
        if merging and cur is None:
            cur = {"i0": i, "i1": i}
        elif merging:
            cur["i1"] = i
        elif cur is not None:
            eps.append(cur)
            cur = None
    if cur is not None:
        eps.append(cur)
    return eps


def _state(rs):
    """逐拍：本拍直接命中 / 记忆命中（含命中轨道）。"""
    memory = {}
    raw, mem = [], []
    for r in rs:
        hits_now = []
        for oid, x, z, src, ok in _objs(r):
            if src == "metric" and ok and _hit(x, z):
                hits_now.append((oid, x, z, r["ts_ns"]))
                memory[oid] = (r["ts_ns"], x, z)
        raw.append({h[0]: (h[1], h[2], 0.0) for h in hits_now})
        if hits_now:
            mem.append(dict(raw[-1]))
            continue
        m = {}
        for oid, (ts, x, z) in memory.items():
            age = (r["ts_ns"] - ts) / 1e9
            if age <= HOLD_S:
                m[oid] = (x, z, age)
        mem.append(m)
    return raw, mem


def _colls(rs):
    out = []
    for i, r in enumerate(rs):
        for c in (r.get("collisions") or []):
            out.append((i, c))
    return out


def analyze(sess: str, verbose_coll: bool) -> None:
    ticks = load(sess)
    ph_n = {}
    for r in ticks:
        ph_n[_phase(r)] = ph_n.get(_phase(r), 0) + 1
    foc = sorted(f for f in (r.get("dgeo_focal") for r in ticks) if f)
    med_foc = foc[len(foc) // 2] if foc else None
    print(f"== {sess}  拍 {len(ticks)}  ph {ph_n}  焦距中位 {med_foc}")

    for run, rs in _runs(ticks).items():
        eps = _episodes(rs)
        colls = _colls(rs)
        raw, mem = _state(rs)
        ph_n = {}
        for r in rs:
            ph_n[_phase(r)] = ph_n.get(_phase(r), 0) + 1
        n_raw = sum(1 for h in raw if h)
        n_mem = sum(1 for h in mem if h)
        by_ph = {}
        for i, r in enumerate(rs):
            ph = _phase(r)
            a = by_ph.setdefault(ph, [0, 0, 0])
            a[0] += 1
            a[1] += 1 if raw[i] else 0
            a[2] += 1 if mem[i] else 0
        print(f"\n-- {run}: 并线段 {len(eps)}  碰撞事件 {len(colls)}"
              f"  框内拍占比 米制 {n_raw/len(rs):.0%} / 记忆 {n_mem/len(rs):.0%}")
        for ph, (n, a, b) in sorted(by_ph.items()):
            print(f"     {ph}: 拍 {n}  米制 {a/n:.0%}  记忆 {b/n:.0%}")

        def _in_ep(i):
            for k, ep in enumerate(eps):
                if ep["i0"] <= i <= ep["i1"]:
                    return k
            return None

        stats = {"raw": 0, "mem": 0, "tp": 0, "fp": 0, "dur": []}
        for ep in eps:
            i0, i1 = ep["i0"], ep["i1"]
            r0 = rs[i0]
            hits = [(j, c) for j, c in colls
                    if i0 <= j <= i1 + 8]          # 段末后 8 拍内落定也算
            raw0, mem0 = raw[i0], mem[i0]
            if not (raw0 or mem0):
                continue
            kind = "米制" if raw0 else "记忆"
            stats["raw" if raw0 else "mem"] += 1
            # 禁令持续到何时（真实轨迹上不再有车在框内）
            clear = None
            for j in range(i0, min(i1 + 60, len(rs) - 1)):
                if not mem[j]:
                    clear = (rs[j]["ts_ns"] - r0["ts_ns"]) / 1e9
                    break
            det = raw0 or mem0
            oid = next(iter(det))
            x, z, sage = det[oid]
            dur = (rs[i1]["ts_ns"] - r0["ts_ns"]) / 1e9
            stats["dur"].append(clear if clear is not None else dur)
            tag = "命中碰撞" if hits else "无碰撞记录"
            stats["tp" if hits else "fp"] += 1
            ex = r0.get("executed_lane")
            rt0 = r0.get("route") or {}
            tgt, ref = rt0.get("tgt"), rt0.get("ref")
            ex1 = rs[min(i1, i0 + 10)].get("executed_lane")
            dirn = "—"
            if ex is not None and ex1 is not None and abs(ex1 - ex) > 0.05:
                dirn = "同向" if (ex1 - ex) * x > 0 else "反向"
            print(f"   [{kind}] ep fid {r0['fid']}..{rs[i1]['fid']}"
                  f" dur {dur:.1f}s ref {ref} tgt {tgt} ex {ex}"
                  f" why {rt0.get('why')}"
                  f" 肇事车 oid{oid} x{x:+.1f} z{z:.1f}（{dirn}）"
                  f" 样本龄 {sage*1000:.0f}ms"
                  f" 禁令余 {clear if clear is not None else '—'}s → {tag}")
            for j, c in hits:
                rel = (rs[j]["ts_ns"] - r0["ts_ns"]) / 1e9
                flag = "荒值" if (c.get("v") or 0) > CRUDE_V else ""
                print(f"      碰撞 id{c['id']} 落定 fid {c['settle']}"
                      f"（起手 {rel:+.1f}s）z {c['z']} x {c['x']}"
                      f" xc {c['xc']} src {c['src']} v {c['v']}{flag}")
        print(f"   起手被拦 {stats['raw'] + stats['mem']}"
              f"（本拍米制 {stats['raw']} / 记忆 {stats['mem']}）"
              f"  其中命中碰撞 {stats['tp']}  无碰撞记录 {stats['fp']}")
        if stats["dur"]:
            ds = sorted(stats["dur"])
            print(f"   禁令平均余量 {sum(ds)/len(ds):.1f}s"
                  f"（中位 {ds[len(ds)//2]:.1f}s）")

        # 每个碰撞：事前 3 s 条件是否成立、能否提前拦
        for i, c in colls:
            trig = None
            for j in range(i, -1, -1):
                if rs[j]["fid"] == c["fid"]:
                    trig = j
                    break
            if trig is None:
                trig = i
            first = None
            for j in range(max(0, trig - 60), trig + 1):
                if mem[j]:
                    first = j
                    break
            k = _in_ep(trig)
            lead = ("—" if first is None else
                    f"{(rs[trig]['ts_ns'] - rs[first]['ts_ns']) / 1e9:.1f}s")
            who = ("—" if first is None else
                   " ".join(f"oid{o}(x{x:+.1f},z{z:.1f},龄{a*1000:.0f}ms)"
                            for o, (x, z, a) in mem[first].items()))
            flag = "荒值" if (c.get("v") or 0) > CRUDE_V else ""
            print(f"   ↳碰撞 id{c['id']} 落定 fid {c['settle']}"
                  f" 触发 fid {c['fid']} z {c['z']} x {c['x']}"
                  f" xc {c['xc']} v {c['v']}{flag}"
                  f" | 触发时 phase {_phase(rs[trig])}"
                  f"{'' if k is None else f' 并在段{k}'}"
                  f" | 条件提前 {lead} 成立 {who}")
            if verbose_coll:
                hist = []
                for j in range(max(0, trig - 120), i + 1):
                    for o, x, z, s, ok in _objs(rs[j]):
                        if o == c["id"] and s == "metric" and x is not None:
                            hist.append((j, x, z, ok))
                for j, x, z, ok in hist:
                    rel = (rs[j]["ts_ns"] - rs[trig]["ts_ns"]) / 1e9
                    print(f"        {rel:+.1f}s fid {rs[j]['fid']}"
                          f" x{x:+.2f} z{z:+.2f}"
                          f"{'' if ok else ' 不安全'}"
                          f" {'在框' if _hit(x, z) else ''}")


def _curve(sess: str, windows) -> None:
    """不同纵向窗下的提前量与常开占比（判据能不能当预警用）。"""
    ticks = load(sess)
    for run, rs in _runs(ticks).items():
        eps = _episodes(rs)
        colls = _colls(rs)
        hits_by_w = {}
        for w in windows:
            hits_by_w[w] = [
                {oid for oid, x, z, s, ok in _objs(r)
                 if s == "metric" and ok and x is not None and z is not None
                 and 0.0 <= z <= w and abs(x) <= HALF_M}
                for r in rs]
        print(f"\n== {sess} / {run}  并线段 {len(eps)}  碰撞 {len(colls)}")
        for w in windows:
            armed = sum(1 for h in hits_by_w[w] if h) / len(rs)
            banned = sum(1 for ep in eps if hits_by_w[w][ep["i0"]])
            leads = []
            for i, c in colls:
                trig = next((j for j in range(i, -1, -1)
                             if rs[j]["fid"] == c["fid"]), i)
                first = next((j for j in range(max(0, trig - 60), trig + 1)
                              if c["id"] in hits_by_w[w][j]), None)
                leads.append(-1.0 if first is None else
                             (rs[trig]["ts_ns"] - rs[first]["ts_ns"]) / 1e9)
            seg = " ".join(f"{v:.1f}" for v in leads)
            print(f"   窗 {w:>4.0f}m  常开拍 {armed:.1%}"
                  f"  起手被拦 {banned}/{len(eps)}"
                  f"  各碰撞提前 {seg}")


def _ttc(sess: str, ttc_max: float, lat_m: float) -> None:
    """换成到站时间窗：闭速为正且 z/v < ttc_max 时算命中。"""
    ticks = load(sess)
    for run, rs in _runs(ticks).items():
        eps = _episodes(rs)
        colls = _colls(rs)

        def hit_set(r):
            out = set()
            for row in ((r.get("otab") or {}).get("t") or []):
                if len(row) < 13 or row[0] != "car":
                    continue
                oid, x, z, src, ok, v = row[1], row[3], row[4], row[9], row[7], row[11]
                if (src != "metric" or not ok or x is None or z is None
                        or v is None or v <= 0):
                    continue
                if abs(x) <= lat_m and z / v <= ttc_max:
                    out.add(oid)
            return out

        hs = [hit_set(r) for r in rs]
        armed = sum(1 for h in hs if h) / len(rs)
        banned = sum(1 for ep in eps if hs[ep["i0"]])
        leads = []
        for i, c in colls:
            trig = next((j for j in range(i, -1, -1)
                         if rs[j]["fid"] == c["fid"]), i)
            first = next((j for j in range(max(0, trig - 60), trig + 1)
                          if c["id"] in hs[j]), None)
            leads.append(-1.0 if first is None else
                         (rs[trig]["ts_ns"] - rs[first]["ts_ns"]) / 1e9)
        print(f"   TTC<{ttc_max}s 横 ±{lat_m}m  常开 {armed:.0%}"
              f"  起手被拦 {banned}/{len(eps)}"
              f"  各碰撞提前 " + " ".join(f"{v:.1f}" for v in leads))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--curve" in sys.argv:
        for s in args:
            _curve(s, (6.0, 10.0, 15.0, 20.0, 30.0, 40.0))
    elif "--ttc" in sys.argv:
        for s in args:
            print(f"== {s}")
            for t, l in ((1.0, 2.5), (1.5, 2.5), (1.5, 3.5), (2.0, 3.5)):
                _ttc(s, t, l)
    else:
        for s in args:
            analyze(s, "--coll" in sys.argv)
