#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏内圆盘光标纯视觉识别器（与虚拟手柄/点击关键路径完全解耦）。

职责：
  - 签名剖面法识别游戏内白色圆盘光标（normal / interactive 两态）。
  - 时间连续性筛选（select_cursor）。
  - 无任何手柄驱动或输入注入依赖，可供诊断叠加层或上层模块独立调用。
"""
from __future__ import annotations

import math

import cv2
import numpy as np

# ======================================================================
#  光标三态签名（实测色值，容差见 *_TOL）
# ======================================================================
GRAY_DIFF = 18            # 三通道两两最大差 <= 此值 -> 中性灰像素（掩码层，宽松）
SAT_THRES = 40            # 饱和度过高的彩色排除
PURE_GRAY_TOL = 10        # 射线解析层纯灰门槛

# 三态签名（BGR 顺序读取；剖面在 RGB 空间比对则转换）。pressed 态不识别
# （按下由程序自身触发，程序知道按下时机）。
STATE_SIGNATURES = {
    "normal": {"center": (255, 255, 255), "radius": (6, 13), "ring": (133, 133, 133), "ring_thick": (2, 7)},
    "interactive": {"center": (192, 192, 192), "radius": (6, 12), "ring": (250, 250, 250), "ring_thick": (2, 6)},
}
CENTER_TOL = 30      # 内盘色容差（每通道）
RING_TOL = 30        # 环色容差（每通道）
THRESH_SCORE = 0.60  # 归一化匹配分下限，低于此视为非光标

# 种子定位参数
SEED_CORE_TOL = 25
SEED_RING_TOL = 20
CORE_AREA = (80, 900)
RING_AREA = (120, 900)
CORE_MIN_CIRC = 0.45
RING_MIN_CIRC = 0.25
SEED_MERGE_DIST = 16

# 时间连续性先验
JUMP_DIST = 80.0
JUMP_MIN_SCORE = 0.85
NEAR_SCORE_REL = 0.15
MISS_STREAK_RESET = 15


def _is_pure_gray(b, g, r):
    return (abs(b - g) <= PURE_GRAY_TOL and abs(g - r) <= PURE_GRAY_TOL
            and abs(b - r) <= PURE_GRAY_TOL)


def _color_dist(c1, c2):
    return math.sqrt((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2 + (c1[2] - c2[2]) ** 2)


def radial_profile(frame, cx, cy, max_r=32):
    """沿四个对角方向从质心逐像素采样色值序列（遇到越界立即截断）。"""
    h, w = frame.shape[:2]
    dirs = [(-1, -1), (1, -1), (-1, 1), (1, 1)]
    prof = []
    for dx, dy in dirs:
        seq = []
        for k in range(0, max_r + 1):
            x = int(round(cx + dx * k))
            y = int(round(cy + dy * k))
            if not (0 <= x < w and 0 <= y < h):
                break
            b, g, r = int(frame[y, x, 0]), int(frame[y, x, 1]), int(frame[y, x, 2])
            seq.append((b, g, r))
        if len(seq) >= 4:
            prof.append(seq)
    return prof


def _ray_segments(seq, max_tol=40):
    if not seq:
        return []
    segments = []
    cur_start = 0
    for i in range(1, len(seq)):
        if _color_dist(seq[i], seq[i - 1]) > max_tol:
            segments.append((cur_start, i))
            cur_start = i
    segments.append((cur_start, len(seq)))
    out = []
    for s, e in segments:
        if e - s >= 2:
            seg = seq[s:e]
            avg = tuple(int(round(sum(c[k] for c in seg) / len(seg))) for k in range(3))
            out.append((avg, s, e))
    return out


def _ray_parse(seq):
    segs = _ray_segments(seq)
    if not segs:
        return None, None, None, 0
    center_color, s0, e0 = segs[0]
    if not _is_pure_gray(*center_color):
        return center_color, e0, None, 0
    center_r = e0
    for avg, s, e in segs[1:]:
        if _is_pure_gray(*avg) and _color_dist(avg, center_color) > CENTER_TOL * 0.8:
            return center_color, center_r, avg, e - s
    return center_color, center_r, None, 0


def _color_mask(frame, ref, tol):
    ref = np.array(ref, dtype=np.int16)
    diff = frame.astype(np.int16) - ref
    dist2 = (diff * diff).sum(axis=2)
    return (dist2 <= tol * tol).astype(np.uint8) * 255


def _seeds_from_mask(mask, area_lo, area_hi, min_circ):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    seeds = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < area_lo or area > area_hi:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter < 1e-6:
            continue
        circularity = 4 * math.pi * area / (perimeter * perimeter)
        if circularity < min_circ:
            continue
        x, y, cw, ch = cv2.boundingRect(cnt)
        seeds.append((x + cw // 2, y + ch // 2, area, circularity))
    return seeds


def _dedup_seeds(all_seeds):
    all_seeds = sorted(all_seeds, key=lambda s: s[2], reverse=True)
    kept = []
    for s in all_seeds:
        if all((s[0] - k[0]) ** 2 + (s[1] - k[1]) ** 2 > SEED_MERGE_DIST ** 2
               for k in kept):
            kept.append(s)
    return kept


def build_seeds(frame_rgb):
    R, G, B = (frame_rgb[:, :, i].astype(np.int16) for i in range(3))
    rgb = np.stack([R, G, B], axis=2)
    seeds = []
    seeds += _seeds_from_mask(_color_mask(rgb, STATE_SIGNATURES["interactive"]["center"],
                                          SEED_CORE_TOL),
                              *CORE_AREA, CORE_MIN_CIRC)
    seeds += _seeds_from_mask(_color_mask(rgb, STATE_SIGNATURES["normal"]["ring"],
                                          SEED_RING_TOL),
                              *RING_AREA, RING_MIN_CIRC)
    max_diff = np.maximum(np.maximum(np.abs(R - G), np.abs(G - B)), np.abs(B - R))
    gray_mask = (max_diff <= GRAY_DIFF).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(gray_mask, cv2.MORPH_OPEN, kernel, iterations=1)
    seeds += _seeds_from_mask(mask, 120, 2600, 0.55)
    return _dedup_seeds(seeds)


class CursorCandidate:
    __slots__ = ("pos", "area", "radius_est", "circularity", "aspect",
                 "score", "state", "center_color", "ring_color", "ring_thick")

    def __init__(self):
        self.pos = (0, 0)
        self.area = 0.0
        self.radius_est = 0.0
        self.circularity = 0.0
        self.aspect = 1.0
        self.score = 0.0
        self.state = "reject"
        self.center_color: tuple | None = None
        self.ring_color: tuple | None = None
        self.ring_thick = 0


def _score_against_signature(cand, sig):
    if cand.center_color is None:
        return 0.0
    center = cand.center_color
    s_center = sig["center"]
    center_err = _color_dist(center, s_center)
    center_score = max(0.0, 1.0 - center_err / (CENTER_TOL * 3.0))
    r_est = cand.radius_est
    r_lo, r_hi = sig["radius"]
    if r_lo <= r_est <= r_hi:
        r_score = 1.0
    else:
        r_score = max(0.0, 1.0 - abs(r_est - (r_lo + r_hi) / 2) / max(1, (r_hi - r_lo) or 1))
    if sig["ring"] is None:
        if cand.ring_color is None or cand.ring_thick < 1:
            ring_score = 1.0
        else:
            ring_score = max(0.0, 1.0 - cand.ring_thick / 6.0)
    else:
        if cand.ring_color is None:
            ring_score = 0.2
        else:
            ring_err = _color_dist(cand.ring_color, sig["ring"])
            ring_score = max(0.0, 1.0 - ring_err / (RING_TOL * 3.0))
            if sig["ring_thick"]:
                thick_lo, thick_hi = sig["ring_thick"]
                if not (thick_lo <= cand.ring_thick <= thick_hi):
                    ring_score *= max(0.3, 1.0 - abs(cand.ring_thick - (thick_lo + thick_hi) / 2) / 6.0)
    shape_score = min(1.0, cand.circularity / 0.9)
    score = center_score * 0.45 + r_score * 0.15 + ring_score * 0.30 + shape_score * 0.10
    return float(max(0.0, min(1.0, score)))


def detect_cursor(frame_rgb):
    """识别光标，返回 (列表[CursorCandidate]，选中或None)。"""
    frame = frame_rgb
    seeds = build_seeds(frame)
    if not seeds:
        return [], None
    targets = []
    for sx, sy, s_area, s_circ in seeds:
        c = CursorCandidate()
        c.pos = (sx, sy)
        c.area = float(s_area)
        c.radius_est = math.sqrt(s_area / math.pi)
        c.circularity = s_circ
        prof = radial_profile(frame, sx, sy)
        parsed = [_ray_parse(s) for s in prof]
        valid = [p for p in parsed if p[0] is not None and _is_pure_gray(*p[0])]
        if not valid:
            c.center_color = None
            c.score = 0.0
            c.state = "reject"
            targets.append(c)
            continue
        centers = [p[0] for p in valid]
        c.center_color = tuple(int(round(sum(x[k] for x in centers) / len(centers))) for k in range(3))
        radii = sorted(p[1] for p in valid)
        c.radius_est = radii[len(radii) // 2] if len(radii) else 0
        ring_rays = [p for p in valid if p[2] is not None]
        if ring_rays:
            ring_colors = [p[2] for p in ring_rays]
            c.ring_color = tuple(int(round(sum(x[k] for x in ring_colors) / len(ring_colors))) for k in range(3))
            c.ring_thick = sorted(p[3] for p in ring_rays)[len(ring_rays) // 2]
        else:
            c.ring_color, c.ring_thick = None, 0
        best_state = "reject"
        best_score = 0.0
        for name, sig in STATE_SIGNATURES.items():
            sc = _score_against_signature(c, sig)
            if sc > best_score:
                best_state = name
                best_score = sc
        c.state = "interactive" if best_state == "interactive" else best_state
        c.score = best_score
        targets.append(c)
    targets.sort(key=lambda c: c.score, reverse=True)
    sel = select_cursor(targets, None, 0)
    return targets, sel


def select_cursor(targets, last_pos, miss_streak):
    if not targets:
        return None
    top = targets[0]
    if top.score < THRESH_SCORE:
        return None
    if last_pos is None or miss_streak >= MISS_STREAK_RESET:
        return top

    def dist(c):
        return math.hypot(c.pos[0] - last_pos[0], c.pos[1] - last_pos[1])

    near = [c for c in targets if c.score >= THRESH_SCORE and dist(c) <= JUMP_DIST]
    if near:
        best_near = max(near, key=lambda c: c.score)
        if top.score - best_near.score <= NEAR_SCORE_REL:
            return best_near
        return top
    return top if top.score >= JUMP_MIN_SCORE else None
