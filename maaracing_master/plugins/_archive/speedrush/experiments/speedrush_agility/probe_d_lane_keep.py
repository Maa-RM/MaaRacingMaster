"""P3-A D 线：实录里「松杆后的车道保持辅助」是否按描述工作。

**唯一问题**：在「零杆 ≥0.6 s、名义车速 ≥50 km/h、无撞车」的窗口里，
车头相对车道的角度（``lat_ctrl.psi``）是否向车道方向收敛？横位是保持、
漂移还是回到车道中心？人工局与程序局是否不同？

**口径**：

- 杆：程序局取 trace 的 ``steer_x``（MaaFramework 归一杆的原始 16 位值，
  ``/32767``；==0 才算松杆）；人工局取录制手柄 ``pads.jsonl`` 的 ``lx``
  （16 位，约 180~200 Hz；任一样本非零即整拍不算松杆）。
  人工局 trace 里的 ``steer_x`` 是影子决策的反事实杆，不是物理杆，不用。
- 窗口：同一采样口径下连续松杆 ≥0.6 s（辅助在 0.5 s 接管，留 0.1 s 观察）；
  速度门用 ``v_ego``，但该列读数恒为配置参考速（本机三局实测均为 41.0 m/s），
  不能证明真实车速，只证明「按名义车速门槛一直满足」。
- 辅助段：``[t0+0.5, t1]``；``psi`` 名义上是相对车道的车头角（rad，右正），
  实际是「车道前瞻方向 + 横速反解」的融合估计，换线/弯道过渡后会被观测器
  自身拉回 0，不能单独当物理车头角；``psi_age`` ≤300 ms 才算本拍可信。车头角变化 ``dpsi``、横位变化取两路：
  planner 信念横位 ``executed_lane``，以及车道线中点横位（相邻两线 L 的中点，
  单位道，自车 0）——后者与 ``executed_lane`` 有常值偏置，但窗口内的**变化**
  与「离车道中心变远/变近」不受偏置影响。
- 「回中心」判据：|车道线中点横位| 在辅助段内变小 ≥0.05 道；「保持」判据：
  变化 ≤0.05 道（约 18 cm）。
- 撞车：窗口内任一拍 ``collisions`` 非空即剔除。漂移无 trace 直接读数，
  只能靠 `collisions` 与大幅横速侧证，列为缺口。

用法::

    .venv/Scripts/python.exe -X utf8 probe_d_lane_keep.py
    .venv/Scripts/python.exe -X utf8 probe_d_lane_keep.py --csv _out/d_lane_keep.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import statistics
from pathlib import Path

FULL = 32767.0
ZERO_RUN_S = 0.6
ASSIST_DELAY_S = 0.5
PSI_AGE_MAX_MS = 300.0
PSI_STEP_MAX_RAD = 0.04      # 单拍 0.04 rad ≈ 34°/s，超过即观测重锁而非物理
HOLD_BAND_LANE = 0.05
STRAIGHT_LIMIT_RAD = 0.0175    # 1°

SESSIONS = {
    "20261119_104138": "program",
    "20261118_195555": "program",
    "20261118_225251": "manual",
}

APP = Path(os.environ["APPDATA"]) / "MaaRacingMaster"
LOGS = APP / "logs"
DEBUG = APP / "debug" / "speedrush"


def load_ticks(path: Path) -> list[dict]:
    ticks = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("fid") is not None:
                ticks.append(rec)
    ticks.sort(key=lambda r: r["ts_mono_ns"])
    return ticks


def stages(ticks: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for r in ticks:
        out.setdefault(int(r.get("stage_id") or 0), []).append(r)
    return out


def manual_pads(session: str) -> dict[int, list[tuple[int, float]]]:
    """phase -> [(ts_ns, lx_raw)]，与 trace 的 ts_mono_ns 同一单调钟。"""
    out: dict[int, list[tuple[int, float]]] = {}
    for meta_path in sorted(DEBUG.glob("*_manual_p*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("session") != session:
            continue
        pads_path = meta_path.parent / "pads.jsonl"
        if not pads_path.exists():
            continue
        rows = []
        for line in open(pads_path, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            lx = rec.get("lx")
            if lx is None:
                continue
            rows.append((int(rec["ts_ns"]), float(lx)))
        rows.sort()
        out[int(meta.get("phase") or 0)] = rows
    return out


def stick_of_manual(pads: list[tuple[int, float]], t0: int, t1: int
                    ) -> tuple[bool, bool]:
    """返回 (是否全零, 是否有采样)；窗口内任一样本非零即视为未松杆。"""
    seen = False
    for ts, lx in pads:
        if ts < t0:
            continue
        if ts >= t1:
            break
        seen = True
        if lx != 0.0:
            return False, True
    return True, seen


def lane_center_offsets(lo: dict | None) -> list[float]:
    """车道线中点横位（道，右正；自车 0）。

    只取被接受、方向同侧、L 间距在 0.5~1.5 道的相邻线对；返回每条可见
    车道中心相对自车的横位。符号：L 为线相对自车的横位（右正），所以
    中线 = (L1+L2)/2，自车相对中心 = -中线。
    """
    if not lo:
        return []
    lines = [c for c in (lo.get("lines") or [])
             if c.get("accepted") and c.get("L") is not None]
    lines.sort(key=lambda c: float(c["L"]))
    out = []
    for a, b in zip(lines, lines[1:]):
        dl = float(b["L"]) - float(a["L"])
        if 0.5 <= abs(dl) <= 1.5:
            out.append(-0.5 * (float(a["L"]) + float(b["L"])))
    return out


def offset_track(run: list[dict]) -> list[float | None]:
    """按拍跟踪同一条车道中心，防止「最近车道中心」在中途换到邻道。

    首拍取离自车最近的候选；之后只接受与上一拍相差 ≤0.25 道（约 5 道/秒，
    远高于辅助可产生的横移）的候选，否则本拍置 None 并保持上次身份。
    """
    out: list[float | None] = []
    prev: float | None = None
    for r in run:
        offs = lane_center_offsets(r.get("lane_obs"))
        pick = None
        if offs:
            if prev is None:
                pick = min(offs, key=abs)
            else:
                cand = [o for o in offs if abs(o - prev) <= 0.25]
                if cand:
                    pick = min(cand, key=lambda o: abs(o - prev))
        out.append(pick)
        if pick is not None:
            prev = pick
    return out


def psi_at(ticks: list[dict], t_ns: int) -> dict | None:
    cand = None
    for r in ticks:
        if r["ts_mono_ns"] <= t_ns:
            cand = r
        else:
            break
    return cand


def windows(ticks: list[dict], kind: str,
            pads: list[tuple[int, float]] | None) -> list[list[dict]]:
    """按采样拍切连续零杆段；返回每段的拍列表。"""
    runs: list[list[dict]] = []
    cur: list[dict] = []
    for i, r in enumerate(ticks):
        if kind == "program":
            zero = (r.get("steer_x") == 0)
        else:
            nxt = (ticks[i + 1]["ts_mono_ns"] if i + 1 < len(ticks)
                   else r["ts_mono_ns"] + int(float(r.get("dt") or 0.05) * 1e9))
            allzero, seen = stick_of_manual(pads or [], r["ts_mono_ns"], nxt)
            zero = bool(allzero and seen)
        if zero:
            cur.append(r)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def run_duration(run: list[dict]) -> float:
    t0 = run[0]["ts_mono_ns"]
    t1 = run[-1]["ts_mono_ns"] + int(float(run[-1].get("dt") or 0.05) * 1e9)
    return (t1 - t0) / 1e9


def analyse(session: str, kind: str, stage: int, ticks: list[dict],
            run: list[dict], rejects: dict[str, int]) -> dict | None:
    dur = run_duration(run)
    if dur < ZERO_RUN_S:
        rejects["零杆不足0.6s"] += 1
        return None
    if any(r.get("collisions") for r in run):
        rejects["窗口内有撞车"] += 1
        return None
    t0 = run[0]["ts_mono_ns"]
    t1 = run[-1]["ts_mono_ns"] + int(float(run[-1].get("dt") or 0.05) * 1e9)
    onset_ns = t0 + int(ASSIST_DELAY_S * 1e9)
    a = None
    for r in run:
        if r["ts_mono_ns"] >= onset_ns:
            a = r
            break
    if a is None:
        rejects["辅助段为空"] += 1
        return None
    b = run[-1]
    la, lb = a["lat_ctrl"], b["lat_ctrl"]
    if la.get("psi") is None or lb.get("psi") is None:
        rejects["辅助段缺 psi"] += 1
        return None
    for ctl in (la, lb):
        age = ctl.get("psi_age")
        if age is None or float(age) > PSI_AGE_MAX_MS:
            rejects["psi 龄超门（无线角证据）"] += 1
            return None
    psi0, psi1 = float(la["psi"]), float(lb["psi"])
    ex0, ex1 = float(a["executed_lane"]), float(b["executed_lane"])
    offs = offset_track(run)
    ia = next(i for i, r in enumerate(run) if r is a)
    off0, off1 = offs[ia], offs[-1]
    seg = [r for r in run if r["ts_mono_ns"] >= onset_ns]
    pre = [r for r in run if r["ts_mono_ns"] < onset_ns]
    vlat = [float(r["v_lat"]) for r in seg if r.get("v_lat") is not None]
    psis = [float(r["lat_ctrl"]["psi"]) for r in seg
            if r["lat_ctrl"].get("psi") is not None]
    assist_s = (t1 - onset_ns) / 1e9
    if assist_s <= 0.05:
        rejects["辅助段过短"] += 1
        return None
    # 松杆后到辅助接管前 0.5 s 的朝向变化率（对照段）
    pre_rate = None
    if len(pre) >= 2 and pre[0]["lat_ctrl"].get("psi") is not None \
            and pre[-1]["lat_ctrl"].get("psi") is not None:
        pre_s = (pre[-1]["ts_mono_ns"] - pre[0]["ts_mono_ns"]) / 1e9
        if pre_s > 0.1:
            pre_rate = (float(pre[-1]["lat_ctrl"]["psi"])
                        - float(pre[0]["lat_ctrl"]["psi"])) / pre_s
    # 物理横速口径：v_lat（道/秒）与车道线中点横位的差分斜率。
    # 二者与 psi 不同，直接来自线在自车系里的横向移动，不含「车头角前瞻」混淆。
    def _vlat(r):
        v = r.get("v_lat")
        return None if v is None else float(v)

    def _slope(seq):
        pts = [(r["ts_mono_ns"] / 1e9, o) for r, o in seq if o is not None]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 0.15:
            return None
        mt = sum(t for t, _ in pts) / len(pts)
        mo = sum(o for _, o in pts) / len(pts)
        vt = sum((t - mt) ** 2 for t, _ in pts)
        if vt <= 1e-9:
            return None
        return sum((t - mt) * (o - mo) for t, o in pts) / vt

    vlat_a, vlat_b = _vlat(a), _vlat(b)
    seg_off = [(r, offs[i]) for i, r in enumerate(run)
               if r["ts_mono_ns"] >= onset_ns]
    pre_off = [(r, offs[i]) for i, r in enumerate(run)
               if r["ts_mono_ns"] < onset_ns]
    rate_off = _slope(seg_off)
    rate_off_pre = _slope(pre_off)
    # 观测洁净度：窗口内 psi 单拍跳变（重锁/换线）与角度断供拍
    steps = []
    ages = []
    prev_psi = None
    for r in run:
        p_ = r["lat_ctrl"].get("psi")
        a_ = r["lat_ctrl"].get("psi_age")
        if p_ is None or (a_ is not None and float(a_) > PSI_AGE_MAX_MS):
            ages.append(1)
        if p_ is not None and prev_psi is not None:
            steps.append(abs(float(p_) - prev_psi))
        prev_psi = None if p_ is None else float(p_)
    psi_step = max(steps) if steps else None
    clean = (psi_step is not None and psi_step <= PSI_STEP_MAX_RAD
             and not ages)
    # psi 积分预测的横移（道）：x' = v/LANE_W·tan(psi)，名义车速 41 m/s
    dx_pred = 0.0
    prev = None
    for r in run:
        p = r["lat_ctrl"].get("psi")
        if p is None:
            prev = None
            continue
        if prev is not None and r["ts_mono_ns"] >= onset_ns:
            dts = (r["ts_mono_ns"] - prev[0]) / 1e9
            dx_pred += (41.0 / 3.5) * 0.5 * (math.tan(prev[1])
                                             + math.tan(float(p))) * dts
        prev = (r["ts_mono_ns"], float(p))
    return {
        "session": session,
        "kind": kind,
        "stage": stage,
        "t0_s": round((t0 - ticks[0]["ts_mono_ns"]) / 1e9, 2),
        "dur_s": round(dur, 2),
        "assist_s": round(assist_s, 2),
        "psi0": round(psi0, 4),
        "psi1": round(psi1, 4),
        "dpsi": round(psi1 - psi0, 4),
        "psi_shrink": round(abs(psi1) - abs(psi0), 4),
        "psi_pre_rate": None if pre_rate is None else round(pre_rate, 4),
        "psi_min_abs": round(min(abs(x) for x in psis), 4) if psis else None,
        "psi_max_abs": round(max(abs(x) for x in psis), 4) if psis else None,
        "psi_rate_deg_s": round(abs(psi1 - psi0) / assist_s * 57.2958, 2),
        "vlat0": None if vlat_a is None else round(vlat_a, 3),
        "vlat1": None if vlat_b is None else round(vlat_b, 3),
        "rate_off": None if rate_off is None else round(rate_off, 3),
        "rate_off_pre": None if rate_off_pre is None else round(rate_off_pre, 3),
        "psi_step": None if psi_step is None else round(psi_step, 4),
        "clean": bool(clean),
        "theta0": None if not a.get("lane_obs") else a["lane_obs"].get("theta"),
        "thu": bool(la.get("thu")),
        "ex0": round(ex0, 3),
        "ex1": round(ex1, 3),
        "dex": round(ex1 - ex0, 3),
        "dx_pred": round(dx_pred, 3),
        "off0": None if off0 is None else round(off0, 3),
        "off1": None if off1 is None else round(off1, 3),
        "doff": None if off0 is None or off1 is None
        else round(off1 - off0, 3),
        "vlat_med": round(statistics.median(vlat), 3) if vlat else None,
        "v_ego": a.get("v_ego"),
        "n": len(run),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append")
    ap.add_argument("--csv")
    args = ap.parse_args()
    rows: list[dict] = []
    rejects: dict[str, int] = collections.defaultdict(int)
    for session, kind in SESSIONS.items():
        if args.session and session not in args.session \
                and not any(session.endswith(s) for s in args.session):
            continue
        path = LOGS / session / "trace" / "speedrush.jsonl"
        if not path.exists():
            print(f"缺 trace: {session}")
            continue
        ticks = load_ticks(path)
        pads = manual_pads(session) if kind == "manual" else {}
        for stage, st_ticks in sorted(stages(ticks).items()):
            runs = windows(st_ticks, kind, pads.get(stage))
            n_all = len(runs)
            for run in runs:
                row = analyse(session, kind, stage, st_ticks, run, rejects)
                if row is not None:
                    rows.append(row)
            print(f"{session} s{stage} {kind}: 零杆段 {n_all}，"
                  f"入窗 {sum(1 for r in rows if r['session'] == session and r['stage'] == stage)}")
    if rejects:
        print("剔除原因：", dict(rejects))
    if not rows:
        print("无入窗样本")
        return
    cols = list(rows[0].keys())
    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print(f"CSV: {out}")
    print("\n逐窗：")
    print("kind stage   t0   dur assist  psi0    psi1   dpsi  shrink "
          "pre°/s rate°/s  ex0   ex1   dex dxPred  off0  off1  doff  vlat cln")
    print("  物理口径：vlat0/vlat1=辅助段起止横速（道/秒），"
          "rateOff=车道线中点横位在辅助段的斜率（道/秒），rateOffPre=松杆后前 0.5s")
    for r in rows:
        off = lambda v: "  -  " if v is None else f"{v:+.3f}"
        pre = "  -  " if r["psi_pre_rate"] is None else f"{r['psi_pre_rate']*57.2958:+5.1f}"
        print(f"{r['kind'][:3]:>4} {r['stage']} {r['t0_s']:7.2f} "
              f"{r['dur_s']:5.2f} {r['assist_s']:5.2f} "
              f"{r['psi0']:+.4f} {r['psi1']:+.4f} {r['dpsi']:+.4f} "
              f"{r['psi_shrink']:+.4f} {pre} {r['psi_rate_deg_s']:6.2f} "
              f"{r['ex0']:+.2f} {r['ex1']:+.2f} {r['dex']:+.3f} "
              f"{r['dx_pred']:+.3f} {off(r['off0'])} {off(r['off1'])} "
              f"{off(r['doff'])} {r['vlat_med']:+.3f} "
              f"{'Y' if r['clean'] else 'n'}")
        vl = lambda v: "  -  " if v is None else f"{v:+.3f}"
        print(f"      vlat0 {vl(r['vlat0'])} vlat1 {vl(r['vlat1'])} "
              f"rateOff {vl(r['rate_off'])} rateOffPre {vl(r['rate_off_pre'])}")
    print("\n分组合计：")
    for kind in ("program", "manual"):
        g = [r for r in rows if r["kind"] == kind]
        if not g:
            continue
        print(f" [{kind}] 全部 {len(g)} 窗；洁净 {sum(1 for r in g if r['clean'])}")
        g = [r for r in g if r["clean"]]
        if not g:
            continue
        big = [r for r in g if abs(r["psi0"]) >= 0.005]
        shrink = [r for r in big if r["psi_shrink"] < -0.002]
        grow = [r for r in big if r["psi_shrink"] > 0.002]
        flips = [r for r in g if r["psi0"] * r["psi1"] < 0]
        with_off = [r for r in g if r["doff"] is not None]
        hold = [r for r in with_off if abs(r["doff"]) <= HOLD_BAND_LANE]
        to_center = [r for r in with_off
                     if abs(r["off1"]) < abs(r["off0"]) - HOLD_BAND_LANE]
        away = [r for r in with_off
                if abs(r["off1"]) > abs(r["off0"]) + HOLD_BAND_LANE]
        print(f" {kind}: 窗 {len(g)}，总零杆 {sum(r['dur_s'] for r in g):.1f}s，"
              f"辅助段 {sum(r['assist_s'] for r in g):.1f}s")
        print(f"   |psi0|≥0.29°: {len(big)}，|psi| 减 {len(shrink)}、增 {len(grow)}、"
              f"符号翻转 {len(flips)}")
        print(f"   有横位读数 {len(with_off)}：保持(≤0.05道) {len(hold)}、"
              f"离中心变近 {len(to_center)}、变远 {len(away)}")
        print(f"   |dpsi| 中位 {statistics.median(abs(r['dpsi']) for r in g):.4f} rad，"
              f"|dex| 中位 {statistics.median(abs(r['dex']) for r in g):.3f} 道，"
              f"辅助时长中位 {statistics.median(r['assist_s'] for r in g):.2f}s")
        settle = [r for r in big if abs(r["psi1"]) <= STRAIGHT_LIMIT_RAD]
        print(f"   起手 |psi0|>0.29° 的窗收尾落到 ≤1°：{len(settle)}/{len(big)}")
        far = [r for r in with_off if abs(r["off0"]) >= 0.15]
        if far:
            print(f"   起手离车道中心 ≥0.15 道的窗 {len(far)}："
                  f"|off0| 中位 {statistics.median(abs(r['off0']) for r in far):.3f}，"
                  f"|off1| 中位 {statistics.median(abs(r['off1']) for r in far):.3f}，"
                  f"doff 中位 {statistics.median(r['doff'] for r in far):+.3f} 道")
        pre_ok = [r for r in g if r["psi_pre_rate"] is not None]
        if pre_ok:
            conv = [r for r in pre_ok
                    if abs(r["dpsi"] / max(r["assist_s"], 1e-6))
                    < abs(r["psi_pre_rate"])]
            print(f"   辅助段朝向速率小于松杆后前 0.5s：{len(conv)}/{len(pre_ok)}")


if __name__ == "__main__":
    main()
