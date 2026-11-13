# -*- coding: utf-8 -*-
"""DepthART 能力探针：官方预处理下三档可跑变体的结构/校准/稳定性/延迟。

**本轮要回答的**（不是选型结论，结论跑完写 README）：
1. 官方预处理（RGB /255 + ImageNet 归一化 + INTER_CUBIC squeeze + 输出
   bilinear align_corners 回原尺寸）与旧探针的 /255 自选归一化差多少；
2. 三档可跑变体（MNV4-S / MNV4-M / MNV4-M-slim-SPF）的输出方向（近=大还是
   近=小）与地面校准残差——M 档在旧轮被判「退化」，本轮用官方预处理复核；
3. 逐帧地面模型残差、障碍对比度、纯推理与全链延迟。

**判据口径**：
- 地面模型按输出方向分两种：近=大（视差向）V=A+m·(r−y_h)+a·u；
  近=小（深度向）V=A+m/(r−y_h)+a·u。残差按中近场动态范围归一。
- 障碍对比度：中带内像素相对同帧地面模型的最大「更近」偏离，按动态范围
  归一（越大=车/障碍越突出）。
- 延迟：DML 纯推理 p50 与 预处理+推理+回原尺寸 全链 p50（warmup 5 + 3 passes）。
- 地平线 y_h=354.2（gate0），只作形状参照，不参与尺度结论。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).parent
WD = ROOT / "weights" / "depthart"
OUT = ROOT / "capability_out"
YH = 354.2
RES = 448
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
VARIANTS = [
    ("mnv4s", "relative_mnv4s_448_default.onnx"),
    ("mnv4m", "relative_mnv4m_448_default.onnx"),
    ("slim_spf", "relative_mnv4m_slim_spf_448_default.onnx"),
]


def list_frames() -> list[Path]:
    frames = sorted((ROOT / "frames").glob("*_raw.jpg"))
    for d in sorted((ROOT / "frames").iterdir()):
        if d.is_dir():
            frames.extend(sorted(d.glob("*_raw.jpg")))
    return frames


def prep(rgb: np.ndarray, imagenet: bool = True) -> np.ndarray:
    im = cv2.resize(rgb, (RES, RES), interpolation=cv2.INTER_CUBIC)
    x = im.astype(np.float32) / 255.0
    if imagenet:
        x = (x - MEAN) / STD
    return x.transpose(2, 0, 1)[None]


def resize_ac(img: np.ndarray, h: int, w: int) -> np.ndarray:
    """bilinear + align_corners（对齐官方 F.interpolate 口径）。"""
    sh, sw = img.shape[:2]
    ys = np.linspace(0, sh - 1, h)
    xs = np.linspace(0, sw - 1, w)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, sh - 1), np.minimum(x0 + 1, sw - 1)
    wy, wx = (ys - y0)[:, None], (xs - x0)[None, :]
    a = img[y0][:, x0] * (1 - wx) + img[y0][:, x1] * wx
    b = img[y1][:, x0] * (1 - wx) + img[y1][:, x1] * wx
    return a * (1 - wy) + b * wy


def _robust(coef, A, y, iters=4):
    for _ in range(iters):
        resid = A @ coef - y
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        keep = np.abs(resid - np.median(resid)) < 2.5 * mad
        coef = np.linalg.lstsq(A[keep], y[keep], rcond=None)[0]
    return coef


def ground_model(v: np.ndarray, near_large: bool):
    """拟合地面模型，返回 coef / 归一化残差 / 动态范围。"""
    h, w = v.shape
    rows = np.arange(h, dtype=np.float64)
    band = (rows >= 430) & (rows <= 670)
    ys, xs = np.nonzero(band[:, None] & np.ones((h, w), bool))
    u = (np.arange(w, dtype=np.float64) + 0.5) / w
    term = (ys - YH) if near_large else 1.0 / np.maximum(ys - YH, 1.0)
    A = np.stack([term, u[xs], np.ones(len(ys))], 1)
    y = v[ys, xs]
    coef = _robust(np.linalg.lstsq(A, y, rcond=None)[0], A, y)
    resid = A @ coef - y
    dyn = abs(np.median(v[640:700]) - np.median(v[360:420])) + 1e-9
    return coef, (float(np.percentile(np.abs(resid - np.median(resid)), 50)) / dyn,
                  float(np.percentile(np.abs(resid - np.median(resid)), 95)) / dyn,
                  float(dyn))


def obstacle_dev(v: np.ndarray, coef, near_large: bool) -> float:
    """中带（500~700 行、中间 40% 列）相对地面模型的「更近」偏离 p95。"""
    h, w = v.shape
    rows = np.arange(h, dtype=np.float64)
    term = (rows - YH) if near_large else 1.0 / np.maximum(rows - YH, 1.0)
    u = (np.arange(w, dtype=np.float64) + 0.5) / w
    r0, r1 = 500, min(700, h)
    c0, c1 = int(w * 0.3), int(w * 0.7)
    pred = (coef[0] * term[r0:r1, None] + coef[1] * u[None, c0:c1]
            + coef[2])
    dev = (v[r0:r1, c0:c1] - pred) * (1.0 if near_large else -1.0)
    dyn = abs(np.median(v[640:700]) - np.median(v[360:420])) + 1e-9
    return float(np.percentile(dev, 95) / dyn)


def color(v: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(v[np.isfinite(v)], (2, 98))
    x = np.clip((v - lo) / max(hi - lo, 1e-9), 0, 1)
    return cv2.applyColorMap((x * 255).astype(np.uint8), cv2.COLORMAP_TURBO)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    frames = list_frames()
    print(f"frames: {len(frames)}")
    rgbs = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB) for p in frames]
    results: dict = {}
    overlay_frames = [0, 5, 10, 15]
    for tag, name in VARIANTS:
        so = ort.SessionOptions()
        so.log_severity_level = 3
        sess = ort.InferenceSession(str(WD / name), sess_options=so,
                                    providers=["DmlExecutionProvider",
                                               "CPUExecutionProvider"])
        # 方向与归一化 A/B（前 12 帧）
        dirs, norm_ab = [], []
        for rgb in rgbs[:12]:
            for imagenet in (True, False):
                out = sess.run(None, {"image": prep(rgb, imagenet)})[0][0, 0]
                v = resize_ac(out.astype(np.float64), rgb.shape[0], rgb.shape[1])
                nl = np.median(v[640:700]) > np.median(v[360:420])
                _, res = ground_model(v, nl)
                norm_ab.append((imagenet, res[0]))
                if imagenet:
                    dirs.append(nl)
        near_large = sum(dirs) >= len(dirs) / 2
        res_in = [r for i, r in norm_ab if i]
        res_255 = [r for i, r in norm_ab if not i]

        # 全量逐帧
        accs = []
        for i, rgb in enumerate(rgbs):
            h, w = rgb.shape[:2]
            t0 = time.perf_counter()
            out = sess.run(None, {"image": prep(rgb)})[0][0, 0]
            t_infer = (time.perf_counter() - t0) * 1000.0
            v = resize_ac(out.astype(np.float64), h, w)
            t_full = (time.perf_counter() - t0) * 1000.0
            coef, (p50, p95, _) = ground_model(v, near_large)
            accs.append({"i": i, "infer_ms": t_infer, "full_ms": t_full,
                         "res_p50": p50, "res_p95": p95,
                         "obstacle_dev": obstacle_dev(v, coef, near_large)})
            if i in overlay_frames:
                c = color(v)
                cv2.putText(c, f"{tag} {'near=large' if near_large else 'near=small'}",
                            (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                            (255, 255, 255), 2)
                cv2.imwrite(str(OUT / f"{tag}_f{i:03d}.jpg"), c)
        # 空载计时（warmup+passes）
        warm = rgbs[:1]
        for _ in range(5):
            sess.run(None, {"image": prep(warm[0])})
        ts = []
        for _ in range(3):
            for rgb in rgbs[:20]:
                t0 = time.perf_counter()
                sess.run(None, {"image": prep(rgb)})
                ts.append((time.perf_counter() - t0) * 1000.0)
        results[tag] = {
            "weights": name,
            "near_large": bool(near_large),
            "norm_ab_res_p50_imagenet": float(np.median(res_in)),
            "norm_ab_res_p50_div255": float(np.median(res_255)),
            "full_ms_p50": float(np.percentile(ts, 50)),
            "full_ms_p95": float(np.percentile(ts, 95)),
            "res_p50_median": float(np.median([a["res_p50"] for a in accs])),
            "res_p95_median": float(np.median([a["res_p95"] for a in accs])),
            "obstacle_dev_p50": float(np.median([a["obstacle_dev"] for a in accs])),
            "frames": accs,
        }
        print(f"[{tag}] near_large={near_large} "
              f"norm p50 imagenet/255 = {np.median(res_in):.5f}/{np.median(res_255):.5f} "
              f"res p50={np.median([a['res_p50'] for a in accs]):.5f} "
              f"obstacle={np.median([a['obstacle_dev'] for a in accs]):.3f} "
              f"full p50={np.percentile(ts, 50):.1f}ms")
    (OUT / "capability.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved:", OUT / "capability.json")


if __name__ == "__main__":
    main()
