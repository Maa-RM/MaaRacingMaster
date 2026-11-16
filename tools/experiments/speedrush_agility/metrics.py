"""speedrush 验证层：指标定义的唯一实现。

本模块只做三件事：切段、取序列、按口径算量。所有阈值集中在 ``PARAMS``，
规格文档只引用参数名，复算报告打印参数值——三处不再各存一份。

口径版本随定义变化递增；报告必须记录版本、输入文件与参数。
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from collections import Counter

CALIBER_VERSION = "sr-base-v2"

#: 全部阈值/口径参数。改这里就是改口径，必须同时升版本号。
PARAMS: dict[str, object] = {
    "lane_w_m": 3.5,               # 1 道 = 3.5 m（实测 lane_w 四段恒 3.5）
    "seg_gap_s": 0.2,              # 相邻拍间隔超过此值即断段
    "rev_theta_sweep": (0.02, 0.05, 0.10, 0.20),  # 滞回阈值（道），全部报，不选一个
    "near_lane": 0.8,              # 「本道机会」判定：|x − ego| < 此值（道）
    "toa_min_s": 1.0,              # 机会到站时间窗口下界
    "toa_max_s": 3.0,              # 上界
    "stick_deadzone_raw": 2000,    # 杆值死区（int16 绝对值，程序/人工同一标尺）
    "geom_require_adjacent": True,  # 几何只比严格相邻拍
    "geom_require_seen_both": True,  # 几何要求两拍都有真实观测
    "geom_require_same_src": True,   # 几何要求两拍同源
    "geometry_event_m": 1.0,       # 仅筛查分量，不是物理异常判据
    "closure_tolerance_m": 0.02,
    "identity_candidate_lane": 0.8,  # 候选邻近筛选，不证明身份
    "run_summary_theta": 0.10,
}


def load_trace(path: str) -> tuple[list[dict], str]:
    """读决策拍并返回 (rows, 文件指纹)。只取含 carsrc 的行。"""
    h = hashlib.sha256()
    rows: list[dict] = []
    with open(path, "rb") as f:
        for raw in f:
            h.update(raw)
            if b'"carsrc"' not in raw:
                continue
            rows.append(json.loads(raw.decode("utf-8")))
    rows.sort(key=lambda r: (r.get("round") or 0, r.get("stage_id") or 0,
                             r.get("seq") or 0))
    return rows, h.hexdigest()[:16]


def split_segments(rows: list[dict], gap_s: float | None = None) -> list[list[dict]]:
    """按 round+stage 切段；段内相邻拍间隔超过 ``seg_gap_s`` 也断段。

    断段之后不再跨段比较——这是所有指标的公共前提。
    """
    gap_ns = (PARAMS["seg_gap_s"] if gap_s is None else gap_s) * 1e9
    segs: list[list[dict]] = []
    for r in rows:
        if (not segs
                or r.get("round") != segs[-1][-1].get("round")
                or r.get("stage_id") != segs[-1][-1].get("stage_id")
                or (r.get("ts_ns") or 0) - (segs[-1][-1].get("ts_ns") or 0) > gap_ns):
            segs.append([])
        segs[-1].append(r)
    return segs


def seg_label(seg: list[dict]) -> str:
    return f"r{seg[0].get('round')}s{seg[0].get('stage_id')}"


def num(x) -> float | None:
    return (float(x) if isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) else None)


def runs_of(seg: list[dict], getter) -> list[list[tuple[int, float]]]:
    """取出连续非 None 的 (拍序号, 值) 段；None 处断段，不插值、不平移。"""
    out: list[list[tuple[int, float]]] = []
    cur: list[tuple[int, float]] = []
    for i, r in enumerate(seg):
        if i and reference_break(seg[i - 1], r) and cur:
            out.append(cur)
            cur = []
        v = getter(r)
        if v is None:
            if cur:
                out.append(cur)
                cur = []
            continue
        cur.append((i, float(v)))
    if cur:
        out.append(cur)
    return out


def reference_break(previous: dict, current: dict) -> bool:
    """记录到的执行重基或路线参考系纪元变化；未知纪元不猜测。"""
    before = (previous.get("route") or {}).get("ep")
    after = (current.get("route") or {}).get("ep")
    return bool((current.get("lat_ctrl") or {}).get("rebase")) or (
        before is not None and after is not None and before != after)


def dt_seconds(seg: list[dict], i: int) -> float | None:
    """第 i 拍到 i+1 拍的实际时长（秒），来自 ts_ns。"""
    if i + 1 >= len(seg):
        return None
    a, b = seg[i].get("ts_ns"), seg[i + 1].get("ts_ns")
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    d = (b - a) / 1e9
    return d if d > 0 else None


def reversals(values: list[float], theta: float) -> tuple[int, list[int]]:
    """滞回反转计数：只有反向累计位移达到 theta 才确认一次反转。

    与「逐拍差分死区」不同：小幅抖动在累计位移不足 theta 时不计方向，
    仍受采样频率、噪声和缺口影响。返回 (反转次数, 确认点之间的段长度/拍)，
    首尾段按观测窗截断，不解释为独立的完整运动周期。
    """
    if len(values) < 2:
        return 0, [len(values)]
    rev_idx: list[int] = []
    direction = 0
    hi = lo = values[0]
    for i in range(1, len(values)):
        v = values[i]
        if direction >= 0:
            hi = max(hi, v)
        if direction <= 0:
            lo = min(lo, v)
        if direction == 0:
            if v - lo >= theta:
                direction, hi = 1, v
            elif hi - v >= theta:
                direction, lo = -1, v
        elif direction == 1:
            if hi - v >= theta:
                rev_idx.append(i)
                direction, lo = -1, v
        else:
            if v - lo >= theta:
                rev_idx.append(i)
                direction, hi = 1, v
    bounds = [-1] + rev_idx + [len(values) - 1]
    runs = [bounds[k + 1] - bounds[k] for k in range(len(bounds) - 1)]
    return len(rev_idx), runs


@dataclass
class RevStat:
    """一条序列的反转统计。分母是「有效观测覆盖的真实时长」。"""
    theta: float
    count: int = 0
    span_s: float = 0.0
    runs: list[int] = field(default_factory=list)
    breaks: int = 0        # None 造成的断段数
    observed: int = 0      # 非 None 拍数

    @property
    def per_s(self) -> float | None:
        return (self.count / self.span_s) if self.span_s > 0 else None

    @property
    def median_run_ticks(self) -> float | None:
        if not self.runs:
            return None
        return float(statistics.median(self.runs))

    def as_dict(self) -> dict:
        return {"theta_lane": self.theta, "count": self.count,
                "span_s": round(self.span_s, 2),
                "per_s": None if self.per_s is None else round(self.per_s, 3),
                "median_run_ticks": self.median_run_ticks,
                "breaks": self.breaks, "observed": self.observed}


def rev_stat(seg: list[dict], getter, theta: float) -> RevStat:
    """按段内连续非 None 序列统计反转率；None 断段且断段时长不进分母。"""
    st = RevStat(theta=theta)
    for run in runs_of(seg, getter):
        st.observed += len(run)
        vals = [v for _, v in run]
        n, runs = reversals(vals, theta)
        st.count += n
        st.runs.extend(runs)
        # 时长：段内相邻（拍序号连续）才累加，跳拍处断开
        for (i0, _), (i1, _) in zip(run, run[1:]):
            if i1 == i0 + 1:
                d = dt_seconds(seg, i0)
                if d:
                    st.span_s += d
    st.breaks = max(0, len(runs_of(seg, getter)) - 1)
    return st


def scene_map(r: dict) -> dict:
    """只取车对象，避免金币组号与跟踪身份的数值重叠。"""
    out: dict = {}
    for o in (r.get("scene") or {}).get("objs") or []:
        if (isinstance(o, (list, tuple)) and len(o) >= 16
                and o[0] == "car" and o[14] is not None):
            out.setdefault(o[14], o)
    return out


def otab_map(r: dict) -> dict:
    """本拍对象表：id → 14 元组。"""
    out: dict = {}
    for e in (r.get("otab") or {}).get("t") or []:
        if (isinstance(e, (list, tuple)) and len(e) >= 14
                and e[0] == "car" and e[1] is not None):
            out.setdefault(e[1], e)
    return out


def carsrc_map(r: dict) -> dict:
    """本拍车源：id → {xr, cd, origin, x}（米，选择坐标系）。"""
    out: dict = {}
    for c in r.get("carsrc") or []:
        cid = c.get("id")
        if cid is None:
            continue
        out.setdefault(cid, {"xr": num(c.get("xr")), "cd": num(c.get("cd")),
                             "origin": num(c.get("origin")), "x": num(c.get("x"))})
    return out


def sel(r: dict) -> dict:
    return r.get("sel_dbg") or {}


def lane_obs_x(r: dict) -> float | None:
    return num((r.get("lane_obs") or {}).get("x"))


def stick_stat(samples: list[tuple[int, float | None]]) -> dict:
    """左端采样保持到下一采样；缺读和大间隔不外推，末拍无额外时长。

    持杆指死区外的连续区间，左右换向但不进死区仍是一段。边界段保留窗内
    时长并单列截断数。零杆样本占比与按时间加权的占比分开报告。
    """
    valid = [(t, num(v)) for t, v in samples]
    values = [v for _, v in valid if v is not None]
    runs = []
    active_s = 0.0
    active = False
    censored = 0
    span = zero_s = 0.0
    intervals = []
    breaks = 0
    for i, (t, v) in enumerate(valid):
        if v is not None and abs(v) < PARAMS["stick_deadzone_raw"] and active:
            runs.append(active_s * 1000)
            active, active_s = False, 0.0
        dt = ((valid[i + 1][0] - t) / 1e9 if i + 1 < len(valid) else None)
        usable = (v is not None and dt is not None
                  and valid[i + 1][1] is not None and 0 < dt <= PARAMS["seg_gap_s"])
        if not usable:
            if active:
                runs.append(active_s * 1000)
                censored += 1
            active, active_s = False, 0.0
            if i + 1 < len(valid):
                breaks += 1
            continue
        intervals.append(dt)
        span += dt
        if abs(v) < PARAMS["stick_deadzone_raw"]:
            zero_s += dt
            if active:
                runs.append(active_s * 1000)
            active, active_s = False, 0.0
        else:
            if not active and (i == 0 or valid[i - 1][1] is None
                               or (t - valid[i - 1][0]) / 1e9 > PARAMS["seg_gap_s"]):
                censored += 1
            active = True
            active_s += dt
    return {"n": len(values),
            "zero": round(sum(abs(v) < PARAMS["stick_deadzone_raw"] for v in values)
                          / len(values), 4) if values else None,
            "zero_time": round(zero_s / span, 4) if span else None,
            "span_s": round(span, 6),
            "effective_hz": round(len(intervals) / span, 2) if span else None,
            "hold_med_ms": round(statistics.median(runs), 1) if runs else None,
            "hold_mean_ms": round(statistics.mean(runs), 1) if runs else None,
            "holds": len(runs), "censored_boundaries": censored, "breaks": breaks}


def request_stat(seg: list[dict]) -> dict:
    """仅使用选择快照的同坐标自车位置；不可观测拍打断 episode。"""
    ticks = empty = unobserved = 0
    episodes = []
    cur = []
    for r in seg:
        d = sel(r)
        ego = num(d.get("ego"))
        own = cross = False
        if ego is None:
            unobserved += 1
        else:
            for key in ("rw", "coin"):
                for item in d.get(key) or []:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    x, t = num(item[0]), num(item[1])
                    if x is None or t is None or not PARAMS["toa_min_s"] <= t <= PARAMS["toa_max_s"]:
                        continue
                    if abs(x - ego) < PARAMS["near_lane"]:
                        own = True
                    else:
                        cross = True
        if cross and not own:
            ticks += 1
            empty += d.get("req") is None
            cur.append(d.get("req"))
        elif cur:
            episodes.append(cur)
            cur = []
    if cur:
        episodes.append(cur)
    no_req = sum(all(v is None for v in e) for e in episodes)
    return {"ticks": ticks, "ticks_no_req": empty,
            "tick_ratio": round(empty / ticks, 4) if ticks else None,
            "episodes": len(episodes), "episodes_no_req": no_req,
            "episode_ratio": round(no_req / len(episodes), 4) if episodes else None,
            "unobserved": unobserved}


def geometry_stat(seg: list[dict]) -> dict:
    """连续入选目标的分量对账，输出计数和逐事件明细，不作物理归因。"""
    P = PARAMS
    last: dict = {}
    pairs = 0
    n = clos = 0
    err_max = 0.0
    dom = Counter()
    grp = Counter()
    events = []
    for i, r in enumerate(seg):
        h = sel(r).get("hit_oid")
        if h is None:
            continue
        o = scene_map(r).get(h)
        c = carsrc_map(r).get(h)
        if o is None or c is None:
            continue
        xs, xr, cd = num(o[1]), c["xr"], c["cd"]
        org = num(sel(r).get("origin"))
        fs = num(sel(r).get("fshift"))
        if None in (xs, xr, cd, org, fs):
            last.pop(h, None)
            continue
        p = last.get(h)
        if p and p[0] is not None:
            adjacent = i - p[5] == 1
            both_seen = bool(p[6]) and bool(o[15])
            same_src = p[7] == o[6]
            ok = ((not P["geom_require_adjacent"] or adjacent)
                  and (not P["geom_require_seen_both"] or both_seen)
                  and (not P["geom_require_same_src"] or same_src)
                  and not reference_break(seg[i - 1], r))
            if ok:
                pairs += 1
                dxr, dcd = xr - p[1], cd - p[2]
                dorg, dfs = org - p[3], fs - p[4]
                err = (xs - p[0]) - (dxr - dorg - dcd + dfs)
                err_max = max(err_max, abs(err))
                if abs(err) <= P["closure_tolerance_m"]:
                    clos += 1
                if abs(xs - p[0]) > P["geometry_event_m"]:
                    n += 1
                    previous = seg[p[5]]
                    events.append({"oid": h, "previous_fid": previous.get("fid"),
                                   "fid": r.get("fid"), "previous_ts_ns": previous["ts_ns"],
                                   "ts_ns": r["ts_ns"], "delta_scene_m": xs - p[0],
                                   "delta_xr_m": dxr, "delta_cd_m": dcd,
                                   "delta_origin_m": dorg, "delta_fshift_m": dfs,
                                   "closure_error_m": err})
                    big = max((abs(dxr), "xr"), (abs(dcd), "cd"),
                              (abs(dorg), "origin"), (abs(dfs), "fshift"))
                    dom[big[1]] += 1
                    a, b = abs(dxr) > P["geometry_event_m"], abs(dcd) > P["geometry_event_m"]
                    grp["both" if a and b else "xr_only" if a
                        else "cd_only" if b else "neither"] += 1
        last[h] = (xs, xr, cd, org, fs, i, o[15], o[6])
    row = {"label": seg_label(seg), "pairs": pairs, "events": n,
           "closed": clos, "err_max": round(err_max, 3),
           "dominant": dict(dom), "groups": dict(grp), "event_details": events}
    return row
