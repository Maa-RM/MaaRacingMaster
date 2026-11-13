# -*- coding: utf-8 -*-
"""投影焦距锚审计：直接光流标定（否定结果）+ 现有锚的互差。

**为什么需要这一轮**：DepthART 的米制距离是 `z = m·k/(V−A−a·u)`，`k = fy·cam_h`；
`cam_h` 由车道线独立给出 1.25m，剩下唯一未知是投影焦距 `fy`。项目里现有两个
自洽但互差约 20% 的锚：

- 纵向（速度）：得分速度底值 41 = 41 m/s（RULES §4.8）→ 线角标定 807.7；
- 横向/几何：车道宽 3.5m + 机位高 1.25m → MoGe 点云口径 ≈670.7。

本轮先试**最直接的独立方法**：真接近段（41 m/s、真时戳）上对静态路面做稀疏
光流，平路几何给 ``dy = (y−y_h)²·v·dt/k``，拟合 `s` 得 `k = v·dt/s`。**实测失败**：
0.1s 帧间隔 + 重复沥青纹理下，PyrLK 的位移场锁不住（大量点反向/横飞），
Farneback 全局流在近场带近似为零；有效点不足，不能出焦距。

结论不是「焦距等于多少」，而是：**现有数据无法独立定标，两个锚的 20% 差是
项目级尺度裁决，不是 DepthART 的模型误差**。输出 `focal_out/focal_anchors.json`，
把否定结果与锚互差留档，避免下一轮重复这条路。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np

import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "focal_out"
SPEED_MS = 41.0
CAM_H = 1.25
ROWS = (430, 660)
YH_GRID = np.arange(320.0, 401.0, 1.0)
LANE_ANGLE_FOCAL = 807.7      # gate0 focal_px（线角几何口径）
MOGE_FOCAL = 670.7            # gate0 notes：MoGe 点云/框投影口径
FOV_DEG = (56.9, 62.0)        # RULES §3.8 保险杠视角曲线（正常速域）


def road_mask(h: int, w: int, boxes) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    m[ROWS[0]:ROWS[1], :] = 1
    for b in boxes:
        x0, y0, x1, y1 = b
        cv2.rectangle(m, (max(0, x0 - 8), max(0, y0 - 8)),
                      (min(w - 1, x1 + 8), min(h - 1, y1 + 8)), 0, -1)
    return m


def features(gray: np.ndarray, mask: np.ndarray) -> np.ndarray:
    pts = cv2.goodFeaturesToTrack(gray, maxCorners=900, qualityLevel=0.008,
                                  minDistance=6, blockSize=7, mask=mask)
    if pts is None or len(pts) < 120:
        ys, xs = np.nonzero(mask)
        if len(ys) == 0:
            return np.empty((0, 1, 2), np.float32)
        idx = np.linspace(0, len(ys) - 1, min(1200, len(ys))).astype(int)
        pts = np.stack([xs[idx], ys[idx]], 1).astype(np.float32)[:, None, :]
    return pts


def fit_origin(x: np.ndarray, dy: np.ndarray, iters: int = 4) -> tuple[float, np.ndarray]:
    wgt = np.ones_like(x)
    s = 0.0
    for _ in range(iters):
        den = np.sum(wgt * x ** 4) + 1e-12
        s = float(np.sum(wgt * x ** 2 * dy) / den)
        resid = dy - s * x ** 2
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        wgt = 1.0 / (1.0 + (resid / (2.5 * mad)) ** 2)
    return s, dy - s * x ** 2


def fov_focal(fov_deg: float, axis_px: float) -> float:
    return axis_px / math.tan(math.radians(fov_deg) / 2.0)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    files = sorted((ROOT / "frames").glob("*_raw.jpg"))
    rgbs = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in files]
    grays = [cv2.cvtColor(x, cv2.COLOR_RGB2GRAY) for x in rgbs]
    ts = {}
    if sim.FRAMES_JSONL.exists():
        for line in sim.FRAMES_JSONL.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            ts[r["file"].split("/")[-1]] = r["ts_ns"] / 1e9
    t = [ts.get(p.name) for p in files]

    det_sess = sim.session("prod_perception/model.onnx")
    lane_sess = sim.session("lane/plus_small_384x640.onnx")
    dets = [sim.detect_cars(det_sess, fr) for fr in rgbs]

    attempts = []
    valid = []
    for i in range(len(files) - 1):
        if t[i] is None or t[i + 1] is None:
            continue
        dt = float(t[i + 1] - t[i])
        if not 0.03 <= dt <= 0.25:
            continue
        h, w = grays[i].shape
        mask = road_mask(h, w, dets[i] + dets[i + 1])
        p0 = features(grays[i], mask)
        rec = {"pair": f"{files[i].name}->{files[i + 1].name}", "dt": dt,
               "n_feat": int(len(p0))}
        if len(p0) >= 30:
            p1, st, _ = cv2.calcOpticalFlowPyrLK(
                grays[i], grays[i + 1], p0, None, winSize=(21, 21), maxLevel=4,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
            p0b, stb, _ = cv2.calcOpticalFlowPyrLK(
                grays[i + 1], grays[i], p1, None, winSize=(21, 21), maxLevel=4,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
            a, b, bb = p0[:, 0], p1[:, 0], p0b[:, 0]
            fb = np.linalg.norm(bb - a, axis=1)
            dx, dy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
            keep = ((st[:, 0] == 1) & (stb[:, 0] == 1) & (fb < 1.2)
                    & (dy > 0.3) & (dy < 120.0) & (np.abs(dx) < 0.35 * dy + 4.0)
                    & (a[:, 1] >= ROWS[0]) & (a[:, 1] < ROWS[1]))
            rec.update({"st": int(st[:, 0].sum()),
                        "fb_ok": int((fb < 1.2).sum()),
                        "dy_p50_p90": [float(np.median(dy)),
                                       float(np.percentile(dy, 90))],
                        "keep": int(keep.sum())})
            # Farneback 全局流（半分辨率）复核：近场带拟合斜率应为正
            scl = 0.5
            a0 = cv2.resize(grays[i], (int(w * scl), int(h * scl)),
                            interpolation=cv2.INTER_AREA)
            a1 = cv2.resize(grays[i + 1], (int(w * scl), int(h * scl)),
                            interpolation=cv2.INTER_AREA)
            flow = cv2.calcOpticalFlowFarneback(a0, a1, None, 0.5, 4, 31, 5, 7, 1.5, 0)
            hh, ww = a0.shape
            ys, xs = np.mgrid[0:hh, 0:ww]
            sel = ((ys > ROWS[0] * scl) & (ys < ROWS[1] * scl)
                   & (np.abs(flow[..., 0] * (w / ww)) < 30)
                   & (flow[..., 1] * (h / hh) > -20)
                   & (flow[..., 1] * (h / hh) < 100))
            if sel.sum() > 100:
                x = ys[sel] * (h / hh) - 354.2
                d = flow[..., 1][sel] * (h / hh)
                s_fb = float(np.sum(x ** 2 * d) / (np.sum(x ** 4) + 1e-12))
                rec["farneback_s"] = s_fb
                rec["farneback_resid_p50"] = float(np.median(np.abs(d - s_fb * x ** 2)))
            if keep.sum() >= 40:
                y = a[keep, 1].astype(np.float64)
                dyv = dy[keep].astype(np.float64)
                best = None
                for yh in YH_GRID:
                    s, resid = fit_origin(y - yh, dyv)
                    score = float(np.median(np.abs(resid)))
                    if best is None or score < best[0]:
                        best = (score, float(yh), s)
                score, yh, s = best
                if s > 1e-7:
                    k = SPEED_MS * dt / s
                    valid.append({"pair": rec["pair"], "n": int(keep.sum()),
                                  "yh_flow": yh, "s": s, "k": k,
                                  "fy": k / CAM_H, "resid_p50": score})
        attempts.append(rec)
        print(rec["pair"], "feat", rec["n_feat"], "keep", rec.get("keep"),)

    anchors = {
        "lane_angle_focal": LANE_ANGLE_FOCAL,
        "moge_pointcloud_focal": MOGE_FOCAL,
        "fov_vertical_focal": [fov_focal(f, 360.0) for f in FOV_DEG],
        "fov_horizontal_focal": [fov_focal(f, 640.0) for f in FOV_DEG],
        "lane_angle_over_moge": LANE_ANGLE_FOCAL / MOGE_FOCAL,
        "mileage_alt_1_22_times_lane_angle": LANE_ANGLE_FOCAL / 1.22,
    }
    out = {"speed_ms": SPEED_MS, "cam_h": CAM_H,
           "flow_valid": valid,
           "flow_attempts": attempts,
           "anchors": anchors,
           "summary": {
               "flow_valid_pairs": len(valid),
               "flow_keep_p50": float(np.median(
                   [a.get("keep", 0) for a in attempts])) if attempts else 0,
               "flow_fy_median": float(np.median([v["fy"] for v in valid]))
               if valid else None,
               "anchors": anchors,
           }}
    (OUT / "focal_anchors.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print("valid pairs:", len(valid))
    print("anchors:", json.dumps(anchors, ensure_ascii=False))
    print("saved:", OUT / "focal_anchors.json")


if __name__ == "__main__":
    main()
