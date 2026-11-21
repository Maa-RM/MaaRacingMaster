# -*- coding: utf-8 -*-
"""在保险杠画面识别成段的黄实线路缘。输入与线模型共用全幅 RGB 坐标。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from maaracing_master.plugins.infinite_drive.lane_obs import LANE_W, VPX, _line_fit_at, _local_line_good

ROAD_HALF_LANES = 2
# 颜色探针的搜索起点和收敛门共用这个标定行。它不承担投影地平线的职责。
_SCAN_Y = 370
_BAND = 15
_VP_TOL = 130


@dataclass(frozen=True)
class YellowEdge:
    slope: float
    intercept: float
    y0: float
    y1: float
    dim: bool = False


def _clusters(band: np.ndarray) -> list[tuple[int, int]]:
    xs = np.flatnonzero(band.any(axis=0))
    return [(int(group[0]), int(group[-1])) for group in np.split(xs, np.flatnonzero(np.diff(xs) > 35) + 1)
            if group.size and group[-1] - group[0] >= 2]


def _tracks(mask: np.ndarray) -> list[list[tuple[int, int, int]]]:
    tracks: list[list[tuple[int, int, int]]] = []
    for y in range(10, mask.shape[0] - 5, _BAND):
        clusters = _clusters(mask[y:y + _BAND])
        used = set()
        for track in tracks:
            prev_y, lo, hi = track[-1]
            predicted = (lo + hi) / 2
            if len(track) > 1:
                earlier_y, earlier_lo, earlier_hi = track[-2]
                step = predicted - (earlier_lo + earlier_hi) / 2
                predicted += step * (y - prev_y) / (prev_y - earlier_y)
            best = min((i for i in range(len(clusters)) if i not in used),
                       key=lambda i: abs(sum(clusters[i]) / 2 - predicted), default=None)
            if best is not None and abs(sum(clusters[best]) / 2 - predicted) < 90:
                used.add(best)
                track.append((y, *clusters[best]))
        tracks.extend([(y, *cluster)] for i, cluster in enumerate(clusters) if i not in used)
    return tracks


def _solid(band: np.ndarray) -> bool:
    runs = []
    padded = np.pad(band, ((0, 0), (1, 1)))
    for row in padded:
        ends = np.flatnonzero(row[1:] != row[:-1])
        if ends.size:
            runs.append(np.max(ends[1::2] - ends[::2]))
    return len(runs) >= 0.55 * band.shape[0] and bool(np.median(runs) >= 4)


def detect_yellow_edges(rgb: np.ndarray) -> list[YellowEdge]:
    """粗掩码跨行成轨后，按线形、实心度和整段颜色证据验收。"""
    if rgb.shape[:2] != (720, 1280):
        return []
    road = rgb[_SCAN_Y:]
    r, g, b = cv2.split(road)
    rb, gb = cv2.subtract(r, b), cv2.subtract(g, b)
    mask = (rb > 110) & (gb > 55) & (r > 160) & (g > 140)
    edges = _fit_edges(road, mask, dim=False)
    if not edges:
        # 雨天路缘会一起降亮度。保留黄蓝色差，随后用两条白线确认它的位置。
        dim_mask = (rb > 75) & (gb > 35) & (r > 100) & (g > 85)
        edges = _fit_edges(road, dim_mask, dim=True)
    return edges


def _fit_edges(road: np.ndarray, mask: np.ndarray, *, dim: bool) -> list[YellowEdge]:
    edges = []
    for track in _tracks(mask):
        if len(track) < 6:
            continue
        ys = np.array([_SCAN_Y + y + _BAND / 2 for y, _, _ in track])
        xs = np.array([(lo + hi) / 2 for _, lo, hi in track])
        slope, intercept = np.polyfit(ys, xs, 1)
        rms = np.sqrt(np.mean((xs - (slope * ys + intercept)) ** 2))
        if rms > 12 or abs(slope * _SCAN_Y + intercept - VPX) > _VP_TOL:
            continue
        failed = interior = 0
        pixels = []
        for y, lo, hi in track:
            region = slice(y, y + _BAND), slice(lo, hi + 1)
            band = mask[region]
            if not _solid(band):
                failed += 1
                interior += lo > 2 and hi < mask.shape[1] - 3
            pixels.append(road[region][band])
        if failed > max(2, int(0.15 * len(track))) or interior > 2:
            continue
        samples = np.concatenate(pixels)
        if len(samples) < 200:
            continue
        red, green, blue = samples.mean(axis=0)
        min_red, min_green, min_diff, max_std = (135, 110, 95, 34) if dim else (180, 160, 110, 28)
        if (min_red <= red <= 255 and min_green <= green <= 235 and 0 <= blue <= 70
                and red - blue >= min_diff and samples.std(axis=0).max() <= max_std):
            edges.append(YellowEdge(float(slope), float(intercept), float(ys[0]), float(ys[-1]), dim))
    return edges


def _matches_model(edge: YellowEdge, line: dict) -> bool:
    if not _local_line_good(line):
        return False
    for fit in line.get("fits", ()):
        lo = max(edge.y0, fit["y0"])
        hi = min(edge.y1, fit["y1"])
        slope_error = edge.slope - fit["s"]
        if (lo <= hi and abs(slope_error) <= 0.7
                and abs(slope_error * ((lo + hi) / 2) + edge.intercept - fit["a"]) <= 20):
            return True
    return False


class YellowAnchor:
    """用同拍线模型复核路缘，连续证据确认后给车道线定绝对道号。"""

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque(maxlen=5)

    def update(self, edges: list[YellowEdge], lines: list[dict], cam_h: float,
               now: float, *, y_v: float | None, reset: bool = False) -> float | None:
        if reset:
            self._samples.clear()
        while self._samples and now - self._samples[0][0] > 0.5:
            self._samples.popleft()
        if y_v is None or cam_h <= 0.0:
            return None
        lines = [line for line in lines if _local_line_good(line)]
        positions = []
        for edge in edges:
            offset = edge.slope * cam_h / LANE_W
            side = 1 if offset > 0 else -1
            if (not 0.10 <= abs(offset) <= 2 * ROAD_HALF_LANES
                    or any(0.7 <= side * (line["L"] - offset) <= 1.3 for line in lines)
                    or (not edge.dim and not any(_matches_model(edge, line) for line in lines))):
                continue
            edge_k = side * ROAD_HALF_LANES
            votes = {}
            rows = np.linspace(max(edge.y0, y_v + 20), edge.y1, 5) if edge.y1 >= y_v + 20 else ()
            for line in lines:
                gaps = []
                for y in rows:
                    fit = _line_fit_at(line, y)
                    if fit is not None:
                        gap = (fit["s"] - edge.slope) * y + fit["a"] - edge.intercept
                        gaps.append(gap * cam_h / (LANE_W * (y - y_v)))
                if not gaps:
                    continue
                step = int(round(float(np.median(gaps))))
                k = edge_k + step
                # 按同一成像行上隔了几个道宽编号，漏掉中间虚线时可直接跳号。
                if not -ROAD_HALF_LANES < k < ROAD_HALF_LANES or max(abs(g - step) for g in gaps) > 0.35:
                    continue
                x = k - line["L"]
                if abs(x - (edge_k - offset)) <= 0.35:
                    votes.setdefault(k, []).append(x)
            if len(votes) >= (2 if edge.dim else 1):
                positions.extend(float(np.median(group)) for group in votes.values())
        if not positions or max(positions) - min(positions) > 0.3:
            return None
        current = float(np.median(positions))
        if self._samples and abs(current - self._samples[-1][1]) > 0.3:
            self._samples.clear()
        self._samples.append((now, current))
        if len(self._samples) < 3:
            return None
        median = float(np.median([pos for _, pos in self._samples]))
        return median if abs(median - current) <= 0.3 else None
