# -*- coding: utf-8 -*-
"""线观测：等间距 + 深度锚定、颜色无关、锚跳变确认与降级。

2026-10-24 重写后，道号不再由黄边界/颜色决定：每拍用新鲜深度路心做锚点，
``k=round(anchor+L)``，线只负责等间距约束与子道残差修正。本文件锁住这条
口径，以及 worker 的同拍快照/锚点入队协议。
"""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from maaracing_master.plugins.speedrush import lane_obs as la
from maaracing_master.plugins.speedrush.config import load_decision
from maaracing_master.plugins.speedrush.lane_obs import (
    COAST_MAX_TICKS, LaneObsConfig, LaneObsWorker, LineTracker, _fit)


def _line(L, color="y", n=20, theta=0.0, side=None):
    if side is None:
        side = -1 if L < 0 else 1
    return {"L": L, "color": color, "n": n, "theta": theta,
            "yellow": 1.0 if color == "y" else 0.0,
            "white": 1.0 if color == "w" else 0.0, "side": side}


def _synth_line(s, a, y0=560, y1=700, n=15):
    """全幅像素直线 x = s·y + a → 模型半分辨率点集（_fit 的输入口径）。"""
    return [{"yc": y / 2.0, "xc": (s * y + a) / 2.0, "p": 1.0}
            for y in (y0 + i * (y1 - y0) / (n - 1) for i in range(n))]


def test_line_theta_uses_given_focal():
    """线角正切 = 像素偏置 ÷ 调用方给的焦距（标定值）：焦距越小线角越大；
    无焦距只丢线角、不丢横位（模块内不再有焦距字面量）。"""
    pts = _synth_line(0.05, 520.0)
    a670 = _fit(pts, 1.28, *la.NEAR_Y, 670.0)
    a808 = _fit(pts, 1.28, *la.NEAR_Y, 807.7)
    assert a670["L"] == pytest.approx(a808["L"])      # 横位不依赖焦距
    assert a670["theta"] is not None and a808["theta"] is not None
    ratio = math.tan(a670["theta"]) / math.tan(a808["theta"])
    assert ratio == pytest.approx(807.7 / 670.0, rel=1e-9)
    none = _fit(pts, 1.28, *la.NEAR_Y, None)
    assert none["theta"] is None
    assert none["L"] == pytest.approx(a670["L"])


def test_worker_focal_holds_last_valid():
    """缺值/越界焦距保持上一次有效测量（不退回常量）。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    w._set_focal(700.0)
    assert w._focal == pytest.approx(700.0)
    w._set_focal(None)
    assert w._focal == pytest.approx(700.0)
    w._set_focal(800.0)
    assert w._focal == pytest.approx(700.0 + la.FOCAL_EMA * 100.0)
    w._set_focal(300.0)                               # 物理带外
    assert w._focal == pytest.approx(700.0 + la.FOCAL_EMA * 100.0)


def test_distance_bands_single_source():
    """近/中/远三段的米制边界来自 ranges（15/40/80 米）；行号按当拍标定反算，
    无焦距时退回冷启动口径（只近段）。"""
    assert (la.NEAR_MAX_M, la.MID_MAX_M, la.FAR_MAX_M) == (15.0, 40.0, 80.0)
    cfg = load_decision().lane_obs
    assert (cfg.near_max_m, cfg.mid_max_m, cfg.far_max_m) == (15.0, 40.0, 80.0)
    bands = la._dist_bands(1.263, 807.7, 361.2, 15.0, 40.0, 80.0)
    assert bands is not None
    scan, near, mid, far = bands
    assert near[1] == 720.0 and near[0] > mid[0] > far[0]   # 越远行号越小
    assert near[0] == pytest.approx(361.2 + 807.7 * 1.263 / 15.0, abs=0.6)
    assert far[0] == pytest.approx(361.2 + 807.7 * 1.263 / 80.0, abs=0.6)
    assert 150 <= scan <= 200
    assert la._dist_bands(1.263, None, 361.2) is None


def test_lines_from_prob_fits_three_distance_bands():
    """按距离分三段拟合（近/中/远）：一条贯穿全图的行应有三个拟合段，采纳段
    （fits[0]）仍是近段——近场 L/theta 不因加段而改变。"""
    prob = np.zeros((360, 640), dtype=np.float32)
    for y in range(185, 360):
        x = int(0.5 * y + 100)
        prob[y, max(0, x - 2):x + 2] = 0.9
    lines = la._lines_from_prob(prob, 1.28, 807.7, 361.2)
    assert len(lines) == 1
    ys = [f["y0"] for f in lines[0]["fits"]]
    assert len(ys) == 3
    assert ys[0] > ys[1] > ys[2]        # 近→中→远（行号递减）
    assert ys[2] < 390.0                # 远段覆盖到 40 米以外


def test_line_evals_picks_segment_covering_row():
    """框底行落在哪段就用哪段的方程：近/中/远三段各不同，按行切换。"""
    line = {"L": 0.0, "accepted": True, "rms": 0.5, "theta": 0.0,
            "fits": [
                {"s": 0.5, "a": 100.0, "y0": 560.0, "y1": 700.0},
                {"s": 0.6, "a": 80.0, "y0": 460.0, "y1": 559.0},
                {"s": 0.7, "a": 60.0, "y0": 380.0, "y1": 459.0},
            ]}
    assert la._line_evals(600.0, [line])[0][0] == pytest.approx(400.0)
    assert la._line_evals(500.0, [line])[0][0] == pytest.approx(380.0)
    assert la._line_evals(400.0, [line])[0][0] == pytest.approx(340.0)


def _extract_track(monkeypatch, pts):
    monkeypatch.setattr(la, "_build_tracks", lambda rows: [pts])
    return la._lines_from_prob(np.zeros((360, 640), np.float32),
                               1.276, 807.7, 360.4)


def test_reversed_far_segment_is_cut_without_extrapolating_back(monkeypatch):
    """实帧 7301：接缝只差约 2.55px，但远段反向；中段不得再外推到坏段。"""
    pts = (_synth_line(1.5997139594, -97.2158436774, 376, 386, 6)
           + _synth_line(-3.5932883343, 1909.929057664, 388, 428, 21))
    line, = _extract_track(monkeypatch, pts)
    assert len(line["fits"]) == 1
    line["accepted"] = True
    assert la._line_evals(408, [line])[0][0] == pytest.approx(443.867, abs=0.01)
    assert la._line_evals(381, [line]) == []
    assert la.lane_pos_nearest(512, 381, [line], 1.276, 360.4) is None


@pytest.mark.parametrize("bad", ["noisy", "offset"])
def test_far_segment_needs_own_quality_and_position(monkeypatch, bad):
    mid = _synth_line(-0.8, 930, 388, 428, 21)
    far = _synth_line(-0.8, 930 + (50 if bad == "offset" else 0), 376, 386, 6)
    if bad == "noisy":
        for i, p in enumerate(far):
            p["xc"] += 5 * (-1 if i % 2 else 1)
    line, = _extract_track(monkeypatch, far + mid)
    assert len(line["fits"]) == 1


def test_smooth_bend_keeps_anchor_and_connects_all_segments(monkeypatch):
    near = _synth_line(-0.8, 930, 430, 700, 100)
    # 逐段缓弯，接缝处连续；远段不改变近段的位置与车头角。
    mid = _synth_line(-0.9, 973, 388, 428, 21)
    far = _synth_line(-1.0, 1011.7, 376, 386, 6)
    line, = _extract_track(monkeypatch, far + mid + near)
    assert len(line["fits"]) == 3
    anchor = _fit(near, 1.276, 430, 720, 807.7, 360.4)
    assert line["L"] == pytest.approx(anchor["L"], abs=0.001)
    for inner, outer in zip(line["fits"], line["fits"][1:]):
        seam = (inner["y0"] + outer["y1"]) / 2
        assert inner["s"] * seam + inner["a"] == pytest.approx(
            outer["s"] * seam + outer["a"], abs=1e-6)
    line["accepted"] = True
    assert la._line_evals(381, [line])


def test_bad_near_does_not_hide_good_mid(monkeypatch):
    near = _synth_line(-0.8, 930, 430, 700, 100)
    for i, p in enumerate(near):
        p["xc"] += 8 * (-1 if i % 2 else 1)
    mid = _synth_line(-0.8, 930, 388, 428, 21)
    line, = _extract_track(monkeypatch, mid + near)
    assert line["rms"] <= la.LINE_RMS_MAX
    assert line["y1"] == 428
    line["accepted"] = True
    assert la._line_evals(600, [line]) == []


def test_far_only_geometry_cannot_establish_ego_lock(monkeypatch):
    lines = _extract_track(monkeypatch, _synth_line(-0.8, 930, 376, 386, 6))
    assert len(lines) == 1 and lines[0]["fits"]
    tracker = LineTracker()
    for fid in range(1, 9):
        res = tracker.update(lines, fid, 0.05, anchor=0.2)
    assert res["n_locked"] == 0
    assert res["locked"] == 0
    assert res["theta"] is None


def test_remote_only_cannot_supply_horizon_or_camera_height(monkeypatch):
    lines = _extract_track(monkeypatch, _synth_line(-0.8, 930, 376, 386, 6))
    left, right = dict(lines[0]), dict(lines[0])
    for line, s, k in ((left, -1, -1), (right, 1, 1)):
        line.update(accepted=True, k=k)
        line["fits"] = [dict(line["fits"][0], s=s, a=640 - s * 360)]
    assert la._vanish_row([left, right]) is None
    assert la._cam_h_from_lines([left, right], 360) is None


def test_worker_expires_horizon_evidence():
    worker = LaneObsWorker(LaneObsConfig(enabled=True))
    worker._yv, worker._yv_raw, worker._yv_t = 360.4, 360.5, 10.0
    worker._yv_evidence_t = 10.0
    res = LineTracker().update([], 1, 0.05)
    fresh = worker._snapshot(res, 1, 1.276, 10, 10.1, False)
    assert fresh["y_v"] == pytest.approx(360.4)
    stale = worker._snapshot(res, 2, 1.276, 12, 12.1, False)
    assert stale["y_v"] is None
    assert stale["cam_h_lane"] is None
    assert stale["y_v_age_ms"] == pytest.approx(2100)


def test_new_raw_sample_does_not_freshen_expired_published_horizon():
    worker = LaneObsWorker(LaneObsConfig(enabled=True))
    worker._yv, worker._yv_t, worker._yv_evidence_t = 360.0, 10.0, 10.0
    worker._yv_hist.extend([360.0] * la.YV_WIN)
    worker._yv, worker._yv_t, worker._yv_refill = la._smooth_vanish(
        worker._yv_hist, 378.0, 12.0, worker._yv_t, worker._yv)
    assert worker._yv_refill
    res = LineTracker().update([], 1, 0.05)
    assert worker._snapshot(res, 1, 1.276, 12, 12.01, False)["y_v"] is None


def test_extraction_does_not_use_expired_horizon(monkeypatch):
    worker = LaneObsWorker(LaneObsConfig(enabled=True))
    worker._yv, worker._yv_t, worker._yv_evidence_t = 378.0, 10.0, 10.0
    monkeypatch.setattr(la.time, "perf_counter", lambda: 12.0)
    monkeypatch.setattr(worker, "_run", lambda rgb: np.zeros((360, 640), np.float32))
    captured = []
    def extract(prob, cam_h, focal, y_v, **kwargs):
        captured.append(y_v)
        return []
    monkeypatch.setattr(la, "_lines_from_prob", extract)
    worker._infer(np.zeros((720, 1280, 3), np.uint8), 1.276, 807.7)
    assert captured == [None]


@pytest.mark.parametrize("bad", [{"rms": 9.0}, {"theta": 0.8}])
def test_bad_geometry_never_updates_ego_or_identity(bad):
    tracker = LineTracker()
    line = dict(_line(-0.2), **bad)
    for fid in range(1, 9):
        res = tracker.update([line], fid, 0.05, anchor=0.2)
    assert res["x"] == pytest.approx(0.2)
    assert res["n_locked"] == 0
    assert not res["lines"][0]["accepted"]


def test_normal_recorded_three_segments_survive_seam_fit(monkeypatch):
    """正常实帧 1448 的方程：接缝的小误差由受约束拟合吸收，不能一刀砍远场。"""
    pts = (_synth_line(-1.2019878539, 1075.66851532, 376, 386, 6)
           + _synth_line(-1.3018569161, 1112.76487779, 388, 428, 21)
           + _synth_line(-1.2957035893, 1113.96575837, 430, 698, 135))
    line, = _extract_track(monkeypatch, pts)
    assert len(line["fits"]) == 3
    line["accepted"] = True
    for y in (564, 408, 381):
        assert la._line_evals(y, [line])


def test_crossing_far_lines_cannot_swap_near_identities(monkeypatch):
    left = (_synth_line(-1.8, 1336.6, 376, 386, 6)
            + _synth_line(-1.3, 1143, 388, 428, 21)
            + _synth_line(-0.8, 928, 430, 700, 100))
    right = (_synth_line(1.8, -56.6, 376, 386, 6)
             + _synth_line(1.3, 137, 388, 428, 21)
             + _synth_line(0.8, 352, 430, 700, 100))
    monkeypatch.setattr(la, "_build_tracks", lambda rows: [left, right])
    lines = la._lines_from_prob(np.zeros((360, 640), np.float32),
                               1.276, 807.7, 360.4)
    for c in lines:
        c["accepted"] = True
    assert len(la._line_evals(600, lines)) == 2
    for y in (376, 381, 386, 388, 400, 428):
        evals = sorted(la._line_evals(y, lines))
        assert len(evals) < 2 or evals[0][1] < evals[1][1]


@pytest.mark.parametrize("offset, expected", [(0, 1), (150, 2)])
def test_dash_gap_joins_only_aligned_fragments(offset, expected):
    prob = np.zeros((360, 640), np.float32)
    for lo, hi, dx in ((215, 260, offset), (273, 350, 0)):
        for y in range(lo, hi):
            x = round((-0.8 * 2 * y + 930 + dx) / 2)
            prob[y, x - 2:x + 3] = 0.9
    lines = la._lines_from_prob(prob, 1.276, 807.7, 360.4)
    assert len(lines) == expected
    if not offset:
        assert min(f["y0"] for f in lines[0]["fits"]) <= 430
        assert max(f["y1"] for f in lines[0]["fits"]) >= 698


def test_gap_merge_cannot_destroy_good_near_evidence(monkeypatch):
    inner = _synth_line(-0.8, 930, 580, 700, 61)
    outer = _synth_line(-0.8, 950, 450, 540, 46)
    monkeypatch.setattr(la, "_build_tracks", lambda rows: [outer, inner])
    lines = la._lines_from_prob(np.zeros((360, 640), np.float32),
                               1.276, 807.7, 360.4)
    assert len(lines) == 2
    assert all(c["rms"] <= la.LINE_RMS_MAX for c in lines)


def test_config_defaults_off_and_detect_every_frame():
    assert LaneObsConfig().enabled is False
    assert LaneObsConfig().detect_every == 1
    assert LaneObsConfig().provider == "cpu"   # 数据类缺省；生产值由 policy 决定
    assert load_decision().lane_obs is not None
    assert load_decision().lane_obs.detect_every == 1
    # 2026-11-13 起走 DML 槽位排班（感知放锁 → 线观测 30ms 槽窗 → 深度），
    # 不再靠 CPU 线程绕开 DML 锁
    assert load_decision().lane_obs.provider == "dml"
    assert load_decision().lane_obs.weights_fp16.endswith(
        "plus_small_fp16_384x640.onnx")


def test_no_anchor_stays_relock():
    """没有深度锚点时不凭空造绝对位；等第一拍新鲜路心。"""
    tr = LineTracker()
    r = tr.update([_line(-0.2)], 0, 0.05)
    assert r["state"] == "RELOCK" and r["x"] is None
    assert r["n_locked"] == 0


def test_anchor_beyond_physical_limit_is_garbage():
    """深度锚超出物理横位上限（车心离路心 >2.6 道）当垃圾：不能拿它给线
    定道号（整道错位/坏平面），本拍诚实弃权；上限内照常锚定。"""
    tr = LineTracker()
    bad = tr.update([_line(-0.2)], 1, 0.05, anchor=2.7)
    assert bad["state"] == "RELOCK" and bad["x"] is None
    assert bad["anchor_src"] == "none"
    ok = tr.update([_line(-0.2)], 2, 0.05, anchor=2.1)
    assert ok["state"] == "TRACK" and ok["anchor_src"] == "depth"
    assert ok["x"] == pytest.approx(2.2, abs=1e-6)


def test_worker_injects_off_max_into_tracker():
    w = LaneObsWorker(LaneObsConfig(enabled=True), off_max=2.6)
    assert w._tracker.OFF_MAX == pytest.approx(2.6)


def test_off_max_default_matches_policy():
    """直构缺省与 policy 的物理上限同值：防止两条尺子各自漂移。"""
    assert la.OFF_MAX == pytest.approx(
        load_decision().validate.x_lane_abs_max)


def test_anchor_sets_integer_k_and_sub_lane_x():
    """anchor=0、线在左 0.2 道 → k=0，子道修正给出 x=+0.2。"""
    tr = LineTracker()
    r = tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    assert r["state"] == "TRACK" and r["relock_src"] == "line"
    assert r["anchor_src"] == "depth"
    assert r["lines"][0]["k"] == 0
    assert r["x"] == pytest.approx(0.2, abs=1e-6)


def test_anchor_is_ego_offset_not_road_center():
    """锚点口径 = 自车横位（右正，与输出 x 同号），不是路心相对自车的位置。

    自车在路心右 0.5 道、两条线分别在其右 0.5 / 左 1.5 道：
    k=round(0.5+L) → (+1, −1)，x=k−L → +0.5。若把路心位置（−0.5）当锚，
    会得到 k=(0, −2)、x=−0.5——整张网格反向偏一道。
    """
    tr = LineTracker()
    r = tr.update([_line(0.5, "w"), _line(-1.5, "w")], 1, 0.05, anchor=0.5)
    assert r["state"] == "TRACK"
    assert [ln["k"] for ln in r["lines"]] == [1, -1]
    assert r["x"] == pytest.approx(0.5, abs=1e-6)


def test_line_lock_ignores_depth_anchor_disagreement():
    """线连续稳定 5 拍后，深度锚整道跳变不能改判 k、也不能否决线：
    输出仍是线的绝对横位（usable=True）。旧口径会沿锚点把两线同时改号，
    横位整道跳到 1.1。"""
    tr = LineTracker()
    lines = [_line(-1.1, "w"), _line(0.9, "w")]
    for fid in range(1, 6):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
        assert r["x"] == pytest.approx(0.1, abs=1e-6)
    assert r["usable"] is True and r["locked"] >= 5
    r2 = tr.update(lines, 6, 0.05, anchor=1.0)      # 深度锚整道跳
    assert r2["state"] == "TRACK"
    assert r2["x"] == pytest.approx(0.1, abs=1e-6)
    assert r2["usable"] is True
    assert [ln["k"] for ln in r2["lines"] if ln.get("accepted")] == [-1, 1]


def test_strong_lock_allows_single_line_origin():
    """强锁（曾以双侧严格条件建锁）之后，只剩一条线也继续作绝对位置：
    线已锁定几十拍时把原点让给深度，深度会差 1~2 道（实测残留整道跳）。"""
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    for fid in range(1, 8):
        tr.update(both, fid, 0.05, anchor=0.0)
    r = tr.update([_line(0.9, "w")], 8, 0.05, anchor=0.0)
    assert r["state"] == "TRACK"
    assert r["x"] == pytest.approx(0.1, abs=1e-6)
    assert r["usable"] is True


def test_coast_keeps_line_lock():
    """短暂 COAST 不清线锁：轨迹 k 还在，一两拍线不合格不该把原点交给
    深度；强锁在横位跳变/RELOCK 时才作废。"""
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    for fid in range(1, 8):
        tr.update(both, fid, 0.05, anchor=0.0)
    r = tr.update([], 8, 0.05, anchor=0.1)          # 无可用线但有深度锚
    assert r["state"] == "COAST"
    r2 = tr.update(both, 9, 0.05)                   # 线回来（深度空窗）
    assert r2["state"] == "TRACK" and r2["usable"] is True


def test_usable_requires_both_sides():
    """位置原点要求左右两侧都有高质量线；同侧两条只能给相对横位，
    usable=False（不能当绝对位置）。"""
    tr = LineTracker()
    lines = [_line(0.9, "w"), _line(2.9, "w")]
    for fid in range(1, 7):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
    assert r["state"] == "TRACK" and r["n_locked"] == 2
    assert r["usable"] is False


def test_equal_spacing_and_color_independent():
    """同样的 L 集合，颜色互换不改变 k/横位——颜色不再是身份。"""
    lines_a = [_line(-1.1, "y"), _line(0.9, "w")]
    lines_b = [_line(-1.1, "w"), _line(0.9, "y")]
    a = LineTracker().update(lines_a, 1, 0.05, anchor=0.0)
    b = LineTracker().update(lines_b, 1, 0.05, anchor=0.0)
    assert [ln["k"] for ln in a["lines"]] == [-1, 1]
    assert a["x"] == pytest.approx(0.1, abs=1e-6)
    assert b["x"] == pytest.approx(a["x"], abs=1e-6)
    assert [ln["k"] for ln in b["lines"]] == [-1, 1]


def test_same_k_duplicate_keeps_smaller_residual():
    """同一整数道号的两条线只留残差小的；等间距下不应有重复身份。"""
    tr = LineTracker()
    r = tr.update([_line(-0.1), _line(-0.3)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 1
    assert r["x"] == pytest.approx(0.1, abs=1e-6)
    assert any(ln.get("reject") == "dup" for ln in r["lines"])


def test_outlier_residual_rejected():
    """残差相对中值偏超 OUTLIER_TOL 的线不参与本拍（野线不拖横位）。"""
    tr = LineTracker()
    r = tr.update([_line(0.0), _line(1.0), _line(2.4)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 2
    assert r["x"] == pytest.approx(0.0, abs=1e-6)
    assert any(ln.get("reject") == "outlier" for ln in r["lines"])


def test_resid_over_gate_rejected():
    tr = LineTracker()
    r = tr.update([_line(0.47)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 0
    assert r["state"] == "COAST"
    assert r["lines"][0].get("reject") == "resid"


def test_anchor_jump_reanchors_after_confirm():
    """2 道级锚跳变：首拍横位跟深度兜底，连续两拍后持久道号才改。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    first = tr.update([_line(-0.2)], 2, 0.05, anchor=2.0)
    assert first["anchor_src"] == "depth"
    assert first["n_locked"] == 0
    assert first["x"] == pytest.approx(2.0, abs=1e-6)
    jump = tr.update([_line(-0.2)], 3, 0.05, anchor=2.0)
    assert jump["anchor_src"] == "depth"
    assert jump["x"] == pytest.approx(2.2, abs=1e-6)


def test_stale_k_fixed_same_frame_when_two_lines_confirm_anchor():
    """两条线同拍支持新鲜锚点、且锚点离当前横位不远：当拍纠正陈旧道号。"""
    tr = LineTracker()
    tr.update([_line(-0.25), _line(0.86)], 1, 0.05, anchor=-1.0)
    assert [ln["k"] for ln in tr.last_lines] == [-1, 0]
    tr.tracks[1].k = 1                       # 模拟空窗里被带偏的陈旧道号
    r = tr.update([_line(-0.25), _line(0.86)], 2, 0.05, anchor=-1.0)
    assert r["anchor_src"] == "depth"
    assert r["n_locked"] == 2
    assert [ln["k"] for ln in r["lines"]] == [-1, 0]
    assert r["x"] == pytest.approx(-0.805, abs=1e-3)


def test_one_lane_depth_jump_still_waits_for_confirm():
    """整道级深度跳变即使两根线都支持，也不当拍翻道（须连续确认）。"""
    tr = LineTracker()
    tr.update([_line(-0.9), _line(0.1)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.9), _line(0.1)], 2, 0.05, anchor=1.0)
    assert r["n_locked"] == 0
    assert r["x"] == pytest.approx(1.0, abs=1e-6)


def test_half_lane_noise_does_not_trigger_same_frame_reanchor():
    """0.5 道级深度噪声即使两根线都能凑上，也不当拍翻道；仍走两拍确认。"""
    tr = LineTracker()
    tr.update([_line(-0.9), _line(0.1)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.9), _line(0.1)], 2, 0.05, anchor=0.5)
    assert r["n_locked"] == 0
    assert r["x"] == pytest.approx(0.5, abs=1e-6)


def test_single_line_still_waits_for_anchor_confirm():
    """只有一条线时不享当拍重锚：单拍深度噪声不能翻道。"""
    tr = LineTracker()
    tr.update([_line(-0.9)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.9)], 2, 0.05, anchor=2.0)
    assert r["n_locked"] == 0
    assert r["x"] == pytest.approx(2.0, abs=1e-6)


def test_no_fresh_anchor_refines_against_last_x():
    """深度空窗：锚点退回上一拍横位，线只做子道细化，不自漂绝对位。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-0.3)], 2, 0.05, anchor=None)
    assert r["state"] == "TRACK"
    assert r["anchor_src"] == "lines"
    assert r["x"] == pytest.approx(0.3, abs=1e-6)


def test_yellow_wrong_fit_sign_is_rejected():
    """左黄线拟合出正 L（实拍 p2 fid 4123）不作绝对参照。"""
    tr = LineTracker()
    r = tr.update([_line(4.43, side=-1)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 0
    assert tr.side_reject == 1
    assert r["lines"][0].get("reject") == "side"


def test_yellow_unknown_side_can_still_be_used():
    """侧别未知不再一票否决：道号由深度锚定，颜色/侧别只做质量提示。"""
    tr = LineTracker()
    r = tr.update([_line(0.3, side=0)], 1, 0.05, anchor=0.0)
    assert r["n_locked"] == 1
    assert r["x"] == pytest.approx(-0.3, abs=1e-6)


def test_short_gap_coasts_then_relocks():
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    for fid in range(2, 2 + COAST_MAX_TICKS):
        r = tr.update([], fid, 0.05, anchor=None)
        assert r["state"] == "COAST"
        assert r["x"] is not None
    r = tr.update([], 2 + COAST_MAX_TICKS, 0.05, anchor=None)
    assert r["state"] == "RELOCK"


def test_blind_coast_stops_extrapolating_after_budget():
    """盲窗封顶（2026-10-31）：线不可用又无深度锚时，横位只按上次横速外推；
    超过 COAST_MAX_TICKS 就没有可积的运动证据了，位置必须冻住——旧行为会
    以恒速一路外推到整道外，随后被新鲜证据整道拽回。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    tr.x_vel = 1.0                       # 人为给一个恒速外推
    for fid in range(2, 2 + COAST_MAX_TICKS):
        tr.update([], fid, 0.05, anchor=None)
    frozen = tr.x_last
    # 检测拍继续空：位置不动（状态 RELOCK，外推已封顶）
    for fid in range(2 + COAST_MAX_TICKS, 2 + COAST_MAX_TICKS + 6):
        r = tr.update([], fid, 0.05, anchor=None)
        assert r["state"] == "RELOCK"
        assert r["lost"] >= COAST_MAX_TICKS
        assert r["x"] == pytest.approx(frozen, abs=1e-9)
    # 非检测帧同样不再推进
    r = tr.predict(0.05)
    assert r["x"] == pytest.approx(frozen, abs=1e-9)


def test_coast_anchor_accepts_line_near_prediction():
    """空窗后线在上一横位附近重新可用：按上一横位定 k，重新 TRACK。"""
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    for fid in range(2, 5):
        tr.update([], fid, 0.05, anchor=None)
    r = tr.update([_line(-1.0)], 5, 0.05, anchor=None)
    assert r["state"] == "TRACK"
    assert r["relock_src"] == "line"
    assert r["x"] == pytest.approx(0.0, abs=0.2)


def test_new_line_k_uses_all_live_lines_when_no_anchor():
    """空窗里新线由所有在期线投票定号；票一致才采用。"""
    tr = LineTracker()
    tr.update([_line(-1.9), _line(0.0), _line(1.0)], 1, 0.05, anchor=0.0)
    r = tr.update([_line(-1.9), _line(-0.9), _line(0.0), _line(1.0)],
                  2, 0.05, anchor=None)
    new = [ln for ln in r["lines"] if ln["L"] == -0.9][0]
    assert new["k"] == -1
    assert new["accepted"] is True
    assert r["n_locked"] == 4


def test_new_line_k_rejected_when_live_lines_disagree():
    """邻居自己漂了一道时票会分歧：宁可不给新线定号，也不照抄单条邻居。"""
    tr = LineTracker()
    tr.update([_line(-1.0), _line(0.0)], 1, 0.05, anchor=0.0)
    tr.update([_line(-1.0)], 2, 0.05, anchor=2.0)
    tr.update([_line(-1.0)], 3, 0.05, anchor=2.0)
    r = tr.update([_line(-0.5)], 4, 0.05, anchor=None)
    new = r["lines"][0]
    assert new["k"] is None
    assert new["accepted"] is False
    assert r["n_locked"] == 0


def _warm_lock(tr: LineTracker, lines: list[dict], n: int = 6) -> dict:
    r = {}
    for fid in range(1, n + 1):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
    assert r["state"] == "TRACK" and r["locked"] >= la.LINE_LOCK_TICKS
    return r


def test_locked_grid_anchors_new_line_to_its_x():
    """锁定期新线按网格自身横位接号，不靠邻居相对票（164953 局 p2 判据）。

    网格 x=+0.1，新线在右 2.0 道 → k=round(0.1+2.0)=2，横位保持 +0.1。
    若按邻居票/整道改判，新线会把网格拽走；这里只接号、不重定网格。
    """
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    r = _warm_lock(tr, both)
    x0 = r["x"]
    r2 = tr.update([*both, _line(2.0, "w")], 7, 0.05, anchor=None)
    new = [ln for ln in r2["lines"] if ln["L"] == 2.0][0]
    assert new["k"] == 2 and new["accepted"] is True
    assert r2["x"] == pytest.approx(x0, abs=1e-6)


def test_locked_grid_rejects_half_lane_new_line():
    """半道位置的新线不接号：x_last+L 落在整数半道，残差门拒，不硬取整。

    这是网格只能在整道身份内接新线、不能被半道拖走的边界。"""
    tr = LineTracker()
    r = _warm_lock(tr, [_line(-1.1, "w"), _line(0.9, "w")])
    r2 = tr.update([_line(-1.1, "w"), _line(0.9, "w"), _line(2.36, "w")],
                   7, 0.05, anchor=None)
    new = [ln for ln in r2["lines"] if ln["L"] == 2.36][0]
    assert new["k"] is None and new["accepted"] is False
    assert r2["x"] == pytest.approx(r["x"], abs=1e-6)


def test_locked_grid_survives_short_coast():
    """锁定后短断一拍（COAST）不解除冻结：线回来时仍按网格横位接号。"""
    tr = LineTracker()
    both = [_line(-1.1, "w"), _line(0.9, "w")]
    r = _warm_lock(tr, both)
    x0 = r["x"]
    coast = tr.update([], 7, 0.05, anchor=None)
    assert coast["state"] == "COAST" and tr.locked >= la.LINE_LOCK_TICKS
    r2 = tr.update([*both, _line(2.0, "w")], 8, 0.05, anchor=None)
    new = [ln for ln in r2["lines"] if ln["L"] == 2.0][0]
    assert new["k"] == 2 and new["accepted"] is True
    assert r2["x"] == pytest.approx(x0, abs=0.05)   # 滑行期允许小量外推
    # 冻结期新鲜深度锚不改判身份：新线仍接网格号，横位不跳
    r3 = tr.update([*both, _line(2.0, "w")], 9, 0.05, anchor=0.3)
    new = [ln for ln in r3["lines"] if ln["L"] == 2.0][0]
    assert new["k"] == 2 and new["accepted"] is True
    assert r3["x"] == pytest.approx(x0, abs=0.05)


def test_relock_without_depth_reanchors_from_last_x():
    """深度断供的长盲窗后，线靠上一横位重新接号，不再永久卡 RELOCK。"""
    tr = LineTracker()
    _warm_lock(tr, [_line(-1.1, "w"), _line(0.9, "w")])
    for fid in range(7, 7 + COAST_MAX_TICKS + 2):
        r = tr.update([], fid, 0.05, anchor=None)
    assert r["state"] == "RELOCK" and tr.locked == 0
    # 与冻结横位一致的线组 → 直接上线（k 由 x_last 取整）
    r2 = tr.update([_line(-0.2, "w"), _line(0.8, "w")], 20, 0.05,
                   anchor=None)
    assert r2["state"] == "TRACK"
    assert [ln["k"] for ln in r2["lines"] if ln["accepted"]] == [0, 1]
    assert r2["x"] == pytest.approx(0.2, abs=1e-6)
    # 半道对不上的线仍不接号（不凭空造绝对位）
    tr2 = LineTracker()
    _warm_lock(tr2, [_line(-1.1, "w"), _line(0.9, "w")])
    for fid in range(7, 7 + COAST_MAX_TICKS + 2):
        r = tr2.update([], fid, 0.05, anchor=None)
    r3 = tr2.update([_line(-0.64, "w")], 20, 0.05, anchor=None)
    assert r3["lines"][0]["k"] is None
    assert r3["n_locked"] == 0


def test_relock_allows_anchor_to_assign_k_again():
    """冻结只在锁定期：跳变/RELOCK 后新线仍由深度锚重新定号。"""
    tr = LineTracker()
    _warm_lock(tr, [_line(-1.1, "w"), _line(0.9, "w")])
    for fid in range(7, 7 + COAST_MAX_TICKS + 2):
        r = tr.update([], fid, 0.05, anchor=None)
    assert r["state"] == "RELOCK" and tr.locked == 0
    r2 = tr.update([_line(-0.2, "w"), _line(0.8, "w")], 20, 0.05,
                   anchor=0.0)
    assert [ln["k"] for ln in r2["lines"] if ln["accepted"]] == [0, 1]
    assert r2["x"] == pytest.approx(0.2, abs=1e-6)


def test_theta_weighted_by_lines():
    tr = LineTracker()
    r = tr.update([_line(-1.0, theta=0.1, n=10),
                   _line(1.0, theta=0.3, n=30)], 1, 0.05, anchor=0.0)
    assert r["theta"] == pytest.approx(0.25, abs=1e-6)


def test_near_side_uses_pixel_side_not_fit_sign():
    from maaracing_master.plugins.speedrush.lane_obs import (
        VPX, _near_x, _side_from_x)
    left = [{"yc": 300 + i, "xc": 200.0} for i in range(6)]
    right = [{"yc": 300 + i, "xc": 440.0} for i in range(6)]
    mid = [{"yc": 300, "xc": VPX / 2.0}]
    assert _side_from_x(_near_x(left)) == -1
    assert _side_from_x(_near_x(right)) == 1
    assert _side_from_x(_near_x(mid)) is None


def test_lane_cam_h_falls_back_outside_bumper_band():
    from maaracing_master.plugins.speedrush.lane_obs import (
        DEFAULT_CAM_H, _lane_cam_h)
    assert _lane_cam_h(4.97) == (DEFAULT_CAM_H, True)
    assert _lane_cam_h(3.603) == (DEFAULT_CAM_H, True)
    assert _lane_cam_h(None) == (DEFAULT_CAM_H, True)
    assert _lane_cam_h(1.40) == (1.40, False)


def test_worker_disabled_is_noop():
    w = LaneObsWorker(LaneObsConfig(enabled=False))
    w.start()
    assert w.latest() is None
    w.stop()


def test_worker_missing_weights_degrades():
    w = LaneObsWorker(LaneObsConfig(enabled=True, weights="不存在.onnx"))
    w.start()
    assert w.error is not None
    assert w.latest() is None
    w.stop()


def test_worker_missing_pack_is_optional_not_fault(monkeypatch):
    """线模型随可选模型包分发：缺包应标记为可选缺失（INFO），校验失败才算故障。"""
    rel = "resources/models/lane/plus_small_384x640.onnx"
    monkeypatch.setattr(la.model_assets, "select", lambda _rel: (None, f"缺少 {rel}"))
    w = LaneObsWorker(LaneObsConfig(enabled=True, weights=rel))
    w.start()
    assert w.optional_missing is True and w.latest() is None

    monkeypatch.setattr(la.model_assets, "select",
                        lambda _rel: (None, f"{rel} 校验失败（文件损坏）"))
    w = LaneObsWorker(LaneObsConfig(enabled=True, weights=rel))
    w.start()
    assert w.optional_missing is False and w.latest() is None


def test_dml_busy_skips_without_calling_model(monkeypatch):
    import maaracing_master.plugins.speedrush.lane_obs as la
    w = LaneObsWorker(LaneObsConfig(enabled=True, provider="dml"))
    w._use_dml = True
    w._sess = object()
    called = {"n": 0}

    class _Sess:
        def get_inputs(self):
            return [type("I", (), {"name": "input"})()]

        def run(self, *args):
            called["n"] += 1

    class _Lock:
        def acquire(self, blocking=False, timeout=None):
            return False

        def release(self):
            pass

    class _StaleBeat:
        def snapshot(self):
            return 1, -1e9          # 没有在跑的控制拍：不走槽位等待

        def wait_newer(self, seq, timeout):
            return True

    w._sess = _Sess()
    monkeypatch.setattr(la, "_letterbox", lambda rgb: np.zeros((1, 3, 384, 640),
                                                              np.float32))
    monkeypatch.setattr(la.dml_lock, "LOCK", _Lock())
    monkeypatch.setattr(la.dml_lock, "TICK_BEAT", _StaleBeat())
    assert w._run(np.zeros((8, 8, 3), np.uint8)) is None
    assert w.busy_skips == 1
    assert called["n"] == 0


def test_dml_run_marks_slot_done(monkeypatch):
    """槽位交接：线观测放锁后必须 mark_done，深度才不会白等一个槽窗。"""
    import maaracing_master.plugins.speedrush.lane_obs as la
    w = LaneObsWorker(LaneObsConfig(enabled=True, provider="dml"))
    w._use_dml = True
    called = {"n": 0}

    class _Sess:
        def get_inputs(self):
            return [type("I", (), {"name": "images"})()]

        def run(self, *args):
            called["n"] += 1
            return [None, np.zeros((1, 2, 384, 640), np.float32)]

    class _Lock:
        def acquire(self, blocking=True, timeout=None):
            return True

        def release(self):
            pass

    class _StaleBeat:
        def snapshot(self):
            return 7, -1e9          # 没有在跑的控制拍：不等槽位，直接跑

        def wait_newer(self, seq, timeout):
            return True

    w._sess = _Sess()
    monkeypatch.setattr(la, "_letterbox", lambda rgb: np.zeros((1, 3, 384, 640),
                                                              np.float32))
    monkeypatch.setattr(la.dml_lock, "LOCK", _Lock())
    monkeypatch.setattr(la.dml_lock, "TICK_BEAT", _StaleBeat())
    monkeypatch.setattr(la.dml_lock, "SLOTS", la.dml_lock._SlotGate())
    assert w._run(np.zeros((8, 8, 3), np.uint8)) is not None
    assert called["n"] == 1
    assert la.dml_lock.SLOTS.wait_done(7, 0.01) is True


def test_latest_is_one_consistent_snapshot():
    """横位、线、锚点必须同拍；不再把新预测和旧检测线拼在一起。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    now = time.perf_counter()
    snap = {"fid": 10, "cam_h": 1.4, "pred": False, "ms": 11.0,
            "n_lines": 1, "x": 0.40, "theta": 0.01, "state": "TRACK",
            "n_locked": 1, "relock_src": "line", "anchor_src": "depth",
            "grid_reject": 0, "side_reject": 0,
            "lines": [{"L": 0.2, "k": 0, "resid": 0.2, "accepted": True}],
            "t_done": now, "t_det": now, "det_fid": 10,
            "det_ts_ns": int(now * 1e9), "frame_ts_ns": int(now * 1e9)}
    w._latest = snap
    out = w.latest()
    assert out["x"] == 0.40
    assert out["lines"] == snap["lines"]
    assert out["anchor_src"] == "depth"
    assert out["pred"] is False
    assert out["det_age_ms"] is not None and out["det_age_ms"] >= 0.0


def test_latest_age_includes_capture_queue_and_inference(monkeypatch):
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    w._latest = {"fid": 10, "det_fid": 10, "frame_ts_ns": 1_000_000_000,
                 "det_ts_ns": 1_000_000_000, "t_done": 1.08, "t_det": 1.08}
    w._last_det_fid = 11  # 后台下一帧尚未发布，不得污染已发布快照。
    monkeypatch.setattr(la.time, "perf_counter", lambda: 1.12)
    first = w.latest()
    assert first["det_fid"] == 10
    assert first["det_age_ms"] == pytest.approx(120)
    assert first["age_ms"] == pytest.approx(120)
    assert first["result_age_ms"] == pytest.approx(40)
    monkeypatch.setattr(la.time, "perf_counter", lambda: 1.17)
    assert w.latest()["det_age_ms"] == pytest.approx(170)


def test_push_keeps_capture_timestamp_with_frame():
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    w._sess = object()
    w.push(np.zeros((4, 4, 3), np.uint8), 7, 1.4, 0, 0.05, False,
           frame_ts_ns=1_000_000_000, anchor=0.7, anchor_strict=True)
    item = w._q.get_nowait()
    assert item[1] == 7 and item[-2:] == (0.7, True)
    assert 1_000_000_000 in item[2:-2]


def test_row_segments_keeps_row_boundaries_and_weighted_centers():
    prob = np.zeros((360, 640), np.float32)
    prob[206, 637:] = [0.8, 0.9, 1.0]
    prob[207, :3] = [1.0, 0.9, 0.8]
    prob[207, 10:12] = [la.PROB_THRESH, 0.95]
    rows = la._row_segments(prob)
    assert [y for y, _ in rows] == [206, 207]
    assert [len(s) for _, s in rows] == [1, 2]
    assert rows[0][1][0]["xc"] == pytest.approx((637*.8+638*.9+639)/2.7, abs=1e-4)
    assert rows[1][1][0]["xc"] == pytest.approx((.9+2*.8)/2.7, abs=1e-5)
    assert rows[1][1][1]["xc"] == 11
    assert la._row_segments(prob, start=208) == []


def test_row_segments_fragmented_probability_matches_direct_weighted_sums():
    rng = np.random.default_rng(1729)
    prob = rng.random((360, 640), dtype=np.float32)
    rows = la._row_segments(prob, start=350)
    for yc, segments in rows:
        active = np.flatnonzero(prob[yc] > la.PROB_THRESH)
        groups = np.split(active, np.flatnonzero(np.diff(active) > 1)+1)
        assert len(segments) == len(groups)
        for seg, indices in zip(segments, groups):
            p = prob[yc, indices]
            assert seg["xc"] == pytest.approx(float((indices*p).sum()/p.sum()), abs=2e-4)
            assert seg["p"] == pytest.approx(float(p.mean()), abs=2e-7)


@pytest.mark.parametrize("detect_every, expected_fid", [(2, 10), (1, 11)])
def test_prediction_keeps_detection_source_but_real_empty_inference_is_new(
        monkeypatch, detect_every, expected_fid):
    w = LaneObsWorker(LaneObsConfig(enabled=True, detect_every=detect_every))
    w._sess = object()
    w._last_det_fid, w._last_det_ts_ns, w._last_det_t = 10, 1_000_000_000, 1.02
    w._tracker.x_last = 0.0
    monkeypatch.setattr(w, "_infer", lambda *a: {"lines": []})
    snapshot = w._snapshot

    def finish(*args):
        w._stop.set()
        return snapshot(*args)

    monkeypatch.setattr(w, "_snapshot", finish)
    w.push(np.zeros((4, 4, 3), np.uint8), 11, 1.28, 0, 0.05, False,
           frame_ts_ns=1_050_000_000)
    w._loop()
    result = w.latest()
    assert result["pred"] and not result["usable"]
    assert result["frame_ts_ns"] == 1_050_000_000
    assert result["det_fid"] == expected_fid
    assert result["det_ts_ns"] == (1_000_000_000 if detect_every == 2 else 1_050_000_000)


@pytest.mark.parametrize("offset, handed_off, expected", [
    (0.010, False, True), (0.035, False, False), (0.010, True, False)])
def test_dml_uses_only_current_open_slot(monkeypatch, offset, handed_off, expected):
    w = LaneObsWorker(LaneObsConfig(enabled=True, provider="dml"))
    w._use_dml = True
    calls = []

    class Session:
        def get_inputs(self):
            return [type("I", (), {"name": "images"})()]

        def run(self, *args):
            calls.append(True)
            return [None, np.zeros((1, 2, 384, 640), np.float32)]

    class Beat:
        def snapshot(self):
            return 7, 1.0

        def wait_newer(self, seq, timeout):
            pytest.fail("本拍帧不得等下一拍再推理")

    monkeypatch.setattr(la.time, "perf_counter", lambda: 1.0 + offset)
    monkeypatch.setattr(la.dml_lock, "TICK_BEAT", Beat())
    monkeypatch.setattr(la.dml_lock, "SLOTS", la.dml_lock._SlotGate())
    monkeypatch.setattr(la.dml_lock, "LOCK", __import__('threading').Lock())
    monkeypatch.setattr(la, "_letterbox", lambda rgb: None)
    if handed_off:
        la.dml_lock.SLOTS.mark_done(7)
    w._sess = Session()
    result = w._run(np.zeros((4, 4, 3), np.uint8))
    assert bool(calls) is expected
    assert (result is not None) is expected
    assert la.dml_lock.SLOTS.wait_done(7, 0) is True


def test_dml_rechecks_slot_after_lock_acquisition(monkeypatch):
    w = LaneObsWorker(LaneObsConfig(enabled=True, provider="dml"))
    w._use_dml = True
    clock = [1.029]

    class Session:
        def get_inputs(self):
            return [type("I", (), {"name": "images"})()]

        def run(self, *args):
            pytest.fail("过窗后不得启动推理")

    class Lock:
        def acquire(self, *args, **kwargs):
            clock[0] = 1.048
            return True

        def release(self):
            pass

    class Beat:
        def snapshot(self):
            return 7, 1.0

        def wait_newer(self, seq, timeout):
            return True

    monkeypatch.setattr(la.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(la.dml_lock, "TICK_BEAT", Beat())
    monkeypatch.setattr(la.dml_lock, "LOCK", Lock())
    monkeypatch.setattr(la.dml_lock, "SLOTS", la.dml_lock._SlotGate())
    monkeypatch.setattr(la, "_letterbox", lambda rgb: None)
    w._sess = Session()
    assert w._run(np.zeros((4, 4, 3), np.uint8)) is None
    assert w.slot_skips == 1


def test_worker_snapshot_carries_line_origin_flags():
    """线程快照必须搬运 usable/locked：漏搬时消费方永远读 None，线为主
    静默失效——2026-10-31 实机一局整局没生效的根因就是这里漏了两个键。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True))
    res = {"state": "TRACK", "x": 0.4, "theta": 0.01, "n_locked": 2,
           "relock_src": "line", "n_lines": 2, "grid_reject": 0,
           "side_reject": 0, "anchor_src": "lines",
           "usable": True, "locked": 7, "lines": [], "pred": False}
    w._focal = 671.3
    snap = w._snapshot(res, 10, 1.28, 0.0, 0.02, False)
    assert snap["usable"] is True and snap["locked"] == 7
    assert snap["focal"] == pytest.approx(671.3)     # 线角尺度可审计


def test_predict_marks_usable_false():
    """滑行预测不是测量：可作位置原点的标志必须为 False，位置交给执行层
    的短断供预算，而不是把外推值冒充新线。"""
    tr = LineTracker()
    lines = [_line(-1.1, "w"), _line(0.9, "w")]
    r = None
    for fid in range(1, 7):
        r = tr.update(lines, fid, 0.05, anchor=0.0)
    assert r["usable"] is True
    p = tr.predict(0.05, 0.0)
    assert p["usable"] is False and p["pred"] is True


def test_push_without_throttle_queues_every_tick():
    """hz=0：每拍都推，由队列深度 1 丢旧帧限流。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True, hz=0.0))
    w._sess = object()
    rgb = np.zeros((4, 4, 3), np.uint8)
    for fid in range(3):
        w.push(rgb, fid, 1.4, 0.0, 0.05, False)
    assert w.push_n == 3
    assert w.drop_n == 2


def test_push_plumbs_same_frame_anchor():
    """锚点跟帧入队：worker 拿到的深度锚点与这条帧同拍；严格标记同帧搬运。"""
    w = LaneObsWorker(LaneObsConfig(enabled=True, hz=0.0))
    w._sess = object()
    rgb = np.zeros((4, 4, 3), np.uint8)
    w.push(rgb, 7, 1.4, 0.0, 0.05, False, anchor=0.7)
    item = w._q.get_nowait()
    assert item[-2] == pytest.approx(0.7)
    assert item[-1] is False
    assert item[1] == 7
    w.push(rgb, 8, 1.4, 0.0, 0.05, False, anchor=0.7, anchor_strict=True)
    assert w._q.get_nowait()[-1] is True


def test_push_throttle_honored_when_configured():
    w = LaneObsWorker(LaneObsConfig(enabled=True, hz=10.0))
    w._sess = object()
    w._last_t = time.perf_counter()
    w.push(np.zeros((4, 4, 3), np.uint8), 1, 1.4, 0.0, 0.05, False)
    assert w.push_n == 0


def test_predict_advances_x_without_new_identity():
    tr = LineTracker()
    tr.update([_line(-0.2)], 1, 0.05, anchor=0.0)
    x0 = tr.x_last
    r = tr.predict(0.1, v_lat=0.0)
    assert r["state"] == "COAST"
    assert r["x"] == pytest.approx(x0, abs=1e-9)
    assert r["n_locked"] == 0
    assert r["pred"] is True


def test_vanish_gate_keeps_snapshot_updating(monkeypatch):
    """灭点质量门曾按 rec["n_lines"] 取键，而 _infer 只返回 lines：灭点历史
    一旦非空，每帧都在这里抛 KeyError（循环吞掉异常、快照不更新），线观测
    掉到 2.1Hz、检测龄涨到秒级——2026-11-13 实机一局的现场。"""
    import threading
    w = LaneObsWorker(LaneObsConfig(enabled=True, provider="cpu"))
    w._sess = object()
    w._yv_hist.append(200.0)      # 已有灭点历史：质量门这条路必然走到
    monkeypatch.setattr(la, "_vanish_row", lambda lines: 200.0)
    monkeypatch.setattr(w, "_infer",
                        lambda rgb, cam_h, focal: {"lines": [_line(0.5)]})
    t = threading.Thread(target=w._loop, daemon=True)
    t.start()
    try:
        w.push(np.zeros((16, 16, 3), np.uint8), 1, 1.28, 0.0, 0.05, False)
        deadline = time.time() + 2.0
        while w.latest() is None and time.time() < deadline:
            time.sleep(0.01)
        assert w.error is None
        snap = w.latest()
        assert snap is not None and snap["fid"] == 1
    finally:
        w._stop.set()
        t.join(timeout=2.0)


def test_beat_counts_published_results_only(monkeypatch):
    """面板的「车道观测 频率」= 有效输出率：跳过或出错的拍不记心跳，否则
    线程空转也会显示 20Hz（2026-11-13 面板与实况对不上的根因）。"""
    import threading
    w = LaneObsWorker(LaneObsConfig(enabled=True, provider="cpu"))
    w._sess = object()
    beats = []
    monkeypatch.setattr(la, "beat", lambda: beats.append(1))
    mode = {"ret": None}
    monkeypatch.setattr(w, "_infer", lambda rgb, cam_h, focal: mode["ret"])
    t = threading.Thread(target=w._loop, daemon=True)
    t.start()
    try:
        w.push(np.zeros((16, 16, 3), np.uint8), 1, 1.28, 0.0, 0.05, False)
        time.sleep(0.2)
        assert beats == []            # 没产出结果：不打心跳
        mode["ret"] = {"lines": [_line(0.5)]}
        w.push(np.zeros((16, 16, 3), np.uint8), 2, 1.28, 0.0, 0.05, False)
        deadline = time.time() + 2.0
        while not beats and time.time() < deadline:
            time.sleep(0.01)
        assert len(beats) == 1
    finally:
        w._stop.set()
        t.join(timeout=2.0)


def _dash_prob(s: float, a: float) -> np.ndarray:
    """概率图上画一条远场虚线：dash 中心落在全幅直线 x=s·y+a 上（640 尺度落图）。"""
    prob = np.zeros((360, 640), np.float32)
    for y640 in range(220, 272, 4):
        x640 = int(round((s * (2.0 * y640) + a) / 2.0))
        prob[y640:y640 + 2, max(0, x640 - 6):x640 + 6] = 1.0
    return prob


def test_row_segments_step_one_hits_thin_dash_rows():
    """逐行扫描：高 2 行的远场虚线两条行都命中（原步长 3 只有 1~2 行，虚线
    轨迹凑不满拟合点数——2026-11-11 分解诊断的主死因）。"""
    prob = np.zeros((360, 640), np.float32)
    prob[230:232, 300:340] = 1.0
    rows = la._row_segments(prob)
    assert len(rows) == 2


def test_lines_from_prob_captures_far_dash_without_color():
    """虚线组能成线，且颜色不参与：L 正确、字段恒 '?'、无 yellow/white 键。
    锁 2026-11-11 的提取改动（虚线与实线同待遇、无颜色匹配）。"""
    s = la.LANE_W / la.DEFAULT_CAM_H          # L=1 道对应的全幅斜率
    a = 600.0 - s * 500.0                     # y=500 处 x=600（远带中部）
    lines = la._lines_from_prob(_dash_prob(s, a), la.DEFAULT_CAM_H, 807.7)
    hit = [ln for ln in lines if abs(ln["L"] - 1.0) <= 0.15]
    assert hit, f"虚线组未成线或 L 偏了：{[ln['L'] for ln in lines]}"
    ln = hit[0]
    assert ln["rms"] <= la.LINE_RMS_MAX
    assert ln["color"] == "?"
    assert "yellow" not in ln and "white" not in ln
    assert ln["y0"] >= 440.0 and ln["y1"] <= 546.0


# ---------- 地平线发布口径（2026-11-12）：中位平滑 + 相机高反解 ----------

def test_smooth_vanish_rejects_single_outlier():
    """单帧离群被中位吸收：坏拍（假灭点）不得直接进合成点图的尺度基准。"""
    from collections import deque
    hist = deque(maxlen=la.YV_WIN)
    val = None
    t = 0.0
    refill = False
    for i, v in enumerate((361.0, 361.2, 360.8, 361.1, 361.0)):
        val, t, refill = la._smooth_vanish(hist, v, i * 0.05, t, val, refill)
    assert val == pytest.approx(361.0, abs=0.2)
    val, t, refill = la._smooth_vanish(hist, 372.0, 0.25, t, val, refill)
    assert abs(val - 361.0) < 1.0, f"离群没有被吸收：{val}"


def test_smooth_vanish_gap_holds_prev_until_window_refills():
    """断检超过 YV_GAP_S 后第一帧可能是假灭点：先保留旧发布值，凑满窗口再采纳中位。"""
    from collections import deque
    hist = deque(maxlen=la.YV_WIN)
    val = None
    t = 0.0
    refill = False
    for i in range(5):
        val, t, refill = la._smooth_vanish(hist, 361.0, i * 0.05, t, val, refill)
    assert val == pytest.approx(361.0)
    val, t, refill = la._smooth_vanish(hist, 378.8, t + 2.2, t, val, refill)
    assert val == pytest.approx(361.0), "断检后的第一帧不得直接进发布值"
    assert refill is True
    mine = (375.2, 362.4, 358.9, 350.4)
    for i, v in enumerate(mine):
        val, t, refill = la._smooth_vanish(hist, v, t + 0.05, t, val, refill)
        if i < 3:
            assert val == pytest.approx(361.0)
    assert val == pytest.approx(362.4), "第五个新样本应发布窗口中位，且不受离群拖带"
    assert refill is False


def test_smooth_vanish_slews_short_cluster():
    """3~4 拍的成簇离群仍被压住：整窗还留着旧样本，不得触发持续偏离出口。"""
    from collections import deque
    hist = deque(maxlen=la.YV_WIN)
    val = None
    t = 0.0
    refill = False
    for i in range(5):
        val, t, refill = la._smooth_vanish(hist, 361.0, i * 0.05, t, val, refill)
    for i in range(3):
        val, t, refill = la._smooth_vanish(hist, 371.0, (5 + i) * 0.05, t, val, refill)
    assert abs(val - 361.0) < 0.5, f"3 拍离群不得整段拉动发布值：{val}"
    for i in range(5):
        val, t, refill = la._smooth_vanish(hist, 361.0, (8 + i) * 0.05, t, val, refill)
    assert abs(val - 361.0) < 0.5


def test_smooth_vanish_follows_sustained_step():
    """整窗 5 拍同侧偏离超过阈值：直接跟到中位，不被 0.1px/拍拖住。"""
    from collections import deque
    hist = deque(maxlen=la.YV_WIN)
    val = None
    t = 0.0
    refill = False
    for i in range(5):
        val, t, refill = la._smooth_vanish(hist, 361.0, i * 0.05, t, val, refill)
    for i in range(5):
        val, t, refill = la._smooth_vanish(hist, 371.0, (5 + i) * 0.05, t, val, refill)
    assert val == pytest.approx(371.0), f"持续偏离没有跟上：{val}"


def test_smooth_vanish_slew_holds_subthreshold_drift():
    """低于出口阈值的台阶仍只按 YV_SLEW_PX/拍走，不整段跟随。"""
    from collections import deque
    hist = deque(maxlen=la.YV_WIN)
    val = None
    t = 0.0
    refill = False
    for i in range(5):
        val, t, refill = la._smooth_vanish(hist, 361.0, i * 0.05, t, val, refill)
    for i in range(5):
        val, t, refill = la._smooth_vanish(hist, 363.0, (5 + i) * 0.05, t, val, refill)
    assert 361.0 < val < 363.0
    assert val == pytest.approx(361.0 + 3 * la.YV_SLEW_PX, abs=0.02)


def test_fit_theta_uses_live_horizon_not_moge_constant():
    """线角的地平线用当拍发布值：固定常数会让线角系统性偏（约 0.9~2°）。"""
    pts = [{"yc": y / 2.0, "xc": (0.3 * y + 100.0) / 2.0, "p": 1.0}
           for y in range(560, 701, 10)]
    t_old = la._fit(pts, 1.28, *la.NEAR_Y, 807.7)["theta"]
    t_new = la._fit(pts, 1.28, *la.NEAR_Y, 807.7, 361.0)["theta"]
    assert t_old is not None and t_new is not None
    assert t_new < t_old, "地平线更高 → 同一线角更小"
    assert abs(t_new - t_old) > 1e-4, "双源差必须真的进入线角"


def _vline(k, cam_h=1.25, y_v=361.0, y0=560.0, y1=700.0):
    """按地面线方程造一条已定道号、已接受的线（x = vpx + L·LANE_W·(y−y_v)/cam_h）。"""
    s = k * la.LANE_W / cam_h
    a = la.VPX - s * y_v
    return {"k": k, "accepted": True,
            "fits": [{"s": s, "a": a, "y0": y0, "y1": y1}]}


def test_lane_cam_h_from_pixel_spacing():
    """两条相邻线的像素间距能独立反解相机高（不经过任何深度读数）。"""
    got = la._cam_h_from_lines([_vline(-1), _vline(1)], 361.0)
    assert got == pytest.approx(1.25, rel=0.01)


def test_lane_cam_h_needs_two_distinct_lanes():
    assert la._cam_h_from_lines([_vline(-1)], 361.0) is None
    assert la._cam_h_from_lines([_vline(1), _vline(1)], 361.0) is None  # 同道
    assert la._cam_h_from_lines([_vline(-1), _vline(1)], None) is None
    bad = dict(_vline(1), accepted=False)
    assert la._cam_h_from_lines([_vline(-1), bad], 361.0) is None


def test_lane_cam_h_separates_chase_view():
    """追尾机位（约 1.9m）必须落在守卫带 1.12~1.55 之外——这是守卫恢复判据的前提。"""
    got = la._cam_h_from_lines([_vline(-1, cam_h=1.9), _vline(1, cam_h=1.9)],
                               361.0)
    assert got is not None and got > 1.55


def test_lane_cam_h_insensitive_to_horizon_offset():
    """y_v 有 4px 偏差时反解只差 2%（守卫是粗门，不吃地平线的细噪声）。"""
    got = la._cam_h_from_lines([_vline(-1), _vline(1)], 365.0)
    assert got == pytest.approx(1.25, rel=0.05)


# ---------- 线几何的相机高输入（2026-11-12）：换源后的合法机位带 ----------

def test_lane_cam_h_accepts_chase_view():
    """线几何要能按真实机位换算：追尾 1.9m 不再被砍回标定值 1.28。

    旧带 1.12~1.55 是「深度平面坏帧」的 v4 防护，换源后它反而会制造静默
    错。视角判定门（view_guard）仍只认保险杠，两者语义不同。"""
    assert la._lane_cam_h(1.9) == (1.9, False)
    assert la._lane_cam_h(1.269) == (1.269, False)


def test_lane_cam_h_still_rejects_far_outliers():
    """带放宽不等于不设防：实机反解会飘到 0.73 / 2.93，越界仍回退。
    界内的单点噪声由 module 侧 5 拍中位先滤一层。"""
    assert la._lane_cam_h(0.73)[1] is True
    assert la._lane_cam_h(2.93)[1] is True
    assert la._lane_cam_h(4.97)[1] is True
    assert la._lane_cam_h(None)[1] is True
    assert la._lane_cam_h(0.73)[0] == la.DEFAULT_CAM_H


# ---------- HUD 距离徽章假线剔除（2026-11-12）----------

def _fit_line(s, a, y0, y1):
    """构造 _lines_from_prob 口径的线（fits 用全幅像素 x = s·y + a）。"""
    return {"fits": [{"s": s, "a": a, "y0": y0, "y1": y1}]}


# 徽章框：徽章中心 x≈618、纵向 592~685（含红环、米字与下方箭头）
BADGE_BOX = (590.0, 592.0, 646.0, 685.0)


def test_badge_only_line_is_dropped():
    """只长在徽章上的竖线（x 恒定在框内、纵向不出框）判为假线。"""
    line = _fit_line(0.0, 620.0, 592.0, 640.0)
    assert la._line_in_badge(line, BADGE_BOX) is True


def test_line_passing_beside_badge_is_kept():
    """贴脸真线纵向跨 520~700、远超徽章高度，比例上不去，必须保留。"""
    real = _fit_line(0.35, 370.0, 520.0, 700.0)
    assert la._line_in_badge(real, BADGE_BOX) is False


def test_line_outside_badge_x_is_kept():
    """横向不在徽章范围内的线（左黄线/右侧虚线）与徽章无关。"""
    left = _fit_line(0.05, 60.0, 520.0, 700.0)
    assert la._line_in_badge(left, BADGE_BOX) is False


def test_drop_badge_lines_uses_frame_box(monkeypatch):
    """_drop_badge_lines 用当帧检测到的框：徽章线被剔、真线保留。"""
    badge = _fit_line(0.0, 620.0, 592.0, 640.0)
    real = _fit_line(0.35, 370.0, 520.0, 700.0)
    monkeypatch.setattr(la, "_hud_badge_box", lambda frame: BADGE_BOX)
    out = la._drop_badge_lines([badge, real], np.zeros((720, 1280, 3), np.uint8))
    assert out == [real]


def test_drop_badge_lines_noop_without_badge(monkeypatch):
    """本帧没有徽章（或模板缺失）时行为与旧版一致：一条都不动。"""
    badge = _fit_line(0.0, 620.0, 592.0, 640.0)
    monkeypatch.setattr(la, "_hud_badge_box", lambda frame: None)
    assert la._drop_badge_lines([badge], np.zeros((720, 1280, 3), np.uint8)) == [badge]


def test_badge_gate_can_be_disabled(monkeypatch):
    """HUD_BADGE_GATE=False 时整道门回退（A/B 隔离）。"""
    badge = _fit_line(0.0, 620.0, 592.0, 640.0)
    monkeypatch.setattr(la, "HUD_BADGE_GATE", False)
    monkeypatch.setattr(la, "_hud_badge_box", lambda frame: BADGE_BOX)
    assert la._drop_badge_lines([badge], np.zeros((720, 1280, 3), np.uint8)) == [badge]


def test_gross_anchor_disagreement_shifts_grid_by_one_lane():
    """整道级复核：锁定后深度连续差超一整道 → 挪一次道号，锁定不重置。

    半道级分歧是常态噪声（路心相位 p50 0.15 道），拿它复核会把网格摇散；
    只有整道级、且最近 5 个新鲜锚里至少 3 个如此，才认一次。
    """
    tr = la.LineTracker()
    lines = [_line(-0.4, side=-1), _line(0.6, side=1)]
    for i in range(8):                      # 先锁住：x 稳定在 0.4
        out = tr.update(lines, i + 1, 0.05, 0.0, False, 0.0)
    assert out["x"] == pytest.approx(0.4)
    assert tr.locked >= la.LINE_LOCK_TICKS
    # 深度连续 5 拍都说车在 1.5 道（差 1.1 道 = 整道级），且锚严格（同源双侧稳定）
    for i in range(5):
        out = tr.update(lines, 20 + i, 0.05, 0.0, False, 1.5, True)
    assert tr.gross_reanchor == 1
    # 只挪整数道：小数部分仍由线自己给（0.4），所以落在 1.4 而不是锚点 1.5
    assert out["x"] == pytest.approx(1.4)
    assert abs(out["x"] - 1.5) < 0.5
    # 锁定没被重置：复核是修正，不是重来
    assert tr.locked >= la.LINE_LOCK_TICKS


def test_half_lane_anchor_disagreement_never_reanchors():
    """半道级分歧（0.6 道）不触发复核——那是路心噪声，不是整道错判。"""
    tr = la.LineTracker()
    lines = [_line(-0.4, side=-1), _line(0.6, side=1)]
    for i in range(8):
        tr.update(lines, i + 1, 0.05, 0.0, False, 0.0)
    for i in range(40):
        out = tr.update(lines, 20 + i, 0.05, 0.0, False, 1.0, True)
    assert tr.gross_reanchor == 0
    assert out["x"] == pytest.approx(0.4, abs=0.05)


def test_gross_reanchor_requires_strict_two_sided_anchor():
    """整道复核只认"同时稳定看清两侧"的锚：单侧/换源/中心逐拍跳的锚
    即使连续差满一道也不许平移网格（2026-11-13 三局：贴左墙段绿区中心
    逐拍跳 0.4 道，旧口径 3/5 拍整道平移、错误保持数十秒）。"""
    tr = la.LineTracker()
    lines = [_line(-0.4, side=-1), _line(0.6, side=1)]
    for i in range(8):
        tr.update(lines, i + 1, 0.05, 0.0, False, 0.0, True)
    # 同一远锚，但明确标记为非严格（单侧/不稳定）
    for i in range(10):
        out = tr.update(lines, 20 + i, 0.05, 0.0, False, 1.5, False)
    assert tr.gross_reanchor == 0
    assert out["x"] == pytest.approx(0.4, abs=0.05)
    # 非严格锚不计数；切回严格后仍按 3/5 拍从头累积
    for i in range(2):
        tr.update(lines, 40 + i, 0.05, 0.0, False, 1.5, True)
    assert tr.gross_reanchor == 0
    for i in range(3):
        out = tr.update(lines, 50 + i, 0.05, 0.0, False, 1.5, True)
    assert tr.gross_reanchor == 1
