"""近场身份连续性的离线真值小集（speedrush，离线）。

**问什么**：人工 120 分超车事件里，被打分车在判定前 1.5 s 的连续拍之间，
跟踪编号在哪一拍断、断时新旧两读数差多少、断后接上的新号是不是原来那辆
物理车。程序先给候选断链与重挂候选（位置/几何判据），最终确认靠标注帧
人工目视——同编号跨拍不保证同一物理车（README 坑点），必须回到原始帧。

口径（与 probe_manual_shadow_pass.py 同源）：

- 判定拍 = `dispatchPlayerCarScore ... score=120 type=overtake` 时刻对到的
  trace 最近拍；被打分车 = 判定拍 |t|≤0.7、横向 |x−ego|≤1.2 的最近真车（非 grid）。
- 人工窗口必须按 `debug/speedrush/*_manual_p*/meta.json` 起止裁剪。
- 车表每拍从 `scene.objs` 取米制真车（id≥0），x=场景横位（道）、t=到站秒；
  纵深 z 用框底接地距离（`car_pos[].cy`+`car_h[].h/2`，焦距/机高逐拍取
  trace，只认 1.0~1.6 m）——metric 源的 objs z 在人工 trace 里普遍为 None。
- 候选链回溯（程序侧，仅供目视聚焦）：从判定拍 oid 往回，缺拍 ≤4 拍视为
  闪烁；真断时在前 3 拍找位置接近的替代 oid（|Δx|≤0.6 道且框底距离不增、
  增幅 ≤6 m），允许续接 1 次。链只是候选，接错/漏接由标注帧裁决。
- 1.2 s 处三分类：判定前 1.2 s 拍上，链里任一 oid 在表 → same/renewed
  （按是否仍是判定拍 oid），否则 none。

用法::

    .venv/Scripts/python.exe -X utf8 probe_identity_truth.py \
        --trace <speedrush.jsonl> [--trace ...] --log <g112.log> \
        [--out <目录>] [--json <路径>]
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import re
import statistics

LANE_W = 3.5
BACK_S = 1.7          # 判定拍前回溯秒数
FLICKER_TICKS = 4     # 缺拍 ≤4 拍视为闪烁
GAP_NEAR_TICKS = 3    # 断点后向前找替代 oid 的拍窗
REJOIN_X_LANE = 0.6   # 替代 oid 的横位接近门（道）
REJOIN_Z_JUMP = 6.0   # 替代 oid 的框底距离增幅门（m）
VPX = 640.7           # gate0.json 横向像素尺子（只画图用）
Y_H = 354.2
A_X = 0.305
TS_RE = re.compile(r"\[(\d{4}\.\d{2}\.\d{2})-(\d{2}\.\d{2}\.\d{2}):(\d{3})\]")
EV_RE = re.compile(r"dispatchPlayerCarScore: type=(\w+), score=(\d+)")


def _events(log_path, t0, t1):
    out = []
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TS_RE.search(line)
            e = EV_RE.search(line)
            if not (m and e) or e.group(2) != "120":
                continue
            stamp = dt.datetime.strptime(
                m.group(1) + " " + m.group(2) + "." + m.group(3),
                "%Y.%m.%d %H.%M.%S.%f").timestamp() * 1e9
            if t0 <= stamp <= t1:
                out.append((stamp, e.group(1)))
    return out


def _load(path):
    ticks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("fid") is not None and rec.get("scene"):
                ticks.append(rec)
    ticks.sort(key=lambda r: r["ts_wall_ns"])
    return ticks


def _manual_windows(debug_root):
    """[(session, phase, start_ns, end_ns, dir)]，按 meta.json 起止。"""
    sr = os.path.join(debug_root, "speedrush")
    out = []
    if not os.path.isdir(sr):
        return out
    for name in sorted(os.listdir(sr)):
        if "_manual_p" not in name:
            continue
        meta_p = os.path.join(sr, name, "meta.json")
        if not os.path.isfile(meta_p):
            continue
        with open(meta_p, encoding="utf-8") as fh:
            meta = json.load(fh)
        s = dt.datetime.fromisoformat(meta["started_at"]).timestamp() * 1e9
        e = dt.datetime.fromisoformat(meta["ended_at"]).timestamp() * 1e9
        out.append((meta["session"], name, s, e,
                    os.path.join(sr, name)))
    return out


def _ground_z(rec, oid_index):
    """框底接地距离（m）：焦距·机高 ÷（框底行 − 地平线行）。

    机高逐拍取 trace 且只认 1.0~1.6 m（README 口径）；焦距取逐帧 MoGe 值，
    缺则回退标定 focal_px。返回 (zg, bottom)，无框或分母退化给 (None, None)。
    """
    cam_h = rec.get("dgeo_cam_h")
    if cam_h is None or not (1.0 <= cam_h <= 1.6):
        return None, None
    focal = rec.get("dgeo_focal") or 807.7
    y_h = 354.2
    d = oid_index.get("h")
    cy = oid_index.get("cy")
    if not d or cy is None:
        return None, None
    bottom = cy + d / 2
    denom = bottom - y_h
    if denom <= 20:
        return None, bottom
    return focal * cam_h / denom, bottom


def _cars(rec):
    """oid → {x, t, src, seen, cy, h, xl, zg}；只收米制真车（id≥0）。"""
    out = {}
    for o in (rec.get("scene") or {}).get("objs") or []:
        if not o or o[0] != "car" or len(o) < 16 or o[1] is None:
            continue
        if o[14] is None or o[14] < 0:
            continue
        out[o[14]] = {"x": o[1] / LANE_W, "t": o[2], "src": o[6],
                      "seen": bool(o[15]) if o[15] is not None else None}
    pos = {d["id"]: d for d in (rec.get("car_pos") or [])}
    hh = {d["id"]: d for d in (rec.get("car_h") or [])}
    for oid, c in out.items():
        p = pos.get(oid)
        if p:
            c["cy"] = p.get("cy")
            c["xl"] = p.get("x")
        q = hh.get(oid)
        if q:
            c["h"] = q.get("h")
        zg, bottom = _ground_z({"dgeo_cam_h": rec.get("dgeo_cam_h"),
                                "dgeo_focal": rec.get("dgeo_focal"),
                                **c}, c)
        c["zg"] = zg
    return out


def _nearest(ticks, ts_list, t):
    i = bisect.bisect_left(ts_list, t)
    cand = [j for j in (i - 1, i, i + 1) if 0 <= j < len(ticks)]
    if not cand:
        return None
    return min(cand, key=lambda j: abs(ts_list[j] - t))


def _chain_back(ticks, ts_list, cars_cache, j, oid_j):
    """从判定拍往前拼候选链。返回段列表与 1.2 s 拍分类。

    段 = {oid, i_from, i_to}（trace 索引，含端点，i_to>i_from，往回拼）。
    """
    horizon = int(BACK_S / 0.05) + 6  # 21 Hz 粗估 + 余量
    segs = [{"oid": oid_j, "i_from": j, "i_to": j}]
    cur = oid_j
    i = j - 1
    last_read = None  # 当前段最后（最早）一次读数 dict
    stop = None
    while i >= 0 and j - i <= horizon:
        cars = cars_cache[i]
        ent = cars.get(cur)
        if ent is not None:
            segs[-1]["i_from"] = i
            last_read = ent
            i -= 1
            continue
        # 缺拍：往回看 FLICKER_TICKS 内是否重现（闪烁，同号续）
        reapp = None
        for k in range(i, max(i - FLICKER_TICKS, -1), -1):
            if cur in cars_cache[k]:
                reapp = k
                break
        if reapp is not None:
            segs[-1]["i_from"] = reapp
            last_read = cars_cache[reapp][cur]
            i = reapp - 1
            continue
        # 真断：向前（更早）GAP_NEAR_TICKS 拍里找位置接近的替代 oid
        t_gap = ts_list[i]
        best, best_cost = None, None
        for k in range(i, max(i - GAP_NEAR_TICKS, -1), -1):
            for oid, ent2 in cars_cache[k].items():
                if oid == cur or any(s["oid"] == oid for s in segs):
                    continue
                if ent2.get("t") is None or last_read is None:
                    continue
                dx = abs(ent2["x"] - last_read["x"])
                if dx > REJOIN_X_LANE:
                    continue
                zg2, zg1 = ent2.get("zg"), last_read.get("zg")
                if zg2 is not None and zg1 is not None:
                    # 更早的拍应更远：允许多 6 m、不许凭空变近 3 m 以上
                    if zg2 - zg1 > REJOIN_Z_JUMP or zg1 - zg2 > 3.0:
                        continue
                    zok = True
                else:
                    zok = False
                dt_s = (ts_list[k] - t_gap) / 1e9
                tt1, tt2 = last_read.get("t"), ent2.get("t")
                tok = (tt1 is not None and tt2 is not None
                       and -0.4 <= tt2 - tt1 <= 0.4)
                if not (zok or tok):
                    continue
                cost = dx * 10 + (0 if zok else 2) + (0 if tok else 2) \
                    + (i - k)
                if best_cost is None or cost < best_cost:
                    best, best_cost = (oid, k), cost
        if best is None:
            stop = ("no_candidate", i)
            break
        # 并存检测：旧号在接续点之后是否仍活着 ≥2 拍——活着说明新旧号
        # 是同时存在的两辆车（fid3241/3897 实锤），这次接续是错的
        coexist = 0
        for k in range(i, min(i + 4, j + 1)):
            if cur in cars_cache[k]:
                coexist += 1
        # 特效黑洞：断窗 ≥0.3 s 且断窗前后 ego_m 单窗跳 >2 m（全屏特效帧
        # 的实测特征：感知全瞎 + 自车读数跳变，fid2022/fid4193）
        gap_s = (ts_list[i] - ts_list[best[1]]) / 1e9
        fx = False
        if gap_s >= 0.3:
            eg1 = ((ticks[i].get("scene") or {}).get("ego_m"))
            eg2 = ((ticks[best[1]].get("scene") or {}).get("ego_m"))
            fx = (eg1 is not None and eg2 is not None
                  and abs(eg1 - eg2) > 2.0)
        segs.append({"oid": best[0], "i_from": best[1], "i_to": best[1],
                     "coexist": coexist, "fx_gap": fx})
        cur = best[0]
        i = best[1] - 1
    # 1.2 s 拍分类
    j12 = _nearest(ticks, ts_list, ts_list[j] - int(1.2e9))
    cls, cls_oid = "no_trace", None
    if j12 is not None:
        c12 = cars_cache[j12]
        cls, cls_oid = "none", None
        if cur in c12:
            cls, cls_oid = "same", cur
        else:
            # 链上任一早段 oid 仍在表 → 同号可用（宽限/续命也算在表）
            for s in segs:
                if s["oid"] != cur and s["oid"] in c12:
                    cls, cls_oid = "same", s["oid"]
                    break
            if cls == "none":
                # 位置接近链外推读数的新 oid → renewed 候选
                anchor = None
                for s in segs:
                    ent_s = cars_cache[s["i_from"]].get(s["oid"])
                    if ent_s is not None and ent_s.get("t") is not None:
                        anchor = ent_s
                        break
                if anchor is not None:
                    for oid, ent2 in c12.items():
                        if ent2.get("t") is None:
                            continue
                        dx = abs(ent2["x"] - anchor["x"])
                        tt = ent2["t"] - anchor["t"]
                        if dx <= 0.75 and -0.5 <= tt <= 0.5:
                            cls, cls_oid = "renewed", oid
                            break
    return segs, stop, (j12, cls, cls_oid)


def analyze(trace_paths, log_path, debug_root):
    windows = _manual_windows(debug_root)
    by_session = {}
    for w in windows:
        by_session.setdefault(w[0], []).append(w)
    all_rows = []
    for tp in trace_paths:
        ticks = _load(tp)
        if not ticks:
            print(f"[skip] {tp}")
            continue
        ts_list = [r["ts_wall_ns"] for r in ticks]
        session = ticks[0].get("session") or os.path.basename(tp)
        wins = by_session.get(session)
        if not wins:
            print(f"[skip] {tp}: 无 manual 窗口")
            continue
        cars_cache = None
        for _, wname, s_ns, e_ns, wdir in wins:
            ev = [(ts, k) for ts, k in _events(log_path, s_ns, e_ns)
                  if k == "overtake"]
            if not ev:
                continue
            if cars_cache is None:
                cars_cache = [_cars(r) for r in ticks]
            for ts, _ in ev:
                j = _nearest(ticks, ts_list, ts)
                if j is None or not (s_ns - 1e9 <= ts_list[j] <= e_ns + 1e9):
                    continue
                rec = ticks[j]
                ego = ((rec.get("scene") or {}).get("ego_m") or 0) / LANE_W
                cands = [(oid, c) for oid, c in cars_cache[j].items()
                         if c.get("t") is not None and abs(c["t"]) <= 0.7
                         and abs(c["x"] - ego) <= 1.2]
                if not cands:
                    all_rows.append({"session": session, "win": wname,
                                     "t": ts, "judge_fid": ticks[j]["fid"],
                                     "oid": None, "cls": "no_judge_car"})
                    continue
                oid_j = min(cands, key=lambda kv: abs(kv[1]["t"]))[0]
                segs, stop, (j12, cls, cls_oid) = _chain_back(
                    ticks, ts_list, cars_cache, j, oid_j)
                row = {"session": session, "win": wname, "t": ts,
                       "judge_ts": ts_list[j],
                       "judge_fid": ticks[j]["fid"], "oid": oid_j,
                       "cls": cls, "cls_oid": cls_oid,
                       "segs": [{"oid": s["oid"],
                                 "i_from": s["i_from"], "i_to": s["i_to"],
                                 "fid_from": ticks[s["i_from"]]["fid"],
                                 "fid_to": ticks[s["i_to"]]["fid"],
                                 "span": s["i_to"] - s["i_from"] + 1,
                                 "coexist": s.get("coexist", 0),
                                 "fx_gap": bool(s.get("fx_gap"))}
                                for s in segs],
                       "stop": stop}
                if j12 is not None:
                    row["fid_1p2"] = ticks[j12]["fid"]
                all_rows.append(row)
    return all_rows


def _print_report(rows):
    n = len(rows)
    judged = [r for r in rows if r.get("oid") is not None]
    print(f"== 人工 120 overtake 事件 {n} 个（可判 {len(judged)}）")
    cnt = {}
    for r in judged:
        cnt[r["cls"]] = cnt.get(r["cls"], 0) + 1
    for k in sorted(cnt):
        print(f"   1.2s 处 {k}: {cnt[k]}")
    print()
    for r in judged:
        segs = " -> ".join(f"{s['oid']}[{s['fid_from']}~{s['fid_to']}]"
                           for s in r["segs"])
        print(f"  {r['win']} 判定fid {r['judge_fid']} 判定oid {r['oid']}"
              f"  1.2s={r['cls']}"
              f"{'(oid ' + str(r['cls_oid']) + ')' if r['cls_oid'] else ''}"
              f"  链 {segs}"
              f"{'  停:' + str(r['stop']) if r['stop'] else ''}")


def _chain_max_xjump(rows, cars_by_session):
    """same 事件的链内最大相邻横位跳（道），并号嫌疑排序用。

    段索引直接用 analyze 存的 i_from/i_to（拍索引，ts 排序后全局唯一）；
    fid 跨回合重复，不能反查。
    """
    out = {}
    for r in rows:
        if r.get("cls") != "same":
            continue
        cars_cache = cars_by_session[r["session"]]
        best = 0.0
        for s in r["segs"]:
            prev = None
            for i in range(s["i_to"], s["i_from"] - 1, -1):
                ent = cars_cache[i].get(s["oid"])
                if ent is None:
                    continue
                if prev is not None:
                    best = max(best, abs(ent["x"] - prev["x"]))
                prev = ent
        out[(r["win"], r["judge_fid"])] = best
    return out


def _pick_frames(row, ticks, ts_list):
    """按事件形态选帧索引（判定拍附近，时间倒序无所谓）。

    判定拍用 judge_ts 定位——fid 只在 stage 内唯一、跨回合重复，按 fid
    反查会撞到别的回合的拍（20261109_103934 的 fid 2022 实测两回合各一）。
    """
    j = bisect.bisect_left(ts_list, row["judge_ts"])
    if j >= len(ts_list) or abs(ts_list[j] - row["judge_ts"]) > int(0.2e9):
        j = j - 1 if j > 0 else 0
    def at(sec_back):
        # 只取判定拍**之前**的目标时刻最近拍（特效/回放帧在判定之后，不许混入）
        t = ts_list[j] - int(sec_back * 1e9)
        k = bisect.bisect_right(ts_list, t) - 1
        return k if k >= 0 else None
    segs = row.get("segs") or []
    if row.get("cls") in ("none", "no_judge_car"):
        cand = [1.6, 1.2, 0.9, 0.6, 0.3, 0.0]
    elif len(segs) >= 2:
        # 断点 = 新段最早拍；旧段最后拍在断点前
        cand = [1.6, 1.2, 0.7, 0.4, 0.15, 0.0]
    else:
        cand = [1.5, 1.0, 0.5, 0.0]
    out = []
    for sec in cand:
        k = at(sec)
        if k is not None and (not out or k != out[-1]):
            out.append((k, sec))
    return out


def _draw_box(d, ent, color, width, label):
    """框底/框顶行画全宽横线 + 文字行（左侧固定列）。

    横向透视反推在近场发散（xl 0.74 道反算 cx 出画，实测 fid 2022），
    只有框底行是已验证的可靠量（README 口径）；横向认车靠目视对横线。
    """
    cy, h = ent.get("cy"), ent.get("h")
    d.text((10, label[1]), label[0], fill=color)
    if cy is None:
        return
    if h:
        top, bot = cy - h / 2, cy + h / 2
        d.line([(0, bot), (1279, bot)], fill=color, width=width)
        d.line([(0, top), (1279, top)], fill=color, width=1)
    else:
        d.line([(0, cy), (1279, cy)], fill=color, width=width)


def _render_event(row, ticks, ts_list, cars_cache, frame_dir, out_path):
    from PIL import Image, ImageDraw, ImageFont
    frames = _pick_frames(row, ticks, ts_list)
    chain = {s["oid"]: (s["fid_from"], s["fid_to"]) for s in (row.get("segs") or [])}
    judge_oid = row.get("oid")
    old_oids = {s["oid"] for s in (row.get("segs") or [])} - {judge_oid}
    cell_w, cell_h = 640, 360
    ncol = 2
    nrow = (len(frames) + ncol - 1) // ncol
    sheet = Image.new("RGB", (cell_w * ncol, cell_h * nrow), (16, 16, 16))
    try:
        font = ImageFont.load_default(size=16)
        font_s = ImageFont.load_default(size=13)
    except TypeError:
        font = font_s = ImageFont.load_default()
    fid2file = {}
    fj = os.path.join(frame_dir, "frames.jsonl")
    if os.path.isfile(fj):
        with open(fj, encoding="utf-8") as fh:
            for line in fh:
                rr = json.loads(line)
                if rr.get("frame_id") is not None:
                    fid2file.setdefault(rr["frame_id"], rr.get("file"))
    for n, (i, sec) in enumerate(frames):
        rec = ticks[i]
        fid = rec["fid"]
        img = None
        rel = fid2file.get(fid)
        if rel and os.path.isfile(os.path.join(frame_dir, rel)):
            img = Image.open(os.path.join(frame_dir, rel))
        if img is None:
            continue
        # 标注画在原图坐标（1280×720）再缩放——近场车 cy 500~720+，
        # 直接画在缩放图上会整体出界
        d = ImageDraw.Draw(img)
        # 文字行按框底行排序，颜色区分链内/链外；label=(文本, 行y)
        shown = sorted(cars_cache[i].items(),
                       key=lambda kv: (kv[1].get("cy") or 0, kv[0]))
        line_y = 30
        for oid, ent in shown:
            if oid == judge_oid:
                color, width = (255, 60, 40), 4
            elif oid in old_oids:
                color, width = (255, 170, 30), 4
            else:
                color, width = (240, 240, 240), 2
            zg = f" zg{ent['zg']:.1f}" if ent.get("zg") is not None else ""
            t_s = ent.get("t")
            t_part = f" t{t_s:.2f}" if t_s is not None else " t--"
            txt = (f"id{oid}{'*' if not ent.get('seen') else ''}"
                   f" x{ent['x']:.2f}" + t_part + zg)
            _draw_box(d, ent, color, width, (txt, line_y))
            line_y += 24
        d.text((10, 6), f"fid={fid} t-{sec:.2f}s", fill=(255, 255, 80), font=font)
        cell = img.resize((cell_w, cell_h))
        sheet.paste(cell, ((n % ncol) * cell_w, (n // ncol) * cell_h))
    sheet.save(out_path, quality=88)
    return out_path


def make_figs(rows, ticks_by_session, cars_by_session, win_dir_by_session,
              out_dir, max_same=3):
    os.makedirs(out_dir, exist_ok=True)
    xj = _chain_max_xjump(rows, cars_by_session)
    broken = [r for r in rows if r.get("cls") in ("none", "renewed")]
    two_seg = [r for r in rows if r.get("cls") == "same" and len(r.get("segs") or []) >= 2]
    same = sorted([r for r in rows if r.get("cls") == "same"
                   and len(r.get("segs") or []) == 1],
                  key=lambda r: -xj.get((r["win"], r["judge_fid"]), 0))
    picked = broken + two_seg + same[:max_same]
    print(f"[figs] 出图事件 {len(picked)} 个"
          f"（断号 {len(broken)} + 二段链 {len(two_seg)} + same 对照 {min(max_same, len(same))}）")
    for r in picked:
        session = r["session"]
        wdir = win_dir_by_session.get((session, r["win"]))
        if wdir is None:
            print(f"[skip] {r['win']}: 找不到帧目录")
            continue
        out_p = os.path.join(out_dir, f"{r['win']}_fid{r['judge_fid']}.jpg")
        _render_event(r, ticks_by_session[session],
                      [t["ts_wall_ns"] for t in ticks_by_session[session]],
                      cars_by_session[session], wdir, out_p)
        print(f"  {os.path.basename(out_p)}  cls={r['cls']}"
              f"  链内最大横跳 {xj.get((r['win'], r['judge_fid']), 0):.2f} 道")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", required=True)
    ap.add_argument("--log", default=os.environ.get("MAAR_G112_LOG"))
    ap.add_argument("--debug-root",
                    default=os.path.expandvars(r"%APPDATA%\MaaRacingMaster\debug"))
    ap.add_argument("--json")
    ap.add_argument("--figs")
    ap.add_argument("--max-same", type=int, default=3)
    a = ap.parse_args()
    if not a.log:
        raise SystemExit("缺少游戏日志：设 MAAR_G112_LOG 或传 --log")
    rows = analyze(a.trace, a.log, a.debug_root)
    _print_report(rows)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"\n[json] {a.json}")
    if a.figs:
        debug_root = a.debug_root
        windows = _manual_windows(debug_root)
        ticks_by_session, cars_by_session = {}, {}
        win_dir_by_session = {}
        for tp in a.trace:
            ticks = _load(tp)
            session = ticks[0].get("session")
            ticks_by_session[session] = ticks
            cars_by_session[session] = [_cars(r) for r in ticks]
        for session, wname, _s, _e, wdir in windows:
            win_dir_by_session[(session, wname)] = wdir
        make_figs(rows, ticks_by_session, cars_by_session,
                  win_dir_by_session, a.figs, a.max_same)
