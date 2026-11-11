# -*- coding: utf-8 -*-
"""车道线金标探针：车道线模型 × 单车道宽 → 逐行深度金标 + 米制模型偏差系数。

金标（维护者定调 2026-11-11，已修订）：车道线模型给每行车道线像素，相邻两条
车道线的像素间距 ↔ 3.5m 物理宽（四车道画线）。**间距在 640 宽的模型输入上量，
生产地面关系的 x_px 是 1280 全幅——间距须 ×2 才同尺度**（2026-11-11 排查实锤：
不乘则 cam_h 恰好虚大 2×，2.228m 假读数的成因）。反推值 ~1.13m 与权威锚
1.25m（保险杠相机离地高，RULES）差 ~9%，作独立交叉核对，不作锚。

深度金标锚 = 权威 1.25m：z_gold(y) = FX·1.25/(y−Y_H)。米制输出模型的偏差系数
（与 MoGe 0.893 同一药方）：金标行上 `coefficient = median(z_gold/model_metric)`，
系数入适配器单入口后继续米制直读。

关键性质：车道间距**直接测 cam_h**（无需焦距）——宽与高在同一深度上，焦距相消。
生产地面关系 `x_px = VPX + L*LANE_W*(y−Y_H)/cam_h`（lane_obs.py）⇒
`cam_h_implied = LANE_W*(y−Y_H)/Δx_px`，逐行解出。

预处理/行分段逐项复刻 lane_obs.py（letterbox 640×360 +12px 边、channel 1
softmax、行 206~360 step 3、PROB_THRESH 0.30）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "bakeoff_out"
FRAMES_DIR = ROOT / "frames" / "20261111_104434_p1"
LANE_WEIGHTS = Path("D:/maaracing_assistant/maaracing_master/plugins/speedrush/"
                    "resources/models/lane/plus_small_384x640.onnx")

LANE_W = 3.5        # 单车道物理宽（gate0/RULES）
VPX = 640.7         # lane_obs 常量（消失点横向）
Y_H = 354.2         # 地平线行（gate0 y_h，720 行口径）
PROB_THRESH = 0.30  # lane_obs 行分段阈值
FX = 670.7          # gate0 notes：MoGe fx≈670（点云/反投影口径，全幅像素）


def letterbox(rgb: np.ndarray) -> np.ndarray:
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, 12, 12, 0, 0, cv2.BORDER_CONSTANT,
                            value=(114, 114, 114))
    return np.ascontiguousarray(
        (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def lane_prob(sess, rgb: np.ndarray) -> np.ndarray:
    out = sess.run(None, {sess.get_inputs()[0].name: letterbox(rgb)})
    logits = out[1][0][:, 12:372, :]
    e = np.exp(logits - logits.max(0, keepdims=True))
    return (e[1] / e.sum(0)).astype(np.float32)  # channel 1 = 车道线概率


def row_segments(prob: np.ndarray):
    """逐行车道线分段（复刻 lane_obs._row_segments）。yc 为 360 行口径。"""
    rows = []
    for yc in range(206, 360, 3):
        row = prob[yc]
        idx = np.flatnonzero(row > PROB_THRESH)
        if len(idx) == 0:
            continue
        splits = np.flatnonzero(np.diff(idx) > 1) + 1
        segs = []
        for seg in np.split(idx, splits):
            if len(seg) == 0:
                continue
            p = row[seg]
            segs.append((float((seg * p).sum() / p.sum()), float(p.mean())))
        if len(segs) >= 2:
            rows.append((yc, segs))
    return rows


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    files = sorted(FRAMES_DIR.glob("*_raw.jpg"))
    frames = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in files]

    so = ort.SessionOptions()
    so.log_severity_level = 3
    lane = ort.InferenceSession(str(LANE_WEIGHTS), sess_options=so,
                                providers=["CPUExecutionProvider"])

    # ── 金标：逐帧逐行 cam_h_implied ──
    # 间距在 640 宽（车道线模型输入）上量，生产关系 x_px 是 1280 全幅——
    # ×2 才同尺度（2026-11-11 排查：不乘则 cam_h 恰好虚大 2×，2.228→1.133m）。
    all_cam = []
    per_frame = []
    for fr, p in zip(frames, files):
        prob = lane_prob(lane, fr)
        rows = row_segments(prob)
        cams, rows_out = [], []
        for yc, segs in rows:
            xs = sorted(x for x, _ in segs)
            gaps = np.diff(xs)
            gaps = gaps[(gaps > 20) & (gaps < 1200)]
            if len(gaps) == 0:
                continue
            spacing = float(np.median(gaps)) * 2.0  # 640 → 1280 全幅
            # yc(360 口径 letterbox) → 原帧行： (yc-12)/360*720
            y_orig = (yc - 12) / 360.0 * 720.0
            # 生产地面关系 Δx = LANE_W·(y−Y_H)/cam_h ⇒ cam_h = LANE_W·(y−Y_H)/Δx
            # （除以间距，不是乘——首版写反；另注意间距须 ×2 到全幅尺度）
            cam_h = LANE_W * (y_orig - Y_H) / spacing if y_orig > Y_H + 20 else None
            if cam_h is not None and np.isfinite(cam_h):
                cams.append(cam_h)
                rows_out.append({"row": round(y_orig), "spacing_px": round(spacing, 1),
                                 "cam_h_implied": round(cam_h, 3)})
        med = float(np.median(cams)) if cams else None
        all_cam += cams
        per_frame.append({"file": p.name, "med_cam_h_implied": med,
                          "n_gold_rows": len(rows_out), "rows": rows_out})
        print(f"{p.name}: gold rows {len(rows_out)}, "
              f"cam_h_implied med {med and round(med, 3)}m")

    cam_med = float(np.median(all_cam)) if all_cam else None
    # 权威锚：保险杠相机离地高 1.25m（维护者 2026-11-11，RULES）。
    # 深度金标用权威锚；车道间距反推值作独立交叉核对。
    CAM_H_AUTH = 1.25
    print(f"\n== 车道间距反推 cam_h = {cam_med and round(cam_med, 3)}m "
          f"(权威锚 {CAM_H_AUTH}m, 差 "
          f"{cam_med and round((cam_med / CAM_H_AUTH - 1) * 100, 1)}%) —— 反推值作交叉核对")

    # ── 米制输出模型的偏差系数（金标行上 z_gold / model_metric）──
    # z_gold(y) = FX·CAM_H_AUTH / (y − Y_H)，锚用权威 1.25m
    def z_gold(y):
        return FX * 1.25 / (y - Y_H)

    models = {}
    # moge2_vits metric 头（run_moge 返回 z 已乘 metric_scale×0.893 —— 用原始
    # metric_scale 之前的话需要改适配器；这里直接用适配器输出，系数即总校正）
    import sys
    sys.path.insert(0, str(ROOT))
    import probe_bakeoff as pb  # noqa: E402
    for name, rel, run_fn in [
        ("moge2_vits_fp32", "moge2_vits/model_fp32.onnx", "moge"),
        ("monodepth2_ms640", "monodepth2/mono+stereo_640x192.onnx", "mono"),
    ]:
        try:
            if run_fn == "moge":
                sess = pb.make_session(rel, free_dims={"batch_size": 1,
                                                       "height": 720,
                                                       "width": 1280})
                def metric_map(rgb, s=sess):
                    v, _ = pb.run_moge(s, rgb)  # 已含 0.893
                    return v
            else:
                sess = pb.make_session(rel)
                def metric_map(rgb, s=sess):
                    im = cv2.resize(rgb, (640, 192), interpolation=cv2.INTER_AREA)
                    x = im.astype(np.float32) / 255.0
                    disp = s.run(None, {"images": x.transpose(2, 0, 1)[None]})[0][0, 0]
                    scaled = 0.01 + 0.98 * disp
                    return (1.0 / scaled) * 5.4  # 官方 disp_to_depth × 立体基线
            coefs = []
            for fr, pf in zip(frames, per_frame):
                mm = metric_map(fr)
                mm = pb.resize_to(mm, (720, 1280))
                for r in pf["rows"]:
                    y = r["row"]
                    if not (Y_H + 30 < y < 700):
                        continue
                    row_m = mm[y]
                    row_m = row_m[np.isfinite(row_m)]
                    if len(row_m) < 50:
                        continue
                    zm = float(np.median(row_m))
                    if zm > 0:
                        coefs.append(z_gold(y) / zm)
            coef = float(np.median(coefs)) if coefs else None
            models[name] = {"deviation_coefficient": coef,
                            "n_gold_samples": len(coefs)}
            print(f"{name}: 偏差系数 {coef and round(coef, 4)} "
                  f"(金标样本 {len(coefs)}) → 米制直读 ×{coef and round(coef, 4)}")
        except Exception as e:  # noqa: BLE001
            models[name] = {"error": str(e)[:200]}
            print(f"[FAIL] {name}: {str(e)[:140]}")

    (OUT_DIR / "lane_gold.json").write_text(json.dumps(
        {"cam_h_implied_median": cam_med,
         "n_cam_samples": len(all_cam),
         "per_frame": per_frame, "models": models},
        indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved:", OUT_DIR / "lane_gold.json")


if __name__ == "__main__":
    main()
