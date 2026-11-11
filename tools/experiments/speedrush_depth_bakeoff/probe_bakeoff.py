# -*- coding: utf-8 -*-
"""深度模型对比主探针（第一轮真机段）：准确度（中近场）、速度、延迟。

自包含（不 import maaracing_master）。预处理逐模型对齐各自官方口径：
- Lite-Mono：encoder+decoder 链，640×192 / 1024×320，RGB /255（disparity，大=近）。
- yolo26 n/s/m-depth：768×768 letterbox（0.6 缩放 + 114 灰边），RGB /255。
- MoGe-2 ViT-S（fp32）：生产同款折叠（free-dim override 720×1280）、/255、
  num_tokens=1032；V = points_z × metric_scale × depth_scale(0.893, gate0)。
  与生产 reconstruct 的差：未做 recover_focal_shift 的 shift 校正（全局 z 偏移，
  对中近场平整度/近远序影响可忽略，见 README）。
- DepthART relative MNV4-S/M @448：全帧 squeeze 到 448×448；归一化按输出结构
  自选（/255 与 ImageNet 二选一，退化者弃用）。b/metric 档带自定义算子
  （SelectiveScan），纯 ORT 不可跑——本轮缺席。
- ZipDepth（社区 fp16）：官方口径 h=448 对齐 32、宽 16:9 → 448×800，/255。
- MiDaS v2.1 small：256×256 squeeze，ImageNet 归一化（inverse depth，大=近）。

判据（标尺独立：近远序与超双曲拟合只用 gate0 的地平线 354.2 与车道宽 3.5m，
均为 L0 游戏事实；不拿待验证模型自己生成尺子）：
- 近远序 ρ：中带各列 Spearman(v, V)，V 为「近=小」统一约定。
- 中近场平整度：V(u,v) = C/(v−v_h) + a·u + b 鲁棒拟合（地面投影形状），残差
  p50/p95 与「按纵向动态归一」的无量纲平整度；v_h=354.2（gate0）。
- 路宽定标（仅 MoGe，米制直读）：底部行最宽连续平段，px→m 用归一化焦距
  0.5234（生产 MoGe fx≈670/1280，gate0 notes），期望 ≈14m（4×3.5m）。
- 障碍对比度（撞出租车段 100-106）：中带最小 V / 平面预测地面 V。
- 延迟/速度：DML 纯推理 warmup 5 + 3 全帧 passes，p50/p95/fps。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).parent
W = ROOT / "weights"
FRAMES_DIR = ROOT / "frames" / "20261111_104434_p1"
OUT_DIR = ROOT / "bakeoff_out"

# gate0.json（几何标定真源）与生产常量（本项目生产实现内）：
HORIZON_V = 354.2        # gate0 y_h：地平线行
LANE_W_M = 3.5           # gate0 lane_w_m
DEPTH_SCALE = 0.893      # gate0 depth_scale（MoGe 米制偏大 1.12 的校正）
MOGE_FX_NORM = 670.7 / 1280.0   # 生产 MoGe fx≈670（gate0 notes），归一化 u∈[0,1]
NEAR_LO, NEAR_HI = 430, 640     # 中近场平整度拟合带（720 行口径）
BOTTOM_LO, BOTTOM_HI = 650, 700  # 路宽定标行
CRASH_FRAMES = (100, 106)       # 撞出租车段

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], np.float32)


def make_session(rel: str, static_hw: tuple[int, int] | None = None,
                 free_dims: dict[str, int] | None = None) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if free_dims:
        for name, dim in free_dims.items():
            so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(W / rel), sess_options=so,
                                providers=["DmlExecutionProvider",
                                           "CPUExecutionProvider"])


# ── 各模型适配器：preprocess(帧) → blob；run(blob) → V(near=small 原生分辨率)、valid ──

def _nchw(rgb: np.ndarray, hw: tuple[int, int], imagenet: bool = False) -> np.ndarray:
    im = cv2.resize(rgb, (hw[1], hw[0]), interpolation=cv2.INTER_AREA)
    x = im.astype(np.float32) / 255.0
    if imagenet:
        x = (x - IMAGENET_MEAN) / IMAGENET_STD
    return x.transpose(2, 0, 1)[None]


def _unpad_map(d: np.ndarray, hw_full: tuple[int, int], hw_in: tuple[int, int],
               pad_top: int, pad_left: int) -> np.ndarray:
    """letterbox 输出图 → 回到缩放后有效区。d: (h_in, w_in)。"""
    scale_h, scale_w = hw_in[0] / hw_full[0], hw_in[1] / hw_full[1]
    scale = min(scale_h, scale_w)
    eh, ew = int(round(hw_full[0] * scale)), int(round(hw_full[1] * scale))
    return d[pad_top:pad_top + eh, pad_left:pad_left + ew]


def run_lite_mono(sess_enc, sess_dec, rgb, hw):
    blob = _nchw(rgb, hw)
    feats = sess_enc.run(None, {"input_image": blob})
    d = sess_dec.run(None, {"features_1": feats[0], "features_2": feats[1],
                            "features_3": feats[2]})[0][0, 0]
    return 1.0 / (np.maximum(d, 1e-6)), np.ones_like(d, bool)  # disparity 大=近 → 取倒数


def run_yolo26(sess, rgb):
    # 768×768 letterbox：0.6 缩放 → 768×432 有效区 + 114 灰边（沿用第一轮口径，768 档）
    im = cv2.resize(rgb, (768, 432), interpolation=cv2.INTER_AREA)
    x = im.astype(np.float32) / 255.0
    blob = np.full((1, 3, 768, 768), 114.0 / 255.0, np.float32)
    blob[0, :, :432, :] = x.transpose(2, 0, 1)
    d = sess.run(None, {"images": blob})[0][0, 0]
    d_eff = _unpad_map(d, (720, 1280), (768, 768), 0, 0)
    return d_eff, np.ones_like(d_eff, bool)


MOGE_TOKEN_CACHE: dict[int, int] = {}  # session id → 可用的 num_tokens


def run_moge(sess, rgb):
    blob = _nchw(rgb, (720, 1280))
    has_tokens = any(i.name == "num_tokens" for i in sess.get_inputs())
    cand = [MOGE_TOKEN_CACHE.get(id(sess)), 1032, 577, 513, 257]
    out = None
    for nt in cand:
        if nt is None:
            continue
        feed = {"image": blob, "num_tokens": np.array(nt, np.int64)} \
            if has_tokens else {"image": blob}
        try:
            out = sess.run(None, feed)
            MOGE_TOKEN_CACHE[id(sess)] = nt
            break
        except Exception:  # noqa: BLE001
            continue
    if out is None:
        raise RuntimeError("MoGe run failed (num_tokens candidates exhausted)")
    pts = out[0][0]
    mask = out[2][0] if len(out) > 2 else np.ones(pts.shape[:2], bool)
    scale = float(out[3][0]) if len(out) > 3 else 1.0
    z = pts[..., 2] * scale * DEPTH_SCALE
    mask = (mask > 0.5) & np.isfinite(z)
    return z, mask


def run_depthart(sess, rgb, imagenet: bool):
    blob = _nchw(rgb, (448, 448), imagenet=imagenet)
    d = sess.run(None, {"image": blob})[0][0, 0]
    return d, np.ones_like(d, bool)


def run_zipdepth(sess, rgb):
    im = cv2.resize(rgb, (800, 448), interpolation=cv2.INTER_LINEAR)
    x = im.astype(np.float32) / 255.0
    d = sess.run(None, {"pixel_values": x.transpose(2, 0, 1)[None]})[0][0, 0]
    return d, np.ones_like(d, bool)


def run_midas(sess, rgb):
    blob = _nchw(rgb, (256, 256), imagenet=True)
    d = sess.run(None, {"0": blob})[0]
    if d.ndim == 3 and d.shape[0] == 1:
        d = d[0]
    d = np.maximum(d, 0.0)
    return 1.0 / (d + 1e-6), np.ones_like(d, bool)  # inverse depth 大=近 → 取倒数


def to_near_small(v: np.ndarray) -> np.ndarray:
    """未知方向的相对输出 → 统一「近=小」约定：底部行中位 < 近地平线行中位
    ⇒ 大=近（disparity 向）⇒ 取倒数（下限取 1 分位，防 0/eps 打爆）；
    否则已近=小原样。原始 ≤0（远场饱和 0，如 ZipDepth 天空带）按无效 NaN——
    截断会把饱和区变成同一平顶值 + 硬边界（热图呈二值化假象）。"""
    h = v.shape[0]
    bottom = np.nanmedian(np.where(v <= 0, np.nan, v)[int(h * 0.9):])
    upper = np.nanmedian(np.where(v <= 0, np.nan, v)[int(h * 0.55):int(h * 0.7)])
    if bottom > upper:
        pos = np.where(v <= 0, np.nan, v)
        floor = float(np.nanpercentile(pos, 1))
        return 1.0 / np.maximum(pos, max(floor, 1e-9))
    return v


def resize_to(v: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    if v.shape[:2] == hw:
        return v
    return cv2.resize(v, (hw[1], hw[0]), interpolation=cv2.INTER_LINEAR)


# ── 准确度判据 ──

def spearman_cols(v: np.ndarray, valid: np.ndarray) -> float:
    """中带各列 Spearman(v, V) 取负：行号向下增大=更近、V（近=小）更小，
    正确的地面序即 ρ_order ≈ +1（原始 spearman ≈ −1）。"""
    from scipy.stats import spearmanr
    h, w = v.shape
    c0, c1 = int(w * 0.3), int(w * 0.7)
    r0, r1 = max(int(HORIZON_V) + 16, 0), min(int(h * 0.96), 690)
    vals = []
    for c in range(c0, c1, 4):
        col_v = v[r0:r1, c]
        col_ok = valid[r0:r1, c] & np.isfinite(col_v)
        if col_ok.sum() < 40:
            continue
        rows = np.arange(r0, r1)[col_ok]
        rho = spearmanr(rows, col_v[col_ok]).statistic
        if np.isfinite(rho):
            vals.append(rho)
    return -float(np.median(vals)) if vals else float("nan")


def ground_fit(v: np.ndarray, valid: np.ndarray) -> dict:
    """V(u,v) = C/(v−v_h) + a·u + b 鲁棒拟合（地面投影形状）。"""
    h, w = v.shape
    vv0, vv1 = NEAR_LO, min(NEAR_HI, h)
    g = 1.0 / (np.arange(h, dtype=np.float64) - HORIZON_V)   # 每行标量（h,）
    u = (np.arange(w, dtype=np.float64) + 0.5) / w           # 每列标量（w,）
    sel = np.zeros_like(v, bool)
    sel[vv0:vv1] = valid[vv0:vv1] & np.isfinite(v[vv0:vv1])
    ys, xs = np.nonzero(sel)
    if len(ys) < 2000:
        return {}
    A = np.stack([g[ys], u[xs], np.ones(len(ys))], 1)
    y = v[ys, xs].astype(np.float64)
    for _ in range(3):
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = A @ coef - y
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        keep = np.abs(resid - np.median(resid)) < 2.5 * mad
        A, y = A[keep], y[keep]
    resid = A @ coef - y
    lo_v = np.median(v[vv0:vv1][valid[vv0:vv1]])
    hi_v = np.nanmedian(v[min(h - 1, BOTTOM_HI)])
    dyn = abs(hi_v - lo_v) + 1e-9
    p50, p95 = np.percentile(np.abs(resid - np.median(resid)), [50, 95])
    return {"resid_p50": float(p50), "resid_p95": float(p95),
            "flatness_norm": float(p50 / dyn), "pitch_u": float(coef[1]),
            "coef": [float(coef[0]), float(coef[1]), float(coef[2])],
            "n_fit": int(len(y))}


def road_width_m(v: np.ndarray, valid: np.ndarray, fx_norm: float,
                 full_w: int) -> float | None:
    """底部行最宽连续平段 → px → 米（backprojection 口径）。仅对已「近=小」
    且近似米制的 V 有意义（MoGe 主用；相对模型给 px 宽参考）。"""
    widths = []
    for r in range(BOTTOM_LO, min(BOTTOM_HI, v.shape[0])):
        row = v[r].astype(np.float64)
        ok = valid[r] & np.isfinite(row)
        if ok.sum() < 100:
            continue
        med = np.median(row[ok])
        mad = np.median(np.abs(row[ok] - med)) * 1.4826 + 1e-6
        flat = ok & (np.abs(row - med) < 3.0 * mad)
        # 最宽连续段
        best, run, start, best_iv = 0, 0, 0, (0, 0)
        for i in range(row.size):
            if flat[i]:
                run += 1
                if run > best:
                    best, best_iv = run, (i - run + 1, i + 1)
            else:
                run = 0
        if best >= 80:
            widths.append(best)
    if not widths:
        return None
    wpx = float(np.median(widths))
    z_mean = float(np.nanmean(v[BOTTOM_LO:BOTTOM_HI][valid[BOTTOM_LO:BOTTOM_HI]]))
    return wpx * z_mean / (fx_norm * full_w)


def obstacle_contrast(v: np.ndarray, valid: np.ndarray, fit: dict) -> float | None:
    """中带最小 V / 拟合地面预测 V（<1 表示近障碍占据地面预期域，撞车段用）。"""
    if not fit or "coef" not in fit:
        return None
    h, w = v.shape
    reg = np.zeros_like(v, bool)
    reg[500:min(700, h), int(w * 0.3):int(w * 0.7)] = True
    sel = reg & valid & np.isfinite(v)
    if sel.sum() < 500:
        return None
    C, a, b = fit["coef"]
    vv = np.arange(h, dtype=np.float64)[:, None]
    uu = (np.arange(w, dtype=np.float64)[None, :] + 0.5) / w
    pred = C / (vv - HORIZON_V) + a * uu + b
    sel_v = v[sel].astype(np.float64)
    sel_p = pred[sel]
    ok = np.isfinite(sel_v) & (sel_p > 0)
    if ok.sum() < 500:
        return None
    return float(np.min(sel_v[ok]) / np.median(sel_p[ok]))


# ── 延迟测量 ──

def bench(fn, frames, warmup=5, passes=3):
    for i in range(warmup):
        fn(frames[0])
    t_fwd, t_full = [], []
    for _ in range(passes):
        for fr in frames:
            t0 = time.perf_counter()
            fn(fr)
            t_full.append((time.perf_counter() - t0) * 1000.0)
    return t_full


def pct(a, q):
    return float(np.percentile(a, q)) if a else float("nan")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    frames = [cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
              for p in sorted(FRAMES_DIR.glob("*.jpg"))]
    print(f"frames: {len(frames)} @ {frames[0].shape}")

    specs = [
        ("lite_mono_640x192", "lite_mono_base_640x192", (192, 640)),
        ("lite_mono_1024x320", "lite_mono_base_1024x320", (320, 1024)),
        ("yolo26n", "yolo26_depth/yolo26n-depth.onnx", None),
        ("yolo26s", "yolo26_depth/yolo26s-depth.onnx", None),
        ("yolo26m", "yolo26_depth/yolo26m-depth.onnx", None),
        ("moge2_vits_fp32", "moge2_vits/model_fp32.onnx", None),
        ("depthart_mnv4s_448", "depthart/relative_mnv4s_448_default.onnx", (448, 448)),
        ("depthart_mnv4m_448", "depthart/relative_mnv4m_448_default.onnx", (448, 448)),
        ("zipdepth_fp16", "zipdepth/zipdepth_fp16_opset17.onnx", (448, 800)),
        ("midas_small", "midas/model-small.onnx", (256, 256)),
    ]

    results = {}
    for name, rel, hw in specs:
        entry: dict = {"weights": rel}
        try:
            if name.startswith("lite_mono"):
                if name == "lite_mono_640x192":
                    hw_lm = (192, 640)
                    enc = make_session("lite_mono_base_640x192/encoder.onnx")
                    dec = make_session("lite_mono_base_640x192/decoder.onnx")
                else:
                    hw_lm = (320, 1024)
                    enc = make_session("lite_mono_base_1024x320/encoder.onnx")
                    dec = make_session("lite_mono_base_1024x320/decoder.onnx")
                fn = lambda rgb, e=enc, d=dec, h_=hw_lm: run_lite_mono(e, d, rgb, h_)
            elif name.startswith("yolo26"):
                sess = make_session(rel)
                fn = lambda rgb, s=sess: run_yolo26(s, rgb)
            elif name.startswith("moge2"):
                sess = make_session(rel, free_dims={"batch_size": 1, "height": 720,
                                                    "width": 1280})
                fn = lambda rgb, s=sess: run_moge(s, rgb)
            elif name.startswith("depthart"):
                sess = make_session(rel)
                inorm = _pick_depthart_norm(sess)
                entry["imagenet_norm"] = inorm
                fn = lambda rgb, s=sess, i=inorm: run_depthart(s, rgb, i)
            elif name.startswith("zipdepth"):
                sess = make_session(rel)
                fn = lambda rgb, s=sess: run_zipdepth(s, rgb)
            else:  # midas
                sess = make_session(rel)
                fn = lambda rgb, s=sess: run_midas(s, rgb)

            # 延迟（纯链：preprocess+forward+post；DML 纯推理另列见 README 口径）
            t = bench(fn, frames)
            entry["latency_ms"] = {"p50": pct(t, 50), "p95": pct(t, 95),
                                   "fps_p50": 1000.0 / pct(t, 50)}

            # 准确度（18 帧逐帧）
            accs = []
            for idx, fr in enumerate(frames, start=89):
                v_raw, valid = fn(fr)
                v = to_near_small(v_raw)
                v = resize_to(v, (720, 1280))
                valid = resize_to(valid.astype(np.float32),
                                  (720, 1280)) > 0.5
                fit = ground_fit(v, valid)
                rec = {"frame": idx, "rho_nearfar": spearman_cols(v, valid)}
                if fit:
                    rec.update({k: fit[k] for k in
                                ("resid_p50", "resid_p95", "flatness_norm")})
                oc = obstacle_contrast(v, valid, fit) if fit else None
                if oc is not None:
                    rec["obstacle_contrast"] = oc
                if name == "moge2_vits_fp32":
                    rec["road_width_m"] = road_width_m(v, valid, MOGE_FX_NORM, 1280)
                accs.append(rec)
            entry["accuracy"] = accs
            entry["accuracy_median"] = {
                "rho_nearfar": float(np.nanmedian([a["rho_nearfar"] for a in accs])),
                "flatness_norm": float(np.nanmedian([a.get("flatness_norm", np.nan)
                                                     for a in accs])),
                "resid_p50": float(np.nanmedian([a.get("resid_p50", np.nan)
                                                 for a in accs])),
            }
            if name == "moge2_vits_fp32":
                rws = [a.get("road_width_m") for a in accs if a.get("road_width_m")]
                entry["road_width_m_median"] = float(np.median(rws)) if rws else None
            print(f"[OK] {name}: lat p50 {entry['latency_ms']['p50']:.1f}ms  "
                  f"rho {entry['accuracy_median']['rho_nearfar']:.3f}  "
                  f"flat {entry['accuracy_median']['flatness_norm']:.4f}")
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)[:300]
            print(f"[FAIL] {name}: {str(e)[:160]}")
        results[name] = entry

    (OUT_DIR / "bakeoff_results.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved:", OUT_DIR / "bakeoff_results.json")


def _pick_depthart_norm(sess) -> bool:
    """DepthART 归一化自选：/255 与 ImageNet 中取输出结构非退化者。"""
    probe = np.random.RandomState(0).randint(0, 255, (448, 448, 3), np.uint8)
    for imagenet in (False, True):
        try:
            d, _ = run_depthart(sess, probe, imagenet)
            if np.nanstd(d) > 1e-3:
                return imagenet
        except Exception:  # noqa: BLE001
            continue
    return False


if __name__ == "__main__":
    main()
