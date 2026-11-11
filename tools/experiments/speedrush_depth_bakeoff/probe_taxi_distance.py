# -*- coding: utf-8 -*-
"""融合探针：锁定出租车测距（生产同款：检测框 × 深度图 → 框底带中位距离）。

生产口径（复刻，不 import maaracing_master）：
- 检测：perception/model.onnx（近场复训版，3 类，640 letterbox + 114 灰边、
  /255、NCHW、(8400,4+n_cls) 解码、类 conf 0.35、NMS 0.45、ROI (0,201,1280,561)）
  ——方法论文本复刻自 speedrush_vision/probe_old_model.py。
- 距离：_box_anchor_z 口径（2026-11-04 修正版）——框底条带（底部 25% 行）
  中位 = 车距；背景只落在框上部行天然排除。
- 出租车跟踪：从最后一帧（0106，贴上出租车，框最大最居中）向前逐帧最近中心
  匹配（IoU>0.2 且中心距 < 0.25 帧宽），要求 car 类。

判据（标尺独立）：
- MoGe（生产 q4f16 权重）：米制直读 × depth_scale(0.893, gate0)，绝对值对照
  41m/s × Δt（frames.jsonl 真时戳，HUD 得分速度）。
- 相对模型：尺 = 干净帧（0089~0093）上「路缘两侧都进画面」的行的全幅路宽
  （14m，RULES 四车道 × 3.5m 画线）——不再用底部行（那里路宽超出画面两边，
  量到的是画幅截断）。乘法换算对 disparity 向模型是近似，单列 caveat。
- 一致性：各模型距离序列按帧 0089 归一后对比衰减形状；单调降到 ~0。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

import sys
sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402  复用 make_session/各模型 run/近=小 换算

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "bakeoff_out"
FRAMES_DIR = ROOT / "frames" / "20261111_104434_p1"
FRAMES_JSONL = Path(r"C:/Users/yomen/AppData/Roaming/MaaRacingMaster/debug/"
                    "20261111_103853/speedrush/drive/20261111_104434_p1/frames.jsonl")

IMGSZ = 640
CLASSES = {0: "coin", 1: "car", 2: "bonus_car"}
CONF = 0.35
ROI = (0, 201, 1280, 561)
DEPTH_SCALE = 0.893          # gate0 depth_scale（MoGe 米制校正）
ROAD_FULL_M = 14.0           # RULES 四车道 × 3.5m 画线
SPEED_MS = 41.0              # HUD 得分速度（维护者口径）
CLEAN_FRAMES = (89, 93)      # 干净路面帧（路宽尺）


def detect_cars(sess, rgb: np.ndarray):
    """perception 模型 car 检测（方法论文本复刻 probe_old_model.detect）。"""
    x1, y1, x2, y2 = ROI
    crop, ox, oy = rgb[y1:y2, x1:x2], x1, y1
    h, w = crop.shape[:2]
    scale = min(IMGSZ / h, IMGSZ / w)
    nh, nw = int(h * scale), int(w * scale)
    px, py = (IMGSZ - nw) // 2, (IMGSZ - nh) // 2
    padded = np.full((IMGSZ, IMGSZ, 3), 114, dtype=np.uint8)
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
    boxes = []
    for i in np.array(keep).ravel():
        a1, b1, a2, b2 = xy[int(i)]
        boxes.append((int((a1 - px) / scale + ox), int((b1 - py) / scale + oy),
                      int((a2 - px) / scale + ox), int((b2 - py) / scale + oy)))
    return boxes


def track_taxi(dets_per_frame):
    """种子帧 = 全帧中面积最大的 car 框（贴脸帧检测器可能失检）；向前后
    双向最近中心匹配（中心距 < 0.25 帧宽且框高不暴涨），返回每帧框或 None。"""
    n = len(dets_per_frame)

    def area(b): return (b[2] - b[0]) * (b[3] - b[1])
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
    for i in range(seed_i - 1, -1, -1):          # 向前（更早帧）
        if cur is None:
            break
        cur = _match(dets_per_frame[i], cur)
        track[i] = cur
    cur = track[seed_i]
    for i in range(seed_i + 1, n):               # 向后（更晚帧）
        if cur is None:
            break
        cur = _match(dets_per_frame[i], cur)
        track[i] = cur
    return track


def _match(dets, cur):
    """最近中心匹配：|Δcx| < 0.25 帧宽、框高 < 1.6×当前（不接更大近物）。"""
    cx, cy = (cur[0] + cur[2]) / 2, (cur[1] + cur[3]) / 2
    h_cur = cur[3] - cur[1]
    best, best_d = None, 1e9
    for b in dets:
        bx, by = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        d = abs(bx - cx) + abs(by - cy) * 2
        h_b = b[3] - b[1]
        if abs(bx - cx) < 0.25 * 1280 and h_b < h_cur * 1.6 and d < best_d:
            best, best_d = b, d
    return best


def band_z(v: np.ndarray, box, min_pts: int = 5) -> float | None:
    """_box_anchor_z 口径：框底条带（底部 25% 行）中位。box 在 1280×720 系。"""
    gh, gw = v.shape[:2]
    x0 = max(0, int(box[0] * gw / 1280)); y0 = max(0, int(box[1] * gh / 720))
    x1 = min(gw, int(box[2] * gw / 1280)); y1 = min(gh, int(box[3] * gh / 720))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    yb = y0 + (y1 - y0) * 3 // 4
    zv = v[yb:y1, x0:x1]
    zv = zv[np.isfinite(zv)]
    if zv.size < min_pts:
        return None
    return float(np.median(zv))


def road_full_width_px(v: np.ndarray, valid: np.ndarray) -> float | None:
    """「路缘两侧都进画面」的行的平段宽（全幅 14m 尺）：从下往上找第一行，
    其平段两端都在画面内（两端各有一处高度不连续），宽度即全幅路宽。"""
    h, w = v.shape
    for r in range(min(700, h - 1), 380, -1):
        row = v[r].astype(np.float64)
        ok = valid[r] & np.isfinite(row)
        if ok.sum() < 100:
            continue
        med = np.median(row[ok])
        mad = np.median(np.abs(row[ok] - med)) * 1.4826 + 1e-9
        flat = ok & (np.abs(row - med) < 3.0 * mad)
        # 最宽连续平段
        best_len, best_iv, run, start = 0, None, 0, 0
        for i in range(w):
            if flat[i]:
                if run == 0:
                    start = i
                run += 1
                if run > best_len:
                    best_len, best_iv = run, (start, i + 1)
            else:
                run = 0
        if best_iv is None or best_len < 120:
            continue
        a, b = best_iv
        # 两端都在画面内（左端 > 2% 宽、右端 < 98% 宽）⇒ 路缘两侧都进画面
        if a > 0.02 * w and b < 0.98 * w:
            return float(b - a)
    return None


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    files = sorted(FRAMES_DIR.glob("*_raw.jpg"))
    frames = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in files]
    # 真时戳（frames.jsonl，按 file 名对齐）
    ts = {}
    for line in FRAMES_JSONL.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        ts[r["file"].split("/")[-1]] = r["ts_ns"] / 1e9
    t = [ts[p.name.replace("_raw.jpg", "_raw.jpg")] for p in files]
    print(f"frames {len(frames)}, dt p50 "
          f"{np.median(np.diff(t)) * 1000:.0f}ms, "
          f"expected drop 0089->0106 ≈ {SPEED_MS * (t[-1] - t[0]):.0f}m")

    # 检测 + 跟踪
    det_sess = pb.make_session("prod_perception/model.onnx")
    dets = [detect_cars(det_sess, fr) for fr in frames]
    track = track_taxi(dets)
    n_tracked = sum(1 for b in track if b)
    print(f"taxi tracked {n_tracked}/{len(frames)} frames; "
          f"last box {track[-1]}")

    # 深度模型（生产 MoGe q4f16 + 全部候选）
    depth_specs = [
        ("moge_prod_q4f16", "prod_moge_q4f16.onnx", None, True),
        ("moge2_vits_fp32", "moge2_vits/model_fp32.onnx", None, False),
        ("zipdepth_fp16", "zipdepth/zipdepth_fp16_opset17.onnx", (448, 800), False),
        ("depthart_mnv4s_448", "depthart/relative_mnv4s_448_default.onnx", (448, 448), False),
        ("depthart_mnv4m_448", "depthart/relative_mnv4m_448_default.onnx", (448, 448), False),
        ("yolo26n", "yolo26_depth/yolo26n-depth.onnx", None, False),
        ("yolo26m", "yolo26_depth/yolo26m-depth.onnx", None, False),
        ("lite_mono_640x192", "lite_mono_base_640x192", (192, 640), False),
        ("midas_small", "midas/model-small.onnx", (256, 256), False),
    ]
    results = {"frames": [int(p.name[:4]) for p in files],
               "ts": t, "track": track, "models": {}}

    for name, rel, hw, is_prod_moge in depth_specs:
        try:
            if name.startswith("moge"):
                sess = pb.make_session(rel, free_dims={"batch_size": 1,
                                                       "height": 720,
                                                       "width": 1280})
                run = (lambda rgb, s=sess: pb.run_moge(s, rgb))
            elif name.startswith("lite_mono"):
                enc = pb.make_session(rel + "/encoder.onnx")
                dec = pb.make_session(rel + "/decoder.onnx")
                run = (lambda rgb, e=enc, d=dec, h_=hw:
                       pb.run_lite_mono(e, d, rgb, h_))
            elif name.startswith("yolo26"):
                sess = pb.make_session(rel)
                run = (lambda rgb, s=sess: pb.run_yolo26(s, rgb))
            elif name.startswith("zipdepth"):
                sess = pb.make_session(rel)
                run = (lambda rgb, s=sess: pb.run_zipdepth(s, rgb))
            elif name.startswith("depthart"):
                sess = pb.make_session(rel)
                run = (lambda rgb, s=sess: pb.run_depthart(s, rgb, False))
            else:
                sess = pb.make_session(rel)
                run = (lambda rgb, s=sess: pb.run_midas(s, rgb))

            # 距离序列（模型单位）
            series, maps = [], {}
            for fr, box in zip(frames, track):
                if box is None:
                    series.append(None)
                    continue
                v_raw, valid = run(fr)
                v = pb.to_near_small(v_raw)
                v = pb.resize_to(v, (720, 1280))
                valid = pb.resize_to(valid.astype(np.float32),
                                     (720, 1280)) > 0.5
                maps.setdefault("v", []).append(v)
                z = band_z(v, box)
                # 注意：pb.run_moge 适配器内部已乘 gate0 depth_scale 0.893（单入口，
                # 与生产 depth_geo 推理同口径），这里不再重复乘（双重校正会使
                # 生产序列偏小 ~10%，2026-11-11 校准对齐时发现）。
                if False and is_prod_moge and z is not None:
                    z *= DEPTH_SCALE
                series.append(z)

            # 路宽尺（干净帧，全幅路宽 → 模型单位换算）
            rw = []
            if not is_prod_moge:
                for idx in range(len(frames)):
                    fi = int(files[idx].name[:4])
                    if not (CLEAN_FRAMES[0] <= fi <= CLEAN_FRAMES[1]):
                        continue
                    v_raw, valid = run(frames[idx])
                    v = pb.to_near_small(v_raw)
                    v = pb.resize_to(v, (720, 1280))
                    valid = pb.resize_to(valid.astype(np.float32),
                                         (720, 1280)) > 0.5
                    wpx = road_full_width_px(v, valid)
                    if wpx:
                        zmid = float(np.nanmedian(v[500:640]))
                        rw.append((fi, wpx, zmid))
                if rw:
                    print(f"  road_full_width_px: "
                          f"{[(fi, round(w)) for fi, w, _ in rw]}")
            scale_m = None
            if rw and not is_prod_moge:
                # 路宽行上宽度 wpx 对应 14m ⇒ m/单位 = 14 / (wpx·zmid/(fx·W))
                fx = pb.MOGE_FX_NORM
                ests = [ROAD_FULL_M / (wpx * zmid / (fx * 1280.0))
                        for fi, wpx, zmid in rw]
                scale_m = float(np.median(ests))

            m_series = []
            for z in series:
                if z is None:
                    m_series.append(None)
                elif is_prod_moge:
                    m_series.append(z)
                elif scale_m:
                    m_series.append(z * scale_m)
                else:
                    m_series.append(None)
            ok = [x for x in m_series if x is not None]
            results["models"][name] = {
                "scale_m_per_unit": scale_m if not is_prod_moge else "metric",
                "series": m_series,
                "drop": (ok[0] - ok[-1]) if len(ok) >= 2 else None,
                "expected_drop": SPEED_MS * (t[-1] - t[0]),
                "monotone": all((m_series[i + 1] is None or m_series[i] is None
                                 or m_series[i + 1] <= m_series[i] * 1.15)
                                for i in range(len(m_series) - 1)),
            }
            print(f"[OK] {name}: scale={results['models'][name]['scale_m_per_unit']} "
                  f"drop={results['models'][name]['drop']} "
                  f"(期望 {results['models'][name]['expected_drop']:.0f}m) "
                  f"first3={[None if x is None else round(x,1) for x in m_series[:3]]} "
                  f"last3={[None if x is None else round(x,1) for x in m_series[-3:]]}")
        except Exception as e:  # noqa: BLE001
            results["models"][name] = {"error": str(e)[:300]}
            print(f"[FAIL] {name}: {str(e)[:160]}")

    (OUT_DIR / "taxi_distance.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved:", OUT_DIR / "taxi_distance.json")


if __name__ == "__main__":
    main()
