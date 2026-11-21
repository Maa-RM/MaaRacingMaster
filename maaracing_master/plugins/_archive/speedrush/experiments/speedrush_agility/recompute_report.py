"""生成 speedrush 目标正确性基线复算报告（口径见 metrics.py）。

用法：
    .venv/Scripts/python.exe maaracing_master/plugins/_archive/speedrush/experiments/speedrush_agility/recompute_report.py \
        --trace <speedrush.jsonl> --pads-dir <debug/speedrush 目录>

输出 RECOMPUTE_REPORT.md 与 RECOMPUTE_REPORT.json，二者同源：
报告里的每个数都由本脚本按 ``metrics.PARAMS`` 算出，不读任何旧摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics as M  # noqa: E402

P = M.PARAMS


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--pads-dir", default="")
    ap.add_argument("--session-id", default="",
                    help="人工记录所属会话；默认取 trace 文件名前缀")
    ap.add_argument("--compare", help="同口径已保存 JSON；不相同则退出失败，不覆盖报告")
    ap.add_argument("--emit-json", action="store_true",
                    help="只把结果 JSON 打到 stdout，不写文件（供对账脚本调用）")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()
    if args.pads_dir and not os.path.isdir(args.pads_dir):
        ap.error("手柄目录不存在")
    expected = None
    if args.compare:
        with open(args.compare, encoding="utf-8") as saved:
            expected = json.load(saved)
    if not args.session_id:
        trace_dir = os.path.dirname(os.path.abspath(args.trace))
        args.session_id = os.path.basename(os.path.dirname(trace_dir))

    rows, sha = M.load_trace(args.trace)
    segs = M.split_segments(rows)
    rep: dict = {"caliber_version": M.CALIBER_VERSION,
                 "input": {"trace": os.path.basename(args.trace), "sha256_16": sha,
                           "pads_dir": os.path.basename(args.pads_dir.rstrip("/\\"))
                                        if args.pads_dir else None},
                 "params": {k: (list(v) if isinstance(v, tuple) else v)
                            for k, v in P.items()},
                 "segments": [], "manual_inputs": [],
                 "mode_assignment": {"r1": "program", "r2": "human-shadow"},
                 "implementation_sha256_16": hashlib.sha256(
                     open(M.__file__, "rb").read() + open(__file__, "rb").read()
                 ).hexdigest()[:16]}

    md: list[str] = []
    md.append("# speedrush 目标正确性·复算报告\n")
    md.append(f"- 口径版本：`{M.CALIBER_VERSION}`")
    md.append(f"- 输入：`{os.path.basename(args.trace)}` sha256[:16]=`{sha}`")
    md.append(f"- 参数：`{json.dumps(rep['params'], ensure_ascii=False)}`")
    md.append("- 模式约定：本组实录 r1 为程序驾驶，r2 为人工驾驶；人工局程序命令仅作反事实记录。")
    md.append(f"- 计算实现指纹：`{rep['implementation_sha256_16']}`")
    md.append("- 本报告所有数字由 `recompute_report.py` 从原始 trace 直接计算；"
              "未读取任何旧脚本摘要或方案表格。\n")

    # ---- A. 分段与时间基 ----
    md.append("## A. 分段与时间基\n")
    md.append("| 段 | 拍数 | 真实时长 s | dt p50 | dt p99 | dt max |")
    md.append("|---|---:|---:|---:|---:|---:|")
    seg_meta = []
    for seg in segs:
        dts = [d for i in range(len(seg)) if (d := M.dt_seconds(seg, i)) is not None]
        dts_s = sorted(dts)
        item = {"label": M.seg_label(seg), "ticks": len(seg),
                "duration_s": round(sum(dts), 2),
                "dt_p50": round(st.median(dts), 3) if dts else None,
                "dt_p99": round(dts_s[int(len(dts_s) * 0.99)], 3) if dts else None,
                "dt_max": round(max(dts), 3) if dts else None}
        seg_meta.append(item)
        md.append(f"| {item['label']} | {item['ticks']} | {item['duration_s']} | "
                  f"{item['dt_p50']} | {item['dt_p99']} | {item['dt_max']} |")
    rep["segments"] = seg_meta
    md.append("")

    # ---- B. 反转率（多阈值，不选一个）----
    md.append("## B. 反转率（滞回口径，θ 全量报）\n")
    md.append("滞回：反向累计位移达到 θ 才确认一次反转；缺读、执行重基标签和路线参考系纪元变化处断段。分母仅累计片段内相邻有效拍时长。未记录的来源重锚仍不可判，不据此证明真实轨迹反转。\n")
    series = {
        "x_target（目标；人工局为反事实）": lambda r: M.num(r.get("x_target")),
        "executed_lane（横位估计）": lambda r: M.num(r.get("executed_lane")),
        "lane_obs.x（视觉横位）": M.lane_obs_x,
    }
    rev_tbl = []
    for name, getter in series.items():
        md.append(f"### {name}\n")
        md.append("| 段 | " + " | ".join(f"θ={t} 次/秒" for t in P["rev_theta_sweep"])
                  + f" | θ={P['run_summary_theta']} 确认间隔中位拍 | 断段 |")
        md.append("|---|" + "---:|" * len(P["rev_theta_sweep"]) + "---:|---:|")
        for seg in segs:
            cells = []
            row = {"label": M.seg_label(seg), "series": name, "by_theta": {}}
            for t in P["rev_theta_sweep"]:
                s = M.rev_stat(seg, getter, t)
                row["by_theta"][str(t)] = s.as_dict()
                cells.append(pct(s.per_s))
            mid = M.rev_stat(seg, getter, P["run_summary_theta"])
            md.append(f"| {M.seg_label(seg)} | " + " | ".join(cells)
                      + f" | {mid.median_run_ticks} | {mid.breaks} |")
            rev_tbl.append(row)
        md.append("")
    rep["reversals"] = rev_tbl

    # ---- C. 引用一致（车类动作，三态）----
    md.append("## C. 引用一致（仅 graze/pass：`hit_oid` 非空的拍）\n")
    md.append("三态：命中身份在规划场景 / 仅在对象表 / 两边都没有。"
              "分母是有身份拍，不含无身份拍——覆盖面单列。\n")
    md.append("| 段 | 全段拍 | 有身份拍 | 占比 | 在场景 | 仅对象表 | 都无 |"
              " 仅对象表中 stale |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    ref_rows = []
    reference_events = []
    for seg in segs:
        n = len(seg)
        nh = sc = ot = none = stale_ot = 0
        for r in seg:
            h = M.sel(r).get("hit_oid")
            if h is None:
                continue
            nh += 1
            sm, tm = M.scene_map(r), M.otab_map(r)
            if h in sm:
                sc += 1
            elif h in tm:
                ot += 1
                if tm[h][8] == "stale":
                    stale_ot += 1
            else:
                none += 1
            if h not in sm:
                entry = tm.get(h)
                source = next((c for c in r.get("carsrc") or [] if c.get("id") == h), None)
                reference_events.append({"label": M.seg_label(seg), "fid": r.get("fid"),
                    "ts_ns": r.get("ts_ns"), "oid": h, "why": r.get("sel_why"),
                    "state": "otab_only" if entry else "neither", "otab_safety_why": entry[8] if entry else None,
                    "carsrc": source, "hold_state": M.sel(r).get("graze_hold"),
                    "target": M.sel(r).get("target"), "waypoints": M.sel(r).get("wp")})
        row = {"label": M.seg_label(seg), "ticks": n, "with_identity": nh,
               "coverage": round(nh / n, 4) if n else None,
               "in_scene": sc, "otab_only": ot, "neither": none,
               "otab_only_stale": stale_ot}
        ref_rows.append(row)
        md.append(f"| {row['label']} | {n} | {nh} | {pct(row['coverage'])} | {sc} | "
                  f"{ot} | {none} | {stale_ot} |")
    rep["reference"] = ref_rows
    rep["reference_events"] = reference_events
    md.append("")

    # ---- D. 目标身份还原（平行表）----
    md.append("引用缺席事件明细见 JSON 的 reference_events。场景过滤、对象保留与安全让位保持均可能导致缺席，不能直接算成错误引用。\n")
    md.append("## D. 目标身份候选匹配（`sel_dbg.rw/coin` 与 `src_elig.*_sel` 同序平行表）\n")
    md.append("判据：与本拍 `sel_dbg.target` 相差 ≤ `identity_candidate_lane` 的候选中，唯一命中记「唯一」，"
              "多于一个记「歧义」，零个记「无匹配」。这是邻近候选计数；唯一候选也未经独立身份确认，不能记为身份正确。\n")
    md.append("| 层 | 段 | 拍数 | 平行表长度一致 | 唯一命中 | 歧义 | 无匹配 |")
    md.append("|---|---|---:|---:|---:|---:|---:|")
    id_rows = []
    for layer, key, selkey in ((1, "rw", "rw_sel"), (3, "coin", "coin_sel")):
        for seg in segs:
            tot = eq = uniq = ambi = nomatch = 0
            for r in seg:
                if r.get("sel_layer") != layer:
                    continue
                d = M.sel(r)
                items = [x for x in (d.get(key) or [])
                         if isinstance(x, (list, tuple)) and len(x) >= 2]
                tgt = M.num(d.get("target"))
                if not items or tgt is None:
                    continue
                tot += 1
                ids = [s[0] for s in ((d.get("src_elig") or {}).get(selkey) or [])
                       if isinstance(s, (list, tuple)) and s]
                if len(ids) != len(items):
                    continue
                eq += 1
                hit = [ids[j] for j, it in enumerate(items)
                       if M.num(it[0]) is not None
                       and abs(M.num(it[0]) - tgt) <= P["identity_candidate_lane"]]
                if len(hit) == 1:
                    uniq += 1
                elif len(hit) > 1:
                    ambi += 1
                else:
                    nomatch += 1
            if tot:
                row = {"layer": layer, "label": M.seg_label(seg), "ticks": tot,
                       "parallel_ok": eq, "unique": uniq, "ambiguous": ambi,
                       "no_match": nomatch}
                id_rows.append(row)
                md.append(f"| {'奖励' if layer == 1 else '金币'} | {row['label']} | "
                          f"{tot} | {eq} | {uniq} | {ambi} | {nomatch} |")
    rep["identity"] = id_rows
    md.append("")

    # ---- E. 几何分量对账 ----
    md.append("## E. 几何分量对账（连续两拍均选择同一身份，双拍 seen、同源且无已记录参考系切换）\n")
    md.append("恒等式：`scene.x = carsrc.xr − origin − cd + fshift`。"
              "分解为 Δxr(车源横位，含估计平移和滤波) / Δcd(路心补偿) / Δorigin / Δfshift 四项，"
              "残差=闭合误差。**本表不出「可疑」判定**——阈值未标定。\n")
    md.append("| 段 | 干净相邻对 | 其中场景超1m | 闭合≤2cm | 残差 max | 主导=cd | 主导=xr |"
              " 主导=origin | 仅xr超1m | 仅cd超1m | 两者 | 都未超 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    geom_rows = []
    for seg in segs:
        row = M.geometry_stat(seg)
        pairs, n, clos = row["pairs"], row["events"], row["closed"]
        dom, grp = row["dominant"], row["groups"]
        geom_rows.append(row)
        md.append(f"| {row['label']} | {pairs} | {n} | {clos} | {row['err_max']} | "
                  f"{dom.get('cd', 0)} | {dom.get('xr', 0)} | {dom.get('origin', 0)} | "
                  f"{grp.get('xr_only', 0)} | {grp.get('cd_only', 0)} | "
                  f"{grp.get('both', 0)} | {grp.get('neither', 0)} |")
    rep["geometry"] = geom_rows
    md.append("")
    md.append("> 成因与影响未定：1.05 m 只是**单个采样点**的限拍步长，"
              "而目标补偿是两点插值之差，逐拍上限不是 1.05 m；"
              "纵深变化能否解释需局部斜率，本表未做归因。\n")

    md.append("分母仅覆盖连续入选且场景可见的目标对，不代表全部车对象。事件前后帧号、时间和有符号分量保存在同源 JSON 的 event_details。xr 超阈总数必须包含 both；主导项是幅度排序，不能当作根因归属。\n")

    # ---- F. 请求覆盖（按拍 / 按 episode）----
    md.append("## F. 请求覆盖：本道无机会、旁道有机会\n")
    md.append("两个分母都报：按拍（每拍独立）、按 episode（连续满足条件的拍合成一段）。\n")
    md.append("| 段 | 按拍：场景拍 | 其中 req 空 | 比例 | 按 episode：段数 | 全程无请求 | 比例 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    req_rows = []
    for seg in segs:
        row = {"label": M.seg_label(seg), **M.request_stat(seg)}
        req_rows.append(row)
        md.append(f"| {row['label']} | {row['ticks']} | {row['ticks_no_req']} | {pct(row['tick_ratio'])} | "
                  f"{row['episodes']} | {row['episodes_no_req']} | {pct(row['episode_ratio'])} |")
    rep["request_coverage"] = req_rows
    md.append("")

    # ---- G. 杆值：同一左端保持算法 ----
    md.append("## G. 杆值统计（程序实际驾驶与人工真实输入分开）\n")
    md.append("死区见参数表。左端采样保持到下一采样；缺读或超出分段间隔时停止累计；末拍不外推。首尾截断按窗内时长计并单列边界数。左右换向但未进死区仍为同一持杆段。\n")
    md.append("| 来源 | 段 | 样本 | 零杆样本占比 | 零杆时间占比 | 持杆中位 ms | 持杆均值 ms | 实测 Hz | 截断边界 |")
    md.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    stick_rows = []
    for seg in segs:
        row = {"source": "program(trace)" if seg[0].get("round") == 1 else "counterfactual(trace)",
               "label": M.seg_label(seg), "nominal_hz": 20,
               **M.stick_stat([(r["ts_ns"], M.num(r.get("steer_x"))) for r in seg])}
        stick_rows.append(row)
    if args.pads_dir:
        for name in sorted(os.listdir(args.pads_dir)):
            pad = os.path.join(args.pads_dir, name, "pads.jsonl")
            meta_path = os.path.join(args.pads_dir, name, "meta.json")
            if not os.path.isfile(pad) or not os.path.isfile(meta_path):
                continue
            with open(meta_path, encoding="utf-8") as mf:
                meta = json.load(mf)
            if meta.get("session") != args.session_id or meta.get("source") != "manual":
                continue
            raw = open(pad, "rb").read()
            samples = [json.loads(line) for line in raw.splitlines() if line.strip()]
            rep["manual_inputs"].append({"name": name, "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
                                         "meta_sha256_16": hashlib.sha256(open(meta_path, "rb").read()).hexdigest()[:16]})
            row = {"source": f"human({name})", "label": name,
                   "nominal_hz": meta.get("pad_poll_hz"),
                   **M.stick_stat([(x["ts_ns"], M.num(x.get("lx"))) for x in samples])}
            stick_rows.append(row)
        if not rep["manual_inputs"]:
            ap.error("指定目录没有与本会话匹配的人工手柄记录")
    for row in stick_rows:
        src = ("程序实际驾驶" if row["source"] == "program(trace)" else
               "反事实命令" if row["source"] == "counterfactual(trace)" else "人工真实输入")
        md.append(f"| {src} | {row['label']} | {row['n']} | {pct(row['zero'])} | "
                  f"{pct(row['zero_time'])} | {row['hold_med_ms']} | {row['hold_mean_ms']} | "
                  f"{row['effective_hz']} | {row['censored_boundaries']} |")
    md.append("\n名义频率与实测频率、有效观测时长及缺口数均保存在 JSON。样本占比受采样影响，跨来源占空比优先查看时间占比。手柄记录覆盖自己的录制窗口，未裁切成与 trace 完全重合的区间。\n")
    md.append("手柄输入指纹：`" + json.dumps(rep["manual_inputs"], ensure_ascii=False) + "`\n")
    rep["sticks"] = stick_rows
    md.append("")

    # ---- H. 侧别锁定归因 ----
    md.append("## H. 侧别锁定归因（`sel_dbg.pin`）\n")
    md.append("贴边拍=why 为 pass/graze；同车大跳=命中 id 与保持 id 都相同、"
              "且目标位移超过 `target_jump_lane`。旧 trace 没有该列时"
              "「有 pin 列」为 0、分布为空——如实报缺列，不猜。只报分布，不判对错。\n")
    md.append("| 段 | 贴边拍 | 有 pin 列 | held | same | 让位 | 让位明细 | 同车大跳 | 跳时让位 | 侧别违背 | 跳时违背 |")
    md.append("|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|")
    pin_rows = []
    for seg in segs:
        row = {"label": M.seg_label(seg), **M.pin_stat(seg)}
        notes = row["notes"]
        y = {k: v for k, v in notes.items() if k in M.PIN_YIELD}
        jy = {k: v for k, v in row["jump_notes"].items() if k in M.PIN_YIELD}
        row["yield"] = sum(y.values())
        row["yield_notes"] = y
        row["jump_yield"] = sum(jy.values())
        pin_rows.append(row)
        detail = ",".join(f"{k}:{v}" for k, v in sorted(y.items())) or "-"
        md.append(f"| {row['label']} | {row['ticks']} | {row['with_pin']} | "
                  f"{notes.get('held', 0)} | {notes.get('same', 0)} | "
                  f"{row['yield']} | {detail} | {row['jumps']} | {row['jump_yield']} | "
                  f"{row['side_break']}/{row['side_ticks']} | {row['jump_side_break']} |")
    rep["pin"] = pin_rows
    md.append("")

    # ---- I. 未定项 ----
    md.append("## I. 未定项（不出数）\n")
    for line in [
        "**证据有效**：旧无身份分支用 `not scene.stale` 测走廊状态，车类分支复用资格标记；混合通过率作废；"
        "新口径需绑定对象种类 × 证据来源 × 用途，并允许「无法判定」。",
        "**动作可行**：原实现只测到站时间存在且为正，且含恒真条件；"
        "改名「时间信息可用」并四分类，不含可达性。",
        "**几何阈值**：本轮只做分量对账，异常判定阈值待标定；不以 1 m 或 0.04 道作为硬门。",
        "**路心补偿成因**：闭合误差见 E 表，成因（道路剖面 / 查询纵深 / "
        "采样点限拍与超时直通）与下游影响未定。",
    ]:
        md.append(f"- {line}")
    md.append("")

    if expected is not None:
        keys = ("caliber_version", "input", "params", "manual_inputs", "implementation_sha256_16", "mode_assignment")
        if any(expected.get(k) != rep.get(k) for k in keys):
            raise SystemExit("不可直接比较：口径、实现或输入指纹不同")
        if expected != rep:
            changed = [k for k in rep if expected.get(k) != rep[k]]
            raise SystemExit("同口径复算不一致：" + ", ".join(changed))
        print("同口径、同输入复算一致；未覆盖报告")
        return
    if args.emit_json:
        print(json.dumps(rep, ensure_ascii=False))
        return
    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, "RECOMPUTE_REPORT")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
