# -*- coding: utf-8 -*-
"""DepthART 生产模拟：检测框 × DepthART 地面校准 → 米制车距；三方对照。

**链路（自包含，不 import 业务代码）**：
```
perception 检测 → 出租车跟踪 → DepthART slim-SPF(官方预处理)
  → 地面模型 V=A+m·(r−y_h)+a·u（近=大，视差向）
  → z = m·k/(V−A−a·u)，k=fy·cam_h
  → 框底条带中位 → 车距
```
**对照三臂**：
- MoGe 生产 q4f16（米制直读 ×0.893，框底条带）；
- DepthART（fy=670.7 / 807.7 两档，cam_h=1.25 权威）；
- 无深度地平面基线：z=k/(框底行−y_h)（同一 k，看深度到底补了多少）。

**车道线**：逐帧灭点行 y_h 与相机高（`probe_lane_geometry`），既供地面模型的
y_h，也作视角守卫可用性证据；cam_h 只用于守卫，不参与距离尺度（权威 1.25）。

输出 `prod_sim_out/prod_sim.json` 与逐帧表。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

import probe_lane_geometry as lg  # noqa: E402  同试验区自建探针

ROOT = Path(__file__).parent
WD = ROOT / "weights"
OUT = ROOT / "prod_sim_out"
FRAMES_JSONL = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/"
                    "20261111_103853/speedrush/drive/20261111_104434_p1/frames.jsonl")
IMGSZ = 640
CONF = 0.35
ROI = (0, 201, 1280, 561)
YH_FALLBACK = 354.2
LANE_W = 3.5
CAM_H = 1.25
VPX = 640.7
FOCALS = (670.7, 807.7)
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
DEPTH_SCALE = 0.893


def session(rel: str, free_dims: dict | None = None) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if free_dims:
        for k, v in free_dims.items():
            so.add_free_dimension_override_by_name(k, int(v))
    return ort.InferenceSession(str(WD / rel), sess_options=so,
                                providers=["DmlExecutionProvider",
                                           "CPUExecutionProvider"])


# ---------- perception ----------
def detect_cars(sess, rgb: np.ndarray):
    x1, y1, x2, y2 = ROI
    crop = rgb[y1:y2, x1:x2]
    h, w = crop.shape[:2]
    scale = min(IMGSZ / h, IMGSZ / w)
    nh, nw = int(h * scale), int(w * scale)
    px, py = (IMGSZ - nw) // 2, (IMGSZ - nh) // 2
    padded = np.full((IMGSZ, IMGSZ, 3), 114, np.uint8)
    padded[py:py + nh, px:px + nw] = cv2.resize(crop, (nw, nh),
                                                interpolation=cv2.INTER_LINEAR)
    blob = padded.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    preds = sess.run(None, {"images": blob})[0][0].T
    xywh, cls_conf = preds[:, :4], preds[:, 4:]
    scores, classes = cls_conf.max(1), cls_conf.argmax(1)
    m = (classes == 1) & (scores > CONF)
    if not m.any():
        return []
    b = xywh[m]
    xy = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2,
                   b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], 1)
    keep = cv2.dnn.NMSBoxes(xy.tolist(), scores[m].tolist(), 0.0, 0.45)
    out = []
    for i in np.array(keep).ravel():
        a1, b1, a2, b2 = xy[int(i)]
        out.append((int((a1 - px) / scale + x1), int((b1 - py) / scale + y1),
                    int((a2 - px) / scale + x1), int((b2 - py) / scale + y1)))
    return out


def track_taxi(dets_per_frame):
    n = len(dets_per_frame)

    def area(b):
        return (b[2] - b[0]) * (b[3] - b[1])

    seed_i, cur = None, None
    for i, dets in enumerate(dets_per_frame):
        for b in dets:
            if 0.2 * 1280 <= (b[0] + b[2]) / 2 <= 0.9 * 1280 \
                    and (cur is None or area(b) > area(cur)):
                cur, seed_i = b, i
    if cur is None:
        return [None] * n
    track = [None] * n
    track[seed_i] = cur
    for i in range(seed_i - 1, -1, -1):
        cur = _match(dets_per_frame[i], cur) if cur else None
        track[i] = cur
    cur = track[seed_i]
    for i in range(seed_i + 1, n):
        cur = _match(dets_per_frame[i], cur) if cur else None
        track[i] = cur
    return track


def _match(dets, cur):
    cx, cy = (cur[0] + cur[2]) / 2, (cur[1] + cur[3]) / 2
    h_cur = cur[3] - cur[1]
    best, best_d = None, 1e9
    for b in dets:
        bx, by = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        d = abs(bx - cx) + abs(by - cy) * 2
        if abs(bx - cx) < 0.25 * 1280 and (b[3] - b[1]) < h_cur * 1.6 and d < best_d:
            best, best_d = b, d
    return best


# ---------- DepthART ----------
def da_prep(rgb):
    im = cv2.resize(rgb, (448, 448), interpolation=cv2.INTER_CUBIC)
    x = im.astype(np.float32) / 255.0
    x = (x - MEAN) / STD
    return x.transpose(2, 0, 1)[None]


def resize_ac(img, h, w):
    sh, sw = img.shape[:2]
    ys = np.linspace(0, sh - 1, h)
    xs = np.linspace(0, sw - 1, w)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, sh - 1), np.minimum(x0 + 1, sw - 1)
    wy, wx = (ys - y0)[:, None], (xs - x0)[None, :]
    a = img[y0][:, x0] * (1 - wx) + img[y0][:, x1] * wx
    b = img[y1][:, x0] * (1 - wx) + img[y1][:, x1] * wx
    return a * (1 - wy) + b * wy


def ground_fit(v, yh):
    h, w = v.shape
    rows = np.arange(h, dtype=np.float64)
    band = (rows >= 390) & (rows <= 670)
    ys, xs = np.nonzero(band[:, None] & np.ones((h, w), bool))
    u = (np.arange(w) + 0.5) / w
    A = np.stack([ys - yh, u[xs], np.ones(len(ys))], 1)
    y = v[ys, xs]
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(4):
        resid = A @ coef - y
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        keep = np.abs(resid - np.median(resid)) < 2.5 * mad
        coef = np.linalg.lstsq(A[keep], y[keep], rcond=None)[0]
    return coef


def band_median(v, box):
    gh, gw = v.shape[:2]
    x0 = max(0, int(box[0] * gw / 1280)); y0 = max(0, int(box[1] * gh / 720))
    x1 = min(gw, int(box[2] * gw / 1280)); y1 = min(gh, int(box[3] * gh / 720))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None, None
    yb = y0 + (y1 - y0) * 3 // 4
    zv = v[yb:y1, x0:x1]
    zv = zv[np.isfinite(zv)]
    if zv.size < 5:
        return None, None
    return float(np.median(zv)), 0.5 * (x0 + x1) / gw


# ---------- MoGe baseline ----------
def moge_run(sess, rgb):
    im = cv2.resize(rgb, (1280, 720), interpolation=cv2.INTER_AREA)
    blob = (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    feed = {"image": blob}
    if any(i.name == "num_tokens" for i in sess.get_inputs()):
        feed["num_tokens"] = np.array(1032, np.int64)
    out = sess.run(None, feed)
    pts = out[0][0]
    mask = out[2][0] if len(out) > 2 else np.ones(pts.shape[:2], bool)
    scale = float(out[3][0]) if len(out) > 3 else 1.0
    z = pts[..., 2] * scale * DEPTH_SCALE
    z[(mask <= 0.5) | ~np.isfinite(z)] = np.nan
    return cv2.resize(z, (1280, 720), interpolation=cv2.INTER_LINEAR)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    files = sorted((ROOT / "frames").glob("*_raw.jpg"))
    frames = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in files]
    ts = {}
    if FRAMES_JSONL.exists():
        for line in FRAMES_JSONL.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            ts[r["file"].split("/")[-1]] = r["ts_ns"] / 1e9
    t = [ts.get(p.name) for p in files]
    print(f"frames {len(frames)}, ts ok {sum(x is not None for x in t)}")

    det_sess = session("prod_perception/model.onnx")
    dets = [detect_cars(det_sess, fr) for fr in frames]
    track = track_taxi(dets)
    print(f"taxi tracked {sum(b is not None for b in track)}/{len(frames)}")

    da = session("depthart/relative_mnv4m_slim_spf_448_default.onnx")
    moge = session("prod_moge_q4f16.onnx",
                   free_dims={"batch_size": 1, "height": 720, "width": 1280})
    lane = session("lane/plus_small_384x640.onnx")

    rows = []
    for i, (rgb, box) in enumerate(zip(frames, track)):
        rec = {"i": i, "file": files[i].name, "box": box, "t": t[i]}
        # 车道几何（y_h + cam_h）
        g = lg.geometry(lg.lane_prob(lane, rgb))
        rec.update({"lane_cam_h": g["cam_h"], "lane_y_v": g["y_v"],
                    "lane_lines": g["lines"]})
        yh = g["y_v"] if g["y_v"] is not None else YH_FALLBACK
        rec["yh_used"] = yh
        # MoGe 基线
        zm = moge_run(moge, rgb)
        mz, _ = band_median(zm, box) if box else (None, None)
        rec["moge_z"] = mz
        # DepthART 地面模型
        out = da.run(None, {"image": da_prep(rgb)})[0][0, 0]
        v = resize_ac(out.astype(np.float64), 720, 1280)
        coef = ground_fit(v, yh)
        A, m, a = float(coef[2]), float(coef[0]), float(coef[1])
        vmed, unorm = band_median(v, box) if box else (None, None)
        rec["da_ground"] = {"A": A, "m": m, "a": a}
        if box and vmed is not None:
            ybot = box[3]
            rec["diag"] = {
                "ybot": ybot, "vmed": vmed,
                "vg_at_bottom": A + m * (ybot - yh) + a * unorm,
                "vg_at_band_mid": A + m * (0.875 * box[3] + 0.125 * box[1] - yh)
                + a * unorm,
                "ground_row_z_670": 670.7 * CAM_H / max(1.0, ybot - yh),
            }
        for fy in FOCALS:
            k = fy * CAM_H
            z = None
            if vmed is not None:
                den = vmed - A - a * unorm
                z = (m * k / den) if den > 1e-6 else None
            rec[f"da_z_fy{int(fy)}"] = z
            # 无深度地平面基线
            zg = None
            if box:
                ybot = box[3]
                zg = (k / (ybot - yh)) if ybot > yh + 1 else None
            rec[f"ground_z_fy{int(fy)}"] = zg
            # 横向（米/道）
            if z is not None and box:
                cx = 0.5 * (box[0] + box[2])
                rec[f"da_xlane_fy{int(fy)}"] = (cx - VPX) * z / fy / LANE_W
        rows.append(rec)
        print(i, files[i].name, "box" if box else "no-box",
              "moge", None if mz is None else round(mz, 1),
              "da670", None if rec.get("da_z_fy670") is None else round(rec["da_z_fy670"], 1),
              "da808", None if rec.get("da_z_fy807") is None else round(rec["da_z_fy807"], 1),
              "gr670", None if rec.get("ground_z_fy670") is None else round(rec["ground_z_fy670"], 1),
              "camh", None if g["cam_h"] is None else round(g["cam_h"], 2))

    # 尺度拟合：DepthART(fy=670) 对 MoGe 的最优乘数
    pairs = [(r["moge_z"], r["da_z_fy670"]) for r in rows
             if r["moge_z"] and r["da_z_fy670"]]
    fit = None
    if pairs:
        ref = np.array([p[0] for p in pairs])
        est = np.array([p[1] for p in pairs])
        s = float((ref * est).sum() / (est * est).sum())
        fit = {"n": len(pairs), "scale_to_moge": s,
               "implied_fy_at_cam_h_1.25": s * FOCALS[0],
               "mae_before": float(np.mean(np.abs(est - ref))),
               "mae_after": float(np.mean(np.abs(s * est - ref)))}
    (OUT / "prod_sim.json").write_text(
        json.dumps({"rows": rows, "fit": fit}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    print("fit:", fit)
    print("saved:", OUT / "prod_sim.json")


if __name__ == "__main__":
    main()
