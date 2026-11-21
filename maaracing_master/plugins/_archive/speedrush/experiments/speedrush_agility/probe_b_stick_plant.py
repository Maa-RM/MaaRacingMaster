# -*- coding: utf-8 -*-
"""P3-A B 线：杆→车响应标定与模型等效性（只读探针，不改生产）。

数据来源
--------
- ``logs/<会话>/trace/speedrush.jsonl``：20Hz 控制拍。关键字段：
  ``steer_x`` 实际下发杆（归一后 /32767）、``steer_norm`` 影子输入（低通后、
  限速/死区之前）、``lane_obs.theta`` 线角与它自己的来源时间戳
  ``theta_src_ts_ns``、``plan.pc`` 计划车头角指令、``lat_ctrl.pm/psi``。
- ``debug/speedrush/<录制目录>/pads.jsonl``：人工阶段物理手柄 lx（200Hz，
  16 位原始轴 /32767）。人工阶段的 ``steer_x`` 是影子拍的计算命令
  （``module.py`` 中 gpad=None 不下发），**不是**物理输入。

子命令
------
    stats    各会话覆盖与杆量分布
    fit      车模拟合（yaw 型 / heading 型，留出验证）
    nonlin   分段增益、死区、方向不对称
    release  松杆段的回正（自回正是否存在）
    planform 计划一阶航向模型在实录上的有效时间常数（分工作域）
    shadow   影子输入（steer_norm）与下发值（steer_x）的差及其预测影响
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sys
from dataclasses import dataclass

import numpy as np

LOGS = os.path.join(os.environ["APPDATA"], "MaaRacingMaster", "logs")
DBG = os.path.join(os.environ["APPDATA"], "MaaRacingMaster", "debug", "speedrush")

LANE_W = 3.5
FULL = 32767.0
H = 0.005          # 拟合积分步长（s）


@dataclass
class Session:
    sid: str
    t: np.ndarray           # 控制拍时刻（s，相对首拍）
    steer_x: np.ndarray     # 实际下发杆（归一）
    steer_norm: np.ndarray  # 影子输入（低通后、限速/死区前）
    steer_pre: np.ndarray   # 低通前命令
    theta_t: np.ndarray     # 线角样本时刻（s）
    theta: np.ndarray       # 线角（rad，右正）
    n_locked: np.ndarray
    psi_cmd: np.ndarray     # 计划车头角指令 plan.pc（无效段 NaN）
    psi_ctrl: np.ndarray    # 控制朝向来源（影子优先）
    csrc: list[str]
    x_lane: np.ndarray
    exec_lane: np.ndarray
    pads_t: np.ndarray | None
    pads_u: np.ndarray | None
    valid: np.ndarray        # 线角样本可信（≥2 条线、|横向位|≤1.8 道）
    pv: np.ndarray           # 本拍短预瞄是否接管（lat_ctrl.pv）


def _manual_dirs(sid: str) -> list[str]:
    out = []
    if not os.path.isdir(DBG):
        return out
    for name in os.listdir(DBG):
        mp = os.path.join(DBG, name, "meta.json")
        if not os.path.exists(mp):
            continue
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("session") == sid and meta.get("source") == "manual":
            out.append((meta.get("phase") or 0, os.path.join(DBG, name)))
    return [p for _, p in sorted(out)]


def _load_pads(sid: str):
    ts, us = [], []
    for d in _manual_dirs(sid):
        path = os.path.join(d, "pads.jsonl")
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            ts.append(r["ts_ns"] / 1e9)
            us.append(max(-1.0, min(1.0, r["lx"] / FULL)))
    return (np.array(ts), np.array(us)) if ts else None


def load(sid: str) -> Session:
    path = os.path.join(LOGS, sid, "trace", "speedrush.jsonl")
    t, sx, sn, sp = [], [], [], []
    tt, th, nl, xth = [], [], [], []
    pc, pctl, xl, ex, cs, pv = [], [], [], [], [], []
    t0 = None
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") or "fid" not in d or "steer_x" not in d:
            continue
        ts = d.get("ts_ns")
        if ts is None:
            continue
        if t0 is None:
            t0 = ts
        t.append((ts - t0) / 1e9)
        sx.append(d["steer_x"] / FULL)
        sn.append(d.get("steer_norm") or 0.0)
        sp.append(d.get("steer_pre_lpf") or 0.0)
        lc = d.get("lat_ctrl") or {}
        p = d.get("plan") or {}
        pc.append(p.get("pc") if p.get("valid") else np.nan)
        pctl.append(lc.get("pm") if lc.get("csrc") == "model" else lc.get("psi"))
        cs.append(lc.get("csrc") or "none")
        pv.append(bool(lc.get("pv")))
        lo = d.get("lane_obs") or {}
        xl.append(lo.get("x") if lo.get("x") is not None else np.nan)
        ex.append(d.get("executed_lane") or 0.0)
        if lo.get("theta") is not None and lo.get("theta_src_ts_ns"):
            q = (lo["theta_src_ts_ns"] - t0) / 1e9
            if tt and abs(q - tt[-1]) < 1e-6:
                continue          # 同一帧被多个控制拍重复读取：只留首条
            tt.append(q)
            th.append(float(lo["theta"]))
            nl.append(int(lo.get("n_locked") or 0))
            xth.append(lo.get("x") if lo.get("x") is not None else np.nan)
    pads = _load_pads(sid)
    pad_t = pad_u = None
    if pads is not None:
        pad_t = pads[0] - t0 / 1e9
        pad_u = pads[1]
    nl_a = np.array(nl)
    xth_a = np.array(xth)
    valid = (nl_a >= 2) & np.isfinite(xth_a) & (np.abs(xth_a) <= 1.8)
    return Session(sid, np.array(t), np.array(sx), np.array(sn), np.array(sp),
                   np.array(tt), np.array(th), nl_a, np.array(pc),
                   np.array([v if v is not None else np.nan for v in pctl]),
                   cs, np.array(xl), np.array(ex), pad_t, pad_u, valid,
                   np.array(pv, dtype=bool))


def src_series(sess: Session, src: str) -> tuple[np.ndarray, np.ndarray]:
    if src == "norm":
        return sess.t, sess.steer_norm
    if src == "pad":
        assert sess.pads_t is not None, "会话无 pads 数据"
        return sess.pads_t, sess.pads_u
    if src == "auto":
        if sess.pads_t is not None:
            return sess.pads_t, sess.pads_u
        return sess.t, sess.steer_x
    return sess.t, sess.steer_x


def zoh(src_t: np.ndarray, src_u: np.ndarray, q: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(src_t, q, side="right") - 1
    ok = idx >= 0
    out = np.where(ok, src_u[np.clip(idx, 0, len(src_u) - 1)], 0.0)
    return out


# ---------------- 车模拟合 ----------------

def _segments(sess: Session, gap: float) -> list[tuple[int, int]]:
    """连续线角段：样本间隔 ≤ gap，且都在可信区（|横向位|≤1.8 道、≥2 条线）。

    横向位超 1.8 道意味着车已贴/压墙（物理上限 ≈2.5 道），那里满杆也不产生
    偏航——不是车模样本，必须剔除。
    """
    segs = []
    start = None
    for i in range(len(sess.theta_t)):
        ok = bool(sess.valid[i])
        if not ok:
            if start is not None and i - start >= 3:
                segs.append((start, i - 1))
            start = None
            continue
        if start is None:
            start = i
        elif sess.theta_t[i] - sess.theta_t[i - 1] > gap:
            if i - start >= 3:
                segs.append((start, i - 1))
            start = i
    if start is not None and len(sess.theta_t) - start >= 3:
        segs.append((start, len(sess.theta_t) - 1))
    return [s for s in segs if s[1] - s[0] >= 2]


def fit_plant(sess: Session, frame: str = "yaw", src: str = "auto",
              gap: float = 0.35, d: float = 0.20, tau: float = 0.20,
              self_center: bool = True, seg_bias: bool = True,
              fixed_g: float | None = None) -> dict:
    """段内积分拟合 ``θ``，杆值外生（零阶保持）。

    yaw 型：``r' = (g·u(t−d) − k2·θ_meas − r)/τ + c_seg``、``θ' = r``。
    阻尼项用**实测** θ 作 forcing（(g,k2) 联合线性）；``c_seg`` 是每段自由偏置，
    吸收道路曲率与未建模漂移（Frisch-Waugh 组内投影消去，`seg_bias=True`）。
    head 型：``θ' = (K·u − θ)/τ + c_seg``（杆=车头角目标；无 k2）。
    段首以实测 θ 重置、r=0。
    """
    from scipy.signal import lfilter
    st, su = src_series(sess, src)
    segs = _segments(sess, gap)
    a = 1.0 - math.exp(-H / tau)
    A = np.zeros((2, 2))
    b = np.zeros(2)
    segs_basis = []
    for (i0, i1) in segs:
        q0, q1 = sess.theta_t[i0], sess.theta_t[i1]
        n = int(round((q1 - q0) / H))
        if n < 2:
            continue
        grid = q0 + np.arange(1, n + 1) * H
        u = zoh(st, su, grid - d)
        th_meas = np.interp(grid, sess.theta_t[i0:i1 + 1],
                            sess.theta[i0:i1 + 1])
        idx_all = np.round((sess.theta_t[i0 + 1:i1 + 1] - q0) / H).astype(int) - 1
        iis = np.arange(i0 + 1, i1 + 1)
        keep = (idx_all >= 0) & (idx_all < n)
        idx = idx_all[keep]
        iis = iis[keep]
        if len(idx) < 3:
            continue
        yv = sess.theta[iis] - sess.theta[i0]
        lp = lambda x: lfilter([a], [1, -(1 - a)], x, zi=[0.0])[0]  # noqa: E731
        if frame == "head":
            B1 = lp(u)
            B2 = np.zeros_like(B1)
        else:
            B1 = np.cumsum(lp(u)) * H
            B2 = -np.cumsum(lp(th_meas)) * H
        B3 = np.cumsum(lp(np.ones_like(u))) * H     # 常值偏置的响应
        x1, x2, x3, yy = B1[idx], B2[idx], B3[idx], yv
        if seg_bias:
            zz = float(x3 @ x3)
            if zz > 1e-12:
                c1 = float(x3 @ x1) / zz
                c2 = float(x3 @ x2) / zz
                cy = float(x3 @ yy) / zz
                x1 = x1 - c1 * x3
                x2 = x2 - c2 * x3
                yy = yy - cy * x3
        A[0, 0] += float(x1 @ x1)
        A[0, 1] += float(x1 @ x2)
        A[1, 0] += float(x2 @ x1)
        A[1, 1] += float(x2 @ x2)
        b[0] += float(x1 @ yy)
        b[1] += float(x2 @ yy)
        segs_basis.append((i0, iis, idx, u, th_meas, B1, B2, B3))
    if fixed_g is not None:
        g = float(fixed_g)
        k2 = 0.0
    elif frame == "head":
        g = b[0] / A[0, 0] if A[0, 0] > 0 else 0.0
        k2 = 0.0
    else:
        try:
            sol = np.linalg.solve(A, b)
            g, k2 = float(sol[0]), (float(sol[1]) if self_center else 0.0)
        except np.linalg.LinAlgError:
            g, k2 = 0.0, 0.0
    se = se_one = 0.0
    nn = one = 0
    for (i0, iis, idx, u, th_meas, B1, B2, B3) in segs_basis:
        yv = sess.theta[iis] - sess.theta[i0]
        pred = g * B1 + k2 * B2
        if seg_bias and len(idx) > 1:
            zz = float(B3[idx] @ B3[idx])
            if zz > 1e-12:
                c = float(B3[idx] @ (yv - pred[idx])) / zz
                pred = pred + c * B3
        resid = pred[idx] - yv
        se += float(resid @ resid)
        nn += len(resid)
        se_one += float(np.sum(np.diff(resid) ** 2))
        one += max(0, len(resid) - 1)
    rmse = math.sqrt(se / max(nn, 1))
    one_rmse = math.sqrt(se_one / max(one, 1))
    var = float(np.var(sess.theta)) if len(sess.theta) else 0.0
    return {"frame": frame, "src": src, "gap": gap, "d": d, "tau": tau,
            "g": g, "k2": k2, "rmse": rmse, "one_rmse": one_rmse,
            "n": nn, "var": var, "segs": len(segs)}


def _sim_asym(u: np.ndarray, g: float, d_steps: int, tau_up: float,
              tau_dn: float, tau_r: float, h: float = H) -> tuple[np.ndarray, np.ndarray]:
    """杆→转向状态（攻击快/释放慢）→偏航率一阶→θ。返回 (r, θ) 基响应。"""
    n = len(u)
    a_up = 1.0 - math.exp(-h / tau_up)
    dn = math.exp(-h / tau_dn)
    a_r = 1.0 - math.exp(-h / tau_r)
    a = 0.0
    r = 0.0
    th = np.zeros(n)
    for k in range(n):
        uk = u[k - d_steps] if k >= d_steps else 0.0
        a = a + a_up * (uk - a) if abs(uk) > 0.02 else a * dn
        r += a_r * (g * a - r)
        th[k] = th[k - 1] + r * h if k else 0.0
    return r, th


def fit_asym(sess: Session, src: str = "auto", gap: float = 0.35,
             d: float = 0.15, tau_up: float = 0.10, tau_dn: float = 0.60,
             tau_r: float = 0.08, seg_bias: bool = True) -> dict:
    """非对称一阶：u → a（按下 τ_up / 松开 τ_dn）→ r（τ_r）→ θ。

    g 用段内闭式最小二乘（θ 对 g 线性），段偏置同 fit_plant。
    """
    st, su = src_series(sess, src)
    segs = _segments(sess, gap)
    d_steps = int(round(d / H))
    A = np.zeros((2, 2))
    b = np.zeros(2)
    cache = []
    for (i0, i1) in segs:
        q0, q1 = sess.theta_t[i0], sess.theta_t[i1]
        n = int(round((q1 - q0) / H))
        if n < 2:
            continue
        grid = q0 + np.arange(1, n + 1) * H
        u = zoh(st, su, grid - d)
        idx_all = np.round((sess.theta_t[i0 + 1:i1 + 1] - q0) / H).astype(int) - 1
        iis = np.arange(i0 + 1, i1 + 1)
        keep = (idx_all >= 0) & (idx_all < n)
        idx, iis = idx_all[keep], iis[keep]
        if len(idx) < 3:
            continue
        yv = sess.theta[iis] - sess.theta[i0]
        _, B1 = _sim_asym(u, 1.0, 0, tau_up, tau_dn, tau_r)
        B3 = np.cumsum(np.ones(n)) * H
        x1, x3, yy = B1[idx], B3[idx], yv
        if seg_bias:
            zz = float(x3 @ x3)
            if zz > 1e-12:
                c1 = float(x3 @ x1) / zz
                cy = float(x3 @ yy) / zz
                x1 = x1 - c1 * x3
                yy = yy - cy * x3
        A[0, 0] += float(x1 @ x1)
        b[0] += float(x1 @ yy)
        cache.append((i0, iis, idx, yv, B1, B3))
    g = b[0] / A[0, 0] if A[0, 0] > 0 else 0.0
    se = se_one = 0.0
    nn = one = 0
    for (i0, iis, idx, yv, B1, B3) in cache:
        pred = g * B1
        zz = float(B3[idx] @ B3[idx])
        if zz > 1e-12:
            c = float(B3[idx] @ (yv - pred[idx])) / zz
            pred = pred + c * B3
        resid = pred[idx] - yv
        se += float(resid @ resid)
        nn += len(resid)
        se_one += float(np.sum(np.diff(resid) ** 2))
        one += max(0, len(resid) - 1)
    return {"src": src, "d": d, "tau_up": tau_up, "tau_dn": tau_dn,
            "tau_r": tau_r, "g": g,
            "rmse": math.sqrt(se / max(nn, 1)),
            "one_rmse": math.sqrt(se_one / max(one, 1)), "n": nn}


def scan_asym(sess: Session, src: str = "auto", gap: float = 0.35,
              d_grid=None, up_grid=None, dn_grid=None) -> dict:
    d_grid = d_grid if d_grid is not None else np.arange(0.08, 0.31, 0.04)
    up_grid = up_grid if up_grid is not None else (0.04, 0.08, 0.12, 0.20)
    dn_grid = dn_grid if dn_grid is not None else (0.2, 0.35, 0.5, 0.7, 1.0, 1.5)
    best = None
    for d in d_grid:
        for up in up_grid:
            for dn in dn_grid:
                r = fit_asym(sess, src, gap, float(d), float(up), float(dn), 0.08)
                if best is None or r["rmse"] < best["rmse"]:
                    best = r
    return best


def scan(sess: Session, frame: str, src: str, gap: float,
         d_grid=None, tau_grid=None) -> dict:
    d_grid = np.arange(0.0, 0.62, 0.02) if d_grid is None else d_grid
    tau_grid = np.arange(0.02, 0.62, 0.02) if tau_grid is None else tau_grid
    best = None
    for d in d_grid:
        for tau in tau_grid:
            r = fit_plant(sess, frame, src, gap, float(d), float(tau))
            if best is None or r["rmse"] < best["rmse"]:
                best = r
    return best


# ---------------- 分段增益 / 松杆回正 ----------------

def binned_gain(sess: Session, src: str, d: float, smooth_s: float = 0.10,
                bins=((0.02, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 0.9),
                      (0.9, 1.01))) -> list[dict]:
    """线角中心差分对杆的增益（只用可信样本；中位数抗线角跳点）。"""
    st, su = src_series(sess, src)
    out = []
    for lo, hi in bins:
        rat = []
        pairs = []
        for i in range(1, len(sess.theta_t) - 1):
            if not (sess.valid[i - 1] and sess.valid[i] and sess.valid[i + 1]):
                continue
            dt = sess.theta_t[i + 1] - sess.theta_t[i - 1]
            if dt <= 0.02 or dt > 2 * smooth_s + 0.06:
                continue
            quart = sess.theta_t[i + 1] - sess.theta_t[i]
            u = float(np.mean(zoh(st, su, np.array(
                [sess.theta_t[i] - d, sess.theta_t[i - 1] - d]))))
            if not (hi * 0.45 <= abs(u) < hi * 1.05) and lo < 0.02:
                pass
            if not (lo <= abs(u) < hi):
                continue
            rate = (sess.theta[i + 1] - sess.theta[i - 1]) / dt
            rat.append(rate / u)
            pairs.append((u, rate, quart))
        if not rat:
            out.append({"bin": f"{lo}-{hi}", "gain": 0.0, "rate": 0.0, "n": 0})
            continue
        out.append({"bin": f"{lo}-{hi}", "gain": float(np.median(rat)),
                    "rate": float(np.median([p[1] for p in pairs])),
                    "n": len(rat)})
    return out


def curve(sess: Session, src: str, d: float, win: float = 0.16) -> list[dict]:
    """有效增益曲线：|u| 分箱 → 中位偏航率、中位 gain（只用可信样本）。"""
    st, su = src_series(sess, src)
    bins = ((0.02, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.45), (0.45, 0.6),
            (0.6, 0.75), (0.75, 0.9), (0.9, 1.01))
    g = np.arange(sess.theta_t[0] + win, sess.theta_t[-1] - win, 0.02)
    th = np.interp(g, sess.theta_t, sess.theta)
    ok = np.interp(g, sess.theta_t, sess.valid.astype(float)) > 0.99
    u = zoh(st, su, g - d)
    k = int(round(win / 2 / 0.02))
    rate = np.full_like(th, np.nan)
    rate[k:-k] = (th[2 * k:] - th[:-2 * k]) / (2 * k * 0.02)
    out = []
    for lo, hi in bins:
        m = ok & np.isfinite(rate) & (np.abs(u) >= lo) & (np.abs(u) < hi)
        n = int(m.sum())
        if n < 5:
            out.append({"bin": f"{lo}-{hi}", "n": n})
            continue
        gain = rate[m] / u[m]
        out.append({"bin": f"{lo}-{hi}", "n": n,
                    "u": float(np.median(np.abs(u[m]))),
                    "rate": float(np.median(rate[m])),
                    "gain": float(np.median(gain)),
                    "gain_p25": float(np.percentile(gain, 25)),
                    "gain_p75": float(np.percentile(gain, 75))})
    return out


def pulse_gain(sess: Session, src: str, d: float, up: float = 0.5,
               lo: float = 0.12, tail: float = 0.25) -> dict:
    """脉冲事件的积分增益：Δθ(t_end+tail) ≈ g·∫u dt（yaw 型短时近似）。

    不依赖稳态假设，也不被「按着不放」段污染；只取两端都干净的孤立脉冲。
    """
    st, su = src_series(sess, src)
    g = np.arange(sess.theta_t[0], sess.theta_t[-1], 0.01)
    th = np.interp(g, sess.theta_t, sess.theta)
    ok = np.interp(g, sess.theta_t, sess.valid.astype(float)) > 0.99
    u = zoh(st, su, g - d)
    i = 1
    out = []
    while i < len(g) - 1:
        if abs(u[i]) >= up and abs(u[i - 1]) < up:
            j = i
            while j + 1 < len(g) and abs(u[j]) >= lo:
                j += 1
            pre = int(0.3 / 0.01)
            t0, t1 = max(0, int(i - pre)), min(len(g) - 1,
                                               int(j + tail / 0.01))
            quiet = bool(np.all(np.abs(u[j + 1:t1 + 1]) < lo)) if t1 > j else True
            if ok[t0:t1 + 1].all() and quiet and t0 <= i - pre + 1:
                I = float(np.sum(u[i:j + 1]) * 0.01)   # 有符号积分
                dth = th[t1] - th[t0]
                if abs(I) > 0.005:
                    out.append({"g": dth / I, "I": I, "dth": dth,
                                "dur": (j - i + 1) * 0.01,
                                "u": float(np.mean(u[i:j + 1])),
                                "t": float(g[i])})
            i = j + 1
        else:
            i += 1
    if not out:
        return {"n": 0}
    gs = np.array([o["g"] for o in out])
    return {"n": len(out), "g_med": float(np.median(gs)),
            "g_p25": float(np.percentile(gs, 25)),
            "g_p75": float(np.percentile(gs, 75)),
            "dur_med": float(np.median([o["dur"] for o in out])),
            "u_med": float(np.median([o["u"] for o in out])),
            "events": out}


def release(sess: Session, src: str, d: float, hold_s: float = 0.30,
            thr: float = 0.03) -> dict:
    """杆≈0 的连续段里，θ 是否衰减（=车自身回正）。返回 dθ/dt 对 θ 的斜率。

    斜率≈0 → 无自回正（纯偏航率指令，松杆保持朝向）；
    斜率<0 → 有回正，时间常数 = -1/斜率（含道路曲率干扰，非纯车辆项）。
    """
    st, su = src_series(sess, src)
    n = len(sess.theta_t)
    num = den = 0.0
    pts = []
    i = 0
    while i < n:
        q = sess.theta_t[i]
        u = float(zoh(st, su, np.array([q - d]))[0])
        if abs(u) > thr:
            i += 1
            continue
        j = i
        while j + 1 < n and sess.theta_t[j + 1] - sess.theta_t[j] <= 0.12:
            uu = float(zoh(st, su, np.array([sess.theta_t[j] - d]))[0])
            if abs(uu) > thr:
                break
            j += 1
        if j > i and sess.theta_t[j] - sess.theta_t[i] >= hold_s:
            rate = (sess.theta[j] - sess.theta[i]) / (sess.theta_t[j] - sess.theta_t[i])
            mid = 0.5 * (sess.theta[j] + sess.theta[i])
            if abs(mid) > 0.005:
                pts.append((mid, rate))
                num += mid * rate
                den += mid * mid
        i = j + 1
    return {"slope": num / den if den > 0 else 0.0, "n": len(pts),
            "pts": pts}


# ---------------- 计划一阶模型的实证时间常数 ----------------

def plan_form(sess: Session, d: float, gap: float = 0.30) -> dict:
    """在实录上拟合计划自己的口径 ``θ_{k+1} = θ_k + a(ψ_cmd − θ_k)``。

    只在两次线角样本都新鲜、ψ_cmd 有效且近似持有时统计；返回 a → τ_eff。
    同时给出「a 相对计划 τ_heading_s=0.25 的比值」。
    """
    tsp = sess.theta_t
    st, su = src_series(sess, "auto")
    num = den = 0.0
    n = 0
    rows = []
    for i in range(1, len(tsp)):
        dt = tsp[i] - tsp[i - 1]
        if dt <= 0.02 or dt > gap:
            continue
        k = int(np.searchsorted(sess.t, tsp[i - 1]))
        if k >= len(sess.t):
            continue
        cmd = sess.psi_cmd[k]
        if not np.isfinite(cmd):
            continue
        e0 = cmd - sess.theta[i - 1]
        dth = sess.theta[i] - sess.theta[i - 1]
        a = 1.0 - math.exp(-dt / 0.25)
        x = a * e0
        num += x * dth
        den += x * x
        n += 1
        rows.append((dt, e0, dth, x, abs(cmd), sess.theta_t[i]))
    a_ratio = num / den if den > 0 else 0.0
    return {"ratio": a_ratio, "n": n, "rows": rows}


def plan_tau(sess: Session, d: float, gap: float = 0.30, dom: str = "all") -> dict:
    rows = plan_form(sess, d, gap)["rows"]
    if dom == "all":
        use = rows
    elif dom == "small":
        use = [r for r in rows if abs(r[3]) < 0.0035]
    elif dom == "mid":
        use = [r for r in rows if 0.0035 <= abs(r[3]) < 0.010]
    else:
        use = [r for r in rows if abs(r[3]) >= 0.010]
    num = sum(r[3] * r[2] for r in use)
    den = sum(r[3] * r[3] for r in use)
    if den <= 0:
        return {"n": 0}
    ratio = num / den
    # 反解每拍等效 a：假设每拍一阶 θ += a_eff*e，由 dt 归一
    a_eff = None
    if use:
        a_eff = ratio * (1.0 - math.exp(-np.mean([r[0] for r in use]) / 0.25))
    return {"n": len(use), "ratio": ratio,
            "a_eff": a_eff,
            "tau_eff": (-np.mean([r[0] for r in use]) / math.log(1 - min(a_eff, 0.999))
                        if a_eff and 0 < a_eff < 1 else None)}


# ---------------- 影子输入 vs 下发值 ----------------

def horizon_error(pred: np.ndarray, meas: np.ndarray, mask: np.ndarray,
                  h_s: float, dt: float = 0.01) -> dict:
    e = (pred - meas)[mask]
    if not len(e):
        return {"n": 0}
    return {"n": int(len(e)), "rmse": float(np.sqrt(np.mean(e ** 2))),
            "bias": float(np.mean(e)), "p95": float(np.percentile(np.abs(e), 95))}


def equiv(sess: Session, horizons=(0.25, 0.5, 1.0), plan_tau: float = 0.25,
          shadow_g: float = 0.28, shadow_tau: float = 0.2,
          hold_cmd: float = 0.03) -> dict:
    """同批真实输入下两种模型的短时预测误差（对实测线角）。

    A=计划口径：ψ 一阶逼近 ψ_cmd（τ=plan_tau），ψ_cmd 取本拍 plan.pc。
    B=影子口径：r'=(shadow_g·u−r)/τ、ψ'=r，u 取本拍实际下发杆 steer_x。
    只统计「指令在本拍后 h 秒内近似不变」（|Δψ_cmd|≤hold_cmd）的样本，
    否则模型与命令本身的变化混在一起。
    """
    out = {}
    g = np.arange(sess.theta_t[0], sess.theta_t[-1], 0.01)
    th = np.interp(g, sess.theta_t, sess.theta)
    okv = np.interp(g, sess.theta_t, sess.valid.astype(float)) > 0.99
    for h in horizons:
        n = int(round(h / 0.01))
        if n >= len(g) - 2:
            continue
        # 样本起点：控制拍
        tk = sess.t[:-n]
        idx0 = np.searchsorted(g, tk)
        idx1 = idx0 + n
        good = (idx0 >= 0) & (idx1 < len(g))
        tk = tk[good]
        idx0, idx1 = idx0[good], idx1[good]
        okm = okv[idx0] & okv[idx1]
        k = np.searchsorted(sess.t, tk)
        # A：计划一阶
        cmd = sess.psi_cmd[k]
        cmd_end = sess.psi_cmd[np.minimum(k + n * 0 + int(round(h / 0.05)),
                                          len(sess.t) - 1)]
        held = np.isfinite(cmd) & np.isfinite(cmd_end)             & (np.abs(cmd_end - cmd) <= hold_cmd)
        a = 1.0 - math.exp(-h / plan_tau)
        predA = th[idx0] + a * (np.where(held, cmd, 0.0) - th[idx0])
        mA = okm & held
        # B：影子按实际下发杆积分（起点 r 取 0.28 模型的当前 r 无法取，按 0）
        u0 = sess.steer_x[k]
        a_sh = 1.0 - math.exp(-h / shadow_tau)
        r_end = shadow_g * u0 * (1.0 - math.exp(-h / shadow_tau))
        predB = th[idx0] + shadow_g * u0 * (h - shadow_tau * (1 - math.exp(-h / shadow_tau)))
        mB = okm
        out[h] = {"A": horizon_error(predA, th[idx1], mA, h),
                  "B": horizon_error(predB, th[idx1], mB, h),
                  "held_frac": float(np.mean(held & okm)) if len(mA) else 0.0}
    return out


def closed_loop_tau(sess: Session, horizons=(0.1, 0.2, 0.3, 0.5, 0.8, 1.2),
                    hold_cmd: float = 0.03) -> dict:
    """实测「ψ_cmd → θ」的闭环一阶系数：Δθ ≈ a_eff(h)·(ψ_cmd − θ0)。

    a_eff 与计划 ``step`` 的 1−exp(−h/0.25) 对比即闭环等效误差；按 |e0| 分层
    看饱和/浅杆的影响。
    """
    g = np.arange(sess.theta_t[0], sess.theta_t[-1], 0.01)
    th = np.interp(g, sess.theta_t, sess.theta)
    okv = np.interp(g, sess.theta_t, sess.valid.astype(float)) > 0.99
    out = {}
    for h in horizons:
        n = int(round(h / 0.01))
        idx0 = np.searchsorted(g, sess.t)
        idx1 = idx0 + n
        good = idx1 < len(g)
        idx0, idx1 = idx0[good], idx1[good]
        tk = sess.t[good]
        okm = okv[idx0] & okv[idx1]
        k = np.searchsorted(sess.t, tk)
        cmd = sess.psi_cmd[k]
        kend = np.minimum(k + int(round(h / 0.05)), len(sess.t) - 1)
        cmd_end = sess.psi_cmd[kend]
        held = np.isfinite(cmd) & np.isfinite(cmd_end)             & (np.abs(cmd_end - cmd) <= hold_cmd)
        e0 = np.where(held, cmd, 0.0) - th[idx0]
        dth = th[idx1] - th[idx0]
        res = {}
        for name, m in (("all", okm & held),
                        ("small", okm & held & (np.abs(e0) < 0.03)),
                        ("mid", okm & held & (np.abs(e0) >= 0.03) & (np.abs(e0) < 0.07)),
                        ("big", okm & held & (np.abs(e0) >= 0.07))):
            if int(m.sum()) < 10:
                res[name] = {"n": int(m.sum())}
                continue
            x = e0[m]
            y = dth[m]
            a = float(x @ y / (x @ x)) if float(x @ x) > 0 else 0.0
            r = float(np.corrcoef(x, y)[0, 1])
            a_plan = 1.0 - math.exp(-h / 0.25)
            tau_eff = (-h / math.log(1 - a)) if 0 < a < 0.999 else None
            res[name] = {"n": int(m.sum()), "a": a, "a_plan": a_plan,
                         "tau_eff": tau_eff, "corr": r}
        out[h] = res
    return out


def contract(sess: Session, g: float = 0.45, span_s: float = 0.30) -> dict:
    """影子输入（steer_norm）与下发值（steer_x）的契约差：来源、峰值与预测影响。"""
    du = sess.steer_norm - sess.steer_x
    ad = np.abs(du)
    dz = np.abs(sess.steer_x) < 1e-6
    rate_cut = ad > 0.60
    cat = {
        "deadzone": int(np.sum(dz & (ad > 1e-6))),
        "rate_limit": int(np.sum(rate_cut)),
        "preview": int(np.sum(sess.pv & (ad > 0.05))),
        "other_small": int(np.sum((~dz) & (~rate_cut) & (ad > 1e-6))),
    }
    # 反向拍（下发值相对上一拍变号且幅度不小）上的差
    rev = np.zeros_like(ad, dtype=bool)
    rev[1:] = (np.sign(sess.steer_x[1:]) != np.sign(sess.steer_x[:-1]))         & (np.abs(sess.steer_x[1:]) > 0.05) & (np.abs(sess.steer_x[:-1]) > 0.05)
    return {"abs_mean": float(ad.mean()), "abs_p95": float(np.percentile(ad, 95)),
            "abs_max": float(ad.max()), "gt005": float((ad > 0.05).mean()),
            "gt02": float((ad > 0.2).mean()),
            "n": int(len(ad)), "cat": cat,
            "ad_on_rev": float(ad[rev].mean()) if rev.any() else 0.0,
            "n_rev": int(rev.sum()),
            "ad_max_rev": float(ad[rev].max()) if rev.any() else 0.0,
            # 影子按 norm、车按 sent 时的偏航率差与 span_s 内累计朝向差
            "rate_gap_rms": float(np.sqrt(np.mean((g * du) ** 2))),
            "rate_gap_max": float(np.max(np.abs(g * du))),
            "dtheta_span_max": float(np.max(np.abs(g * du)) * span_s),
            "dtheta_span_rms": float(np.sqrt(np.mean((g * du * span_s) ** 2)))}


def edge_profile(sess: Session, src: str, d: float = 0.0,
                 up: float = 0.3, bins=((0.3, 0.6), (0.6, 1.01)),
                 pre: float = 0.3, post: float = 0.6) -> list[dict]:
    """上升沿锁定的平均偏航率剖面：直接看延迟、上升与衰减。"""
    st, su = src_series(sess, src)
    g = np.arange(sess.theta_t[0], sess.theta_t[-1], 0.01)
    th = np.interp(g, sess.theta_t, sess.theta)
    ok = np.interp(g, sess.theta_t, sess.valid.astype(float)) > 0.99
    u = zoh(st, su, g - d)
    k = 3
    rate = np.full_like(th, np.nan)
    rate[k:-k] = (th[2 * k:] - th[:-2 * k]) / (2 * k * 0.01)
    out = []
    for lo, hi in bins:
        acc = []
        for i in range(int(pre / 0.01), len(g) - int(post / 0.01)):
            if abs(u[i]) >= up and abs(u[i - 1]) < 0.1 and lo <= abs(u[i]) < hi:
                w0, w1 = i - int(pre / 0.01), i + int(post / 0.01)
                if ok[w0:w1].all() and np.all(np.abs(u[i + 1:i + int(0.25/0.01)]) >= 0.1):
                    acc.append(rate[w0:w1])
        if len(acc) < 3:
            out.append({"bin": f"{lo}-{hi}", "n": len(acc)})
            continue
        m = np.nanmean(np.array(acc), axis=0)
        out.append({"bin": f"{lo}-{hi}", "n": len(acc),
                    "t": np.arange(-pre, post, 0.01), "rate": m})
    return out


def shadow_divergence(sess: Session, g: float, tau: float) -> dict:
    d = np.abs(sess.steer_norm - sess.steer_x)
    # 影子按 steer_norm 推进、车按 steer_x 响应 → 稳态偏航率差 = g·Δu
    rate_gap = g * (sess.steer_norm - sess.steer_x)
    return {"abs_mean": float(np.mean(d)), "abs_p95": float(np.percentile(d, 95)),
            "abs_max": float(np.max(d)),
            "frac_gt_0_05": float(np.mean(d > 0.05)),
            "frac_gt_0_2": float(np.mean(d > 0.2)),
            "rate_gap_rms": float(np.sqrt(np.mean(rate_gap ** 2))),
            "rate_gap_max": float(np.max(np.abs(rate_gap)))}


def holdout(train_sid: str, test_sid: str, frame: str = "yaw",
            src: str = "auto") -> dict:
    """一局拟合、另一局留出：固定 (g,τ,d) 直接预测，与「θ 不变」基线对比。"""
    tr = load(train_sid)
    te = load(test_sid)
    b = scan(tr, frame, src, 0.35)
    # 留出：用训练参数，k2 也固定（不再拟合）
    fixed = fit_plant(te, frame, src, 0.35, b["d"], b["tau"], fixed_g=b["g"])
    # 基线 1：θ 不变
    base_flat = 0.0
    n = 0
    for (i0, i1) in _segments(te, 0.35):
        d = te.theta[i0 + 1:i1 + 1] - te.theta[i0]
        base_flat += float(d @ d)
        n += len(d)
    base = math.sqrt(base_flat / max(n, 1))
    # 基线 2：只用 g 缩放（无 τ/d，瞬时比例）
    own = scan(te, frame, src, 0.35)
    return {"train": train_sid, "test": test_sid, "frame": frame,
            "params": (b["d"], b["tau"], b["g"]),
            "train_rmse": b["rmse"], "test_rmse": fixed["rmse"],
            "test_one": fixed["one_rmse"], "base_flat": base,
            "own": (own["d"], own["tau"], own["g"], own["rmse"]), "n": fixed["n"]}


def cmd(kind: str, sid: str = ""):
    sids = [sid] if sid else ["20261118_225251", "20261118_195555",
                              "20261119_104138"]
    if kind == "stats":
        for s in sids:
            sess = load(s)
            nz = int(np.sum(np.abs(sess.steer_x) > 1e-6))
            print(f"{s}: ticks={len(sess.t)} dur={sess.t[-1]:.1f}s "
                  f"theta={len(sess.theta_t)} steer_x!=0={nz} "
                  f"pads={'yes' if sess.pads_t is not None else 'no'} "
                  f"theta_span={sess.theta_t[0]:.1f}~{sess.theta_t[-1]:.1f}")
            if sess.pads_t is not None:
                print(f"   pad span {sess.pads_t[0]:.1f}~{sess.pads_t[-1]:.1f} "
                      f"|u|>0.05 {np.mean(np.abs(sess.pads_u) > 0.05)*100:.1f}% "
                      f"|u|>0.9 {np.mean(np.abs(sess.pads_u) > 0.9)*100:.1f}%")
    elif kind == "fit":
        for s in sids:
            sess = load(s)
            src = "auto"
            for frame in ("yaw", "head"):
                b = scan(sess, frame, src, 0.35)
                print(f"{s} {frame:4s} src={src} d={b['d']:.2f} tau={b['tau']:.2f} "
                      f"g={b['g']:.3f} k2={b['k2']:.3f} rmse={b['rmse']:.4f} "
                      f"one={b['one_rmse']:.4f} var={b['var']:.4f} n={b['n']} "
                      f"segs={b['segs']}")
    elif kind == "nonlin":
        for s in sids:
            sess = load(s)
            src = "auto"
            for d in (0.15, 0.20, 0.25):
                bins = binned_gain(sess, src, d)
                print(f"{s} d={d:.2f} " + " | ".join(
                    f"{b['bin']}:{b['gain']:.3f}(n{b['n']})" for b in bins))
    elif kind == "curve":
        for s_ in sids:
            sess = load(s_)
            for d in (0.10, 0.15, 0.20, 0.25):
                cs = curve(sess, "auto", d)
                print(f"{s_} d={d:.2f} " + " | ".join(
                    (f"{c['bin']}: n{c['n']} rate{c['rate']:+.3f} gain{c['gain']:+.2f}"
                     f"[{c['gain_p25']:+.2f},{c['gain_p75']:+.2f}]")
                    if c.get("n", 0) >= 5 else f"{c['bin']}: n{c['n']}"
                    for c in cs))
    elif kind == "pulse":
        for s_ in sids:
            sess = load(s_)
            for d in (0.12, 0.18, 0.24):
                r = pulse_gain(sess, "auto", d)
                if not r.get("n"):
                    print(f"{s_} d={d:.2f} n=0")
                    continue
                print(f"{s_} d={d:.2f} n={r['n']} g_med={r['g_med']:.3f} "
                      f"[{r['g_p25']:.3f},{r['g_p75']:.3f}] dur={r['dur_med']:.2f}s "
                      f"u={r['u_med']:.2f}")
    elif kind == "release":
        for s in sids:
            sess = load(s)
            for d in (0.15, 0.20):
                r = release(sess, "auto", d)
                print(f"{s} d={d:.2f} slope={r['slope']:.3f} /s n={r['n']}")
    elif kind == "planform":
        for s in sids:
            sess = load(s)
            for dom in ("all", "small", "mid", "large"):
                r = plan_tau(sess, 0.2, dom=dom)
                if not r.get("n"):
                    print(f"{s} {dom}: n=0")
                    continue
                print(f"{s} {dom:6s} n={r['n']} ratio={r['ratio']:.2f} "
                      f"a_eff={r['a_eff']} tau_eff={r['tau_eff']}")
    elif kind == "asym":
        for s_ in sids:
            sess = load(s_)
            r = scan_asym(sess, "auto", 0.35)
            print(f"{s_} asym d={r['d']:.2f} up={r['tau_up']:.2f} dn={r['tau_dn']:.2f} "
                  f"g={r['g']:.3f} rmse={r['rmse']:.4f} one={r['one_rmse']:.4f} n={r['n']}")
    elif kind == "holdout":
        pairs = [("20261118_195555", "20261119_104138"),
                 ("20261119_104138", "20261118_195555"),
                 ("20261118_195555", "20261118_225251")]
        for a, b in pairs:
            for frame in ("yaw", "head"):
                r = holdout(a, b, frame)
                print(f"train {r['train']} -> test {r['test']} {frame}: "
                      f"params d={r['params'][0]:.2f} tau={r['params'][1]:.2f} "
                      f"g={r['params'][2]:.3f} | train_rmse={r['train_rmse']:.4f} "
                      f"test_rmse={r['test_rmse']:.4f} (one={r['test_one']:.4f}) "
                      f"flat_baseline={r['base_flat']:.4f} | test own d={r['own'][0]:.2f} "
                      f"tau={r['own'][1]:.2f} g={r['own'][2]:.3f} rmse={r['own'][3]:.4f}")
    elif kind == "contract":
        for s_ in sids:
            sess = load(s_)
            r = contract(sess)
            print(f"{s_} n={r['n']} abs_mean={r['abs_mean']:.4f} p95={r['abs_p95']:.4f} "
                  f"max={r['abs_max']:.3f} gt0.05={r['gt005']*100:.2f}% "
                  f"gt0.2={r['gt02']*100:.2f}% cat={r['cat']} "
                  f"| rev n={r['n_rev']} ad_mean={r['ad_on_rev']:.3f} max={r['ad_max_rev']:.3f}")
            print(f"   rate_gap rms={r['rate_gap_rms']:.4f} max={r['rate_gap_max']:.3f} "
                  f"dtheta(0.3s) rms={r['dtheta_span_rms']:.4f} max={r['dtheta_span_max']:.4f}")
    elif kind == "edges":
        for s_ in sids:
            sess = load(s_)
            for pr in edge_profile(sess, "auto", 0.0):
                if pr.get("n", 0) < 3:
                    print(f"{s_} {pr['bin']} n={pr.get('n')}")
                    continue
                t = pr["t"]
                r = pr["rate"]
                pick = [i for i in range(len(t)) if abs(t[i]) < 1e-6
                        or abs((t[i] % 0.1)) < 1e-6 or abs((t[i] % 0.1) - 0.1) < 1e-6]
                line = " ".join(f"{t[i]:+.2f}:{r[i]:+.3f}" for i in pick)
                print(f"{s_} {pr['bin']} n={pr['n']} {line}")
    elif kind == "equiv":
        for s_ in sids:
            sess = load(s_)
            r = equiv(sess)
            for h, v in r.items():
                print(f"{s_} h={h:.2f} A(plan) n={v['A'].get('n')} "
                      f"rmse={v['A'].get('rmse', 0):.4f} bias={v['A'].get('bias', 0):+.4f} "
                      f"| B(shadow) n={v['B'].get('n')} rmse={v['B'].get('rmse', 0):.4f} "
                      f"bias={v['B'].get('bias', 0):+.4f} | held={v['held_frac']:.2f}")
    elif kind == "cltau":
        for s_ in sids:
            sess = load(s_)
            r = closed_loop_tau(sess)
            for h, v in r.items():
                line = f"{s_} h={h:.2f} "
                for name in ("all", "small", "mid", "big"):
                    q = v[name]
                    if q.get("n", 0) < 10:
                        line += f"| {name}: n{q.get('n', 0)} "
                    else:
                        line += (f"| {name}: n{q['n']} a={q['a']:.2f}/"
                                 f"{q['a_plan']:.2f} tau={q['tau_eff'] if q['tau_eff'] is None else round(q['tau_eff'], 2)} "
                                 f"r={q['corr']:.2f} ")
                print(line)
    elif kind == "shadow":
        for s in sids:
            sess = load(s)
            r = shadow_divergence(sess, 0.46, 0.2)
            print(f"{s} " + " ".join(f"{k}={v:.4f}" for k, v in r.items()))


if __name__ == "__main__":
    cmd(sys.argv[1] if len(sys.argv) > 1 else "stats",
        sys.argv[2] if len(sys.argv) > 2 else "")
