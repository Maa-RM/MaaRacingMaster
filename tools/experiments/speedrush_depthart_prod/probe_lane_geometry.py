# -*- coding: utf-8 -*-
"""车道线独立几何探针：逐帧灭点行 + 相机高（不依赖深度模型）。

**方法**（与车道线金标同源）：同一行上相邻两条车道线的像素间距 Δx ↔ 3.5m，
地面关系 ``cam_h = lane_w·(y−y_h)/Δx``——**不需要焦距**。y_h 用同一帧多条线的
交点（灭点行）现估，不用固定常数。逐帧对全部相邻线对、近场行取中位。

**本轮要回答的**：
1. 保险杠机位帧上 cam_h 是否稳定落在权威带 1.12~1.55（视角守卫可用性）；
2. 逐帧 y_h 与 gate0 常数 354.2 的偏差（相机俯仰/路面平直假设的观测残差）；
3. 线对数量与近场行覆盖（断线/遮挡时还够不够算）。

**与生产同步**（2026-11-11 起）：行扫描步长 1（原 3）、提取不吃颜色——与
`lane_obs._row_segments` / `_build_tracks` 同口径，见 commit `cebfa34`。

输出 `lane_geom_out/lane_geom.json` 与两帧叠加图（线 + 灭点行 + cam_h）。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).parent
WEIGHTS = ROOT / "weights" / "lane" / "plus_small_384x640.onnx"
OUT = ROOT / "lane_geom_out"
LANE_W = 3.5
PROB_THR = 0.30
IN_W, IN_H = 640, 360
PAD = 12
ROWS = range(206, 360)             # 生产口径（裁边后的 360 行坐标，步长 1）
MIN_TRACK = 8
MAX_RMS = 4.0                      # 全幅像素
# 灭点行的物理门：相机俯仰有界，地平线不会跑出 ±20px（fy=630 下约 ±2°）。
# 步长 1 后远场碎片变多，稀疏碎片两两交点会给出 300 行的假灭点（0102/0103 实测），
# 门是俯仰先验，不是拟合参数。
YH_LO, YH_HI = 341.0, 381.0


def list_frames() -> list[Path]:
    frames = sorted((ROOT / "frames").glob("*_raw.jpg"))
    for d in sorted((ROOT / "frames").iterdir()):
        if d.is_dir():
            frames.extend(sorted(d.glob("*_raw.jpg")))
    return frames


def lane_prob(sess, rgb: np.ndarray) -> np.ndarray:
    im = cv2.resize(rgb, (IN_W, IN_H), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, PAD, PAD, 0, 0, cv2.BORDER_CONSTANT,
                            value=(114, 114, 114))
    blob = (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    out = sess.run(None, {sess.get_inputs()[0].name: blob})
    logits = out[1][0][:, PAD:PAD + IN_H, :]
    e = np.exp(logits - logits.max(0, keepdims=True))
    return (e[1] / e.sum(0)).astype(np.float32)


def tracks(prob: np.ndarray, tol: float = 26.0, max_gap: int = 10):
    """行段 → 轨迹（生产 _row_segments/_build_tracks 的最小复刻，全幅坐标）。

    全幅坐标：tol=26px（生产 13 个裁边像素）、max_gap=10 个裁边行（=20 全幅行）、
    速度平滑限幅 8（比值与分辨率无关）。
    """
    rows = []
    for yc in ROWS:
        idx = np.flatnonzero(prob[yc] > PROB_THR)
        if len(idx) == 0:
            continue
        splits = np.flatnonzero(np.diff(idx) > 1) + 1
        segs = []
        for seg in np.split(idx, splits):
            if len(seg) == 0:
                continue
            p = prob[yc][seg]
            segs.append((float((seg * p).sum() / p.sum()) * 2.0, yc * 2.0))
        if segs:
            rows.append((yc * 2.0, segs))
    built: list[dict] = []
    for y, segs in rows:
        used = set()
        for tr in built:
            tr["_pred"] = None
            if y - tr["y"] > max_gap * 2:
                continue
            tr["_pred"] = tr["x"] + tr.get("v", 0.0) * (y - tr["y"])
        for x, _ in segs:
            best, best_d = None, tol * 2
            for j, tr in enumerate(built):
                if j in used or tr["_pred"] is None:
                    continue
                d = abs(x - tr["_pred"])
                if d < best_d:
                    best, best_d = j, d
            if best is None:
                built.append({"pts": [(x, y)], "x": x, "y": y, "v": 0.0})
                used.add(len(built) - 1)
            else:
                tr = built[best]
                dv = (x - tr["x"]) / max(1.0, y - tr["y"])
                tr["v"] = 0.7 * tr.get("v", 0.0) + 0.3 * float(np.clip(dv, -8, 8))
                tr["pts"].append((x, y))
                tr["x"], tr["y"] = x, y
                used.add(best)
    return [t["pts"] for t in built]


def fit_line(pts):
    a = np.asarray(pts, np.float64)
    if len(a) < MIN_TRACK:
        return None
    y, x = a[:, 1], a[:, 0]
    A = np.stack([y, np.ones_like(y)], 1)
    coef = np.linalg.lstsq(A, x, rcond=None)[0]
    rms = float(np.sqrt(np.mean((A @ coef - x) ** 2)))
    if rms > MAX_RMS:
        return None
    return float(coef[0]), float(coef[1]), rms, len(a)


def vanish_row(fits, y_probe: float = 520.0):
    """多线两两交点中位（带门）：俯仰物理门 + 行 520 处线距 60~900px + MAD 剔除。

    步长 1 后远场碎片变多：疏碎片两两交点会给出 300~348 行的假灭点。线距门挡
    「同线碎片/跨太多道」，MAD 挡少数派假交点。行 520 处车道宽约 450px
    （z≈4.9m），门宽放到 60~900 覆盖近车道到两车道。"""
    yvs = []
    for i in range(len(fits)):
        for j in range(i + 1, len(fits)):
            s1, a1 = fits[i][0], fits[i][1]
            s2, a2 = fits[j][0], fits[j][1]
            if abs(s1 - s2) < 5e-3:
                continue
            yv = (a2 - a1) / (s1 - s2)
            if not (YH_LO <= yv <= YH_HI):
                continue
            d = abs((s1 - s2) * y_probe + (a1 - a2))
            if not (60.0 <= d <= 900.0):
                continue
            yvs.append(float(yv))
    if not yvs:
        return None
    med = float(np.median(yvs))
    mad = float(np.median(np.abs(np.asarray(yvs) - med)) * 1.4826)
    keep = [y for y in yvs if abs(y - med) <= 2.5 * mad] if mad > 0.5 else yvs
    return float(np.median(keep))


def geometry(prob: np.ndarray) -> dict:
    fits = [f for f in (fit_line(p) for p in tracks(prob)) if f]
    if len(fits) < 2:
        return {"lines": len(fits), "cam_h": None, "y_v": None}
    # 灭点行：两两交点（俯仰门 + 线距门 + MAD）
    y_v = vanish_row(fits)
    y_v = 354.2 if y_v is None else y_v
    # 相邻线对：逐行按 x 排序取相邻对，cam_h = lane_w·(y−y_v)/Δx
    ests = []
    for y in range(450, 700, 10):
        xs = sorted(s * y + a for s, a, _, _ in fits)
        for x0, x1 in zip(xs, xs[1:]):
            d = x1 - x0
            if d < 40.0:                 # 近场相邻道至少约 100px；太小是同线碎片
                continue
            ests.append(LANE_W * (y - y_v) / d)
    ests = [e for e in ests if 0.5 <= e <= 3.0]
    cam_h = float(np.median(ests)) if ests else None
    return {"lines": len(fits), "cam_h": cam_h, "y_v": y_v,
            "cam_h_p10_p90": (float(np.percentile(ests, 10)),
                              float(np.percentile(ests, 90))) if ests else None,
            "n_pair_rows": len(ests)}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(str(WEIGHTS), sess_options=so,
                                providers=["DmlExecutionProvider",
                                           "CPUExecutionProvider"])
    frames = list_frames()
    print(f"frames: {len(frames)}")
    recs = []
    for i, p in enumerate(frames):
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        g = geometry(lane_prob(sess, rgb))
        g["file"] = str(p.relative_to(ROOT / "frames"))
        recs.append(g)
        if i < 18 and i % 5 == 0:
            # 叠加：线轨迹 + 灭点行 + cam_h
            prob = lane_prob(sess, rgb)
            vis = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            for pts in tracks(prob):
                for x, y in pts:
                    cv2.circle(vis, (int(x), int(y)), 2, (0, 255, 0), -1)
            yv = g["y_v"] or 354.2
            cv2.line(vis, (0, int(yv)), (1279, int(yv)), (0, 0, 255), 2)
            cv2.putText(vis, f"cam_h={g['cam_h']}", (12, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            cv2.putText(vis, f"y_v={g['y_v']:.1f} lines={g['lines']}", (12, 80),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            cv2.imwrite(str(OUT / f"lane_{i:03d}.jpg"), vis)
    ok = [r for r in recs if r["cam_h"] is not None]
    out = {"frames": recs,
           "summary": {
               "n": len(recs), "n_ok": len(ok),
               "cam_h_median": float(np.median([r["cam_h"] for r in ok])),
               "cam_h_p10_p90": (float(np.percentile([r["cam_h"] for r in ok], 10)),
                                 float(np.percentile([r["cam_h"] for r in ok], 90))),
               "y_v_median": float(np.median([r["y_v"] for r in ok])),
               "in_guard_band_frac": float(np.mean(
                   [1.12 <= r["cam_h"] <= 1.55 for r in ok])),
           }}
    (OUT / "lane_geom.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out["summary"], indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
