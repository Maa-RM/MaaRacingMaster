"""纵向测距/身份可用性探针（speedrush，离线）。

**问什么**：车靠近时，程序对它的纵向距离（z）和到站时间（t）还可信吗？如果
换成「框底接地」直接量距，读数会更稳还是更抖？

为什么不用撞车做样本：RULES §4.3 定案「碰撞只浪费时间、不掉速不扣分」，日志
里也没有碰撞事件，所以拿分速跌当撞车是错的（2026-11-10 撤回）。本探针改用
**近距事件**做样本：某车接地距离 ≤12 m 且与自车横向 ≤0.6 道——车已贴到鼻子
底下，不管有没有撞上，决策都必须已经做完。

接地距离（zg）= focal·cam_h / (cy − y_h)：框底行 cy 落在路面平面上的针孔投影。
标定取 gate0.json（y_h=354.2、focal_px=807.7）与 trace 逐拍机高 dgeo_cam_h。
适用域：cy−y_h ≥ 20 px（约 50 m 内）；越远分母越小、±1 px 就跳数米。框底被
前车截断时 zg 会偏大，探针用「同排行两车框高差 >50%」把两台都标为可疑，
不静默采信。

身份跳变：同 oid 相邻两拍的接地距离跳 >5 m 或 >30%，或横向跳 >1 m。

口径：per-minute 指标的分母一律用**阶段内时间**（stage 掐头 12 s、去尾 10 s 的
累计），不用整场墙钟。

用法::

    .venv/Scripts/python.exe -X utf8 probe_distance_lead.py <会话 ...>
    # 会话写成 20261110_114403 这种目录名（读 %APPDATA%/MaaRacingMaster/logs）
"""

from __future__ import annotations

import argparse
import collections
import json
import os

ROOT = os.path.expandvars(r"%APPDATA%/MaaRacingMaster/logs")
Y_H = 354.2     # gate0.json 地平线行
FOCAL = 807.7   # gate0.json 线角几何焦距（全幅像素）
LANE_W = 3.5    # gate0.json 画线真道宽
ZG_MIN_DENOM = 20.0


def load(sess: str, head: float = 12.0, tail: float = 10.0):
    path = os.path.join(ROOT, sess, "trace", "speedrush.jsonl")
    stages, cur, ticks = [], None, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            rec = json.loads(line)
            ty = rec.get("type")
            if ty == "stage_begin":
                cur = [rec["ts_mono_ns"], None]
                stages.append(cur)
            elif ty == "stage_end" and cur is not None:
                cur[1] = rec["ts_mono_ns"]
                cur = None
            elif rec.get("fid") is not None:
                ticks.append(rec)
    keep = []
    for rec in ticks:
        m = rec.get("ts_mono_ns")
        if m is None:
            continue
        for st in stages:
            if st[1] and (m - st[0]) / 1e9 >= head and (st[1] - m) / 1e9 >= tail:
                keep.append(rec)
                break
    stage_s = sum((s[1] - s[0]) / 1e9 for s in stages if s[1])
    return keep, stage_s


def _zg(cy_bottom, cam_h):
    if cy_bottom is None or not cam_h:
        return None
    d = cy_bottom - Y_H
    if d < ZG_MIN_DENOM:
        return None
    return FOCAL * cam_h / d


def _cars(rec):
    """[(oid, x_m, t_pub, z_pub, src, zg, h, clip, cy_bottom)]，跳过非车行。

    trace 的 `car_pos[].cy` 是**框中心**（perception.py 的 Detection.cy=(y1+y2)//2），
    接地行要加 h/2——中心当框底会把绝对距离算大、分带整体外移（2026-11-10 踩过）。

    同拍同接地行（±3 px）出现框高差超过 50% 的两台车，说明至少一台的框底被
    截断（近车挡住远车），两台都不采信 zg。逐拍机高越出 [1.0, 1.6] m 的拍
    按无供数处理（实测尾部到 2~4 m，是估计失败而非机位变化）。
    """
    pos = {d["id"]: d for d in (rec.get("car_pos") or [])}
    hh = {d["id"]: d for d in (rec.get("car_h") or [])}
    cam_h = rec.get("dgeo_cam_h")
    if not (cam_h and 1.0 <= cam_h <= 1.6):
        cam_h = None
    objs = [o for o in (rec.get("scene") or {}).get("objs") or []
            if o and o[0] == "car" and len(o) > 14]
    rows = []
    for o in objs:
        oid = o[14]
        cy = (pos.get(oid) or {}).get("cy")
        h = (hh.get(oid) or {}).get("h")
        rows.append((oid, None if cy is None or h is None else cy + h / 2.0, h))
    clip = set()
    for i, (oid, cb, h) in enumerate(rows):
        for oid2, cb2, h2 in rows[i + 1:]:
            if None in (cb, cb2, h, h2):
                continue
            if abs(cb - cb2) <= 3 and min(h, h2) * 2 < max(h, h2):
                clip.update((oid, oid2))
    out = []
    for o, (oid, cb, h) in zip(objs, rows):
        out.append((oid, o[1], o[2], o[7], o[6],
                    _zg(cb, cam_h), h, oid in clip, cb))
    return out


def stability(ticks):
    """同 oid 相邻两拍：发布 z 冻结、发布 t 翻倍/减半、接地距离跳变。

    横向跳变按「当拍 z 是否跳/断」分两组给条件率：只看「x 跳里有多少伴 z 跳」
    会把耦合看小——z 稳的拍本来就多。判耦合用 P(x跳|z抖) vs P(x跳|z稳)。
    """
    prev, c, frozen = {}, collections.Counter(), []
    run = collections.Counter()
    for r in ticks:
        for oid, x, t, z, src, zg, h, clip, cy in _cars(r):
            p = prev.get(oid)
            if p is not None:
                if z is not None and p["z"] is not None:
                    if z == p["z"]:
                        run[oid] += 1
                    else:
                        if run[oid] >= 1:
                            frozen.append(run[oid] + 1)
                        run[oid] = 0
                if p["t"] and t and p["t"] > 0.05:
                    ratio = t / p["t"]
                    c["t 可比拍"] += 1
                    if ratio >= 2 or ratio <= 0.5:
                        c["t 单拍翻倍/减半"] += 1
                if x is not None and p["x"] is not None:
                    c["x 可比拍"] += 1
                    xj = abs(x - p["x"]) > 1.0
                    if xj:
                        c["x 跳变拍"] += 1
                    if p["zg"] is not None and zg is not None \
                            and not clip and not p["clip"]:
                        c["zg 可比拍"] += 1
                        zj = abs(zg - p["zg"]) > max(5.0, 0.3 * p["zg"])
                        if zj:
                            c["zg 跳变拍"] += 1
                        c["z稳" if not zj else "z抖"] += 1
                        if xj:
                            c["z稳且x跳" if not zj else "z抖且x跳"] += 1
            prev[oid] = {"z": z, "t": t, "zg": zg, "clip": clip, "x": x}
    return c, frozen


def near_field(ticks):
    """近距事件：接地距离 ≤12 m 且横向 ≤0.6 道（自车已在它后面）。

    每条给出三个时刻的发布读数：首次跨 20 m（可判拍）、首次跨 12 m（起意
    拍该已过）、最近拍（判定拍）。发布 z/t 为 None 就照实写 None。
    """
    first20, first12, closest = {}, {}, {}
    for r in ticks:
        ego = (r.get("sel_dbg") or {}).get("ego")
        for oid, x, t, z, src, zg, h, clip, cy in _cars(r):
            if zg is None or x is None or ego is None:
                continue
            if abs(x / LANE_W - ego) > 0.6:
                continue
            row = (r.get("fid"), zg, z, t, src, r.get("sel_why"))
            if zg <= 20.0:
                first20.setdefault(oid, row)
            if zg <= 12.0:
                first12.setdefault(oid, row)
            if oid not in closest or zg < closest[oid][1]:
                closest[oid] = row
    return [(oid, first20[oid], first12.get(oid), closest[oid])
            for oid in closest if oid in first20]


def _fit(zs, ts, win=0.4):
    """窗口内最小二乘拟合 zg(t)，返 (当前拟合 z, 闭合速度 m/s) 或 None。

    残差 >15% 说明这段不合恒定接近（换道/遮挡/跨车），弃权不硬套。
    """
    pts = [(a, b) for a, b in zip(ts, zs) if ts[-1] - win <= a <= ts[-1]]
    if len(pts) < 5:
        return None
    n = len(pts)
    mt = sum(p[0] for p in pts) / n
    mz = sum(p[1] for p in pts) / n
    den = sum((p[0] - mt) ** 2 for p in pts)
    if den <= 1e-9:
        return None
    slope = sum((p[0] - mt) * (p[1] - mz) for p in pts) / den
    if slope >= -0.5:
        return None                      # 不接近：真值无穷，无可比
    resid = max(abs(b - (mz + slope * (a - mt))) for a, b in pts)
    if resid > 0.15 * mz:
        return None
    return mz + slope * (ts[-1] - mt), -slope


def t_bias(ticks, bands=((10.0, 20.0), (20.0, 30.0), (30.0, 40.0)),
           invalidated: bool = False, clear_s: float = 0.3):
    """以接地距离为真值检验发布 t：中位比、报大 1.5/2 倍占比、报小占比。

    真值 = 拟合 z / 拟合闭合速度。只有连续 12 拍都有干净接地读数、且拟合
    残差合格的车拍才入账——样本少但每条都经得起看。

    ``invalidated``（跳变作废影子，2026-11-11）：同 oid 纵向 >5 m/30%、
    发布 z 跳 >5 m、横向跳 >0.5 道、发布 z 冻结 ≥3 拍或 z=None 的拍判
    「历史作废」——跳变后 ``clear_s`` 内的极值拍（报大 2 倍/报小）单独记
    「作废可截」。**不清拟合序列**（那是真值，作废只管发布 t）——回答：
    报大/报小粗尾里有多少落在跳变后 0.3 s 内、会被作废语义截住。
    """
    series = collections.defaultdict(list)
    st = {b: collections.Counter() for b in bands}
    ratios = {b: [] for b in bands}
    prev: dict = {}
    invalid_until: dict = {}
    frozen_run: collections.Counter = collections.Counter()
    for r in ticks:
        ts = r["ts_mono_ns"] / 1e9
        for oid, x, t, z, src, zg, h, clip, cy in _cars(r):
            p = prev.get(oid)
            jump = False
            if p is not None:
                if zg is not None and p["zg"] is not None and not clip \
                        and not p["clip"] \
                        and abs(zg - p["zg"]) > max(5.0, 0.3 * p["zg"]):
                    jump = True
                elif z is not None and p["z"] is not None \
                        and abs(z - p["z"]) > 5.0:
                    jump = True
                elif x is not None and p["x"] is not None \
                        and abs(x - p["x"]) > 0.5 * LANE_W:
                    jump = True
                elif z is None:
                    jump = True
                elif z == p["z"]:
                    frozen_run[oid] += 1
                    if frozen_run[oid] >= 3:
                        jump = True       # 冻结 ≥3 拍：测距已死
                else:
                    frozen_run[oid] = 0
            prev[oid] = {"z": z, "zg": zg, "x": x, "clip": clip}
            if invalidated:
                if jump:
                    invalid_until[oid] = ts + clear_s
                    frozen_run[oid] = 0
                dead = invalid_until.get(oid, 0) > ts
            else:
                dead = False
            seq = series[oid]
            seq.append((ts, None if (zg is None or clip) else zg))
            seq = seq[-40:]
            series[oid] = seq
            if len(seq) < 12 or any(v is None for _, v in seq[-12:]):
                continue
            got = _fit([v for _, v in seq], [a for a, _ in seq])
            if got is None:
                continue
            zf, vc = got
            t_true = zf / vc
            for lo, hi in bands:
                if lo < zg <= hi:
                    c = st[(lo, hi)]
                    if t is None:
                        c["t None"] += 1
                        if t_true < 3.0:
                            c["t None 且真值<3s"] += 1
                    else:
                        c["有 t"] += 1
                        ratios[(lo, hi)].append(t / t_true)
                        if t > 1.5 * t_true:
                            c["t>1.5倍"] += 1
                        if t > 2.0 * t_true:
                            c["t>2倍"] += 1
                        if t < 0.67 * t_true:
                            c["t<0.67倍"] += 1
                        if dead and (t > 2.0 * t_true or t < 0.67 * t_true):
                            # 极值拍落在跳变后 clear_s 内：作废语义会把它截住
                            c["作废可截"] += 1
                    break
    return st, ratios


def _fmt(row) -> str:
    if row is None:
        return "—"
    fid, zg, z, t, src, why = row
    return f"fid{fid} zg {zg:.1f} 发布z {z} t {t} src {src} {why}"


def analyze(sess: str) -> None:
    ticks, stage_s = load(sess)
    if not ticks:
        print(f"== {sess} 无拍")
        return
    print(f"== {sess}  阶段内 {stage_s:.0f}s  拍 {len(ticks)}")
    stab, frozen = stability(ticks)
    if stab["t 可比拍"]:
        print(f"   发布 t 相邻拍翻倍/减半 {stab['t 单拍翻倍/减半']}/"
              f"{stab['t 可比拍']} = "
              f"{stab['t 单拍翻倍/减半'] / stab['t 可比拍']:.1%}")
    if frozen:
        top = collections.Counter(frozen).most_common(5)
        print(f"   发布 z 冻结段 {len(frozen)}，长度分布 {top}")
    if stab["zg 可比拍"]:
        print(f"   接地距离跳变(>5m 或 >30%) {stab['zg 跳变拍']}/"
              f"{stab['zg 可比拍']} = "
              f"{stab['zg 跳变拍'] / stab['zg 可比拍']:.1%}")
    if stab["x 可比拍"]:
        print(f"   横向跳变(>1m) {stab['x 跳变拍']}/{stab['x 可比拍']} = "
              f"{stab['x 跳变拍'] / stab['x 可比拍']:.1%}")
        s0 = stab["z稳"] or 1
        s1 = stab["z抖"] or 1
        print(f"   横向跳变条件率：z 稳 {stab['z稳且x跳'] / s0:.1%}（{s0} 拍）"
              f" vs z 抖 {stab['z抖且x跳'] / s1:.1%}（{s1} 拍）")

    ev = near_field(ticks)
    print(f"   近距事件 {len(ev)} 次（zg≤12m 且 ≤0.6 道）")
    for oid, r20, r12, rc in ev:
        print(f"   - oid{oid} 跨20m {_fmt(r20)}")
        print(f"           跨12m {_fmt(r12)}")
        print(f"           最近  {_fmt(rc)}")

    st, ratios = t_bias(ticks)
    print("   发布 t 对真值（接地距离拟合闭合速度）：")
    for b, c in st.items():
        n = c["有 t"] + c["t None"]
        if not n:
            continue
        rs = sorted(ratios[b])
        med = rs[len(rs) // 2] if rs else float("nan")
        print(f"   - {b[0]:.0f}~{b[1]:.0f}m 拍 {n}: t None {c['t None']} "
              f"({c['t None'] / n:.0%}，其中真值<3s {c['t None 且真值<3s']}) | "
              f"中位比 {med:.2f} | 报大1.5倍 {c['t>1.5倍']} "
              f"({c['t>1.5倍'] / max(1, c['有 t']):.0%}) | 报大2倍 "
              f"{c['t>2倍']} | 报小 {c['t<0.67倍']} "
              f"({c['t<0.67倍'] / max(1, c['有 t']):.0%})")

    st2, ratios2 = t_bias(ticks, invalidated=True)
    print("   跳变作废影子（纵向 >5m/横向 >0.5 道跳变、发布 z 跳/冻结/断供"
          "后 clear_s 内的极值拍会被作废截住）：")
    for b, c in st2.items():
        n = c["有 t"] + c["t None"]
        if not n:
            continue
        cut = c["作废可截"]
        tot = c["t>2倍"] + c["t<0.67倍"]
        print(f"   - {b[0]:.0f}~{b[1]:.0f}m 拍 {n}: 报大2倍 {c['t>2倍']}"
              f" | 报小 {c['t<0.67倍']} | 极值合计 {tot}，"
              f"其中作废可截 {cut}（{cut / max(1, tot):.0%}）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    args = ap.parse_args()
    for s in args.sessions:
        analyze(s)


if __name__ == "__main__":
    main()