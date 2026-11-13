# -*- coding: utf-8 -*-
"""DepthART 输出边缘锐度归因：模型固有 vs 上采样 vs 度量选错了位置。

**要回答**：此前说「自由空间边界糊 2 米」——到底是模型糊、我们上采样糊，还是
**量的位置本身没有深度跳变**？三种量法互相印证：

1. **接触线**（车胎接地列）：`V−V_g` 的竖直过渡带。车底与车下路面深度连续，
   几何上就没有跳变，这里的「过渡宽度」是路面自身深度梯度的长度，不是模糊。
2. **硬不连续**（车顶 vs 背景）：同一列跨车顶，10%→90% 的升幅跨多少行。这一处
   真实深度差很大，模型若锐就该是 1~3 行（448 网格）。
3. **网络内部上采样痕迹**：沿行做二阶差分，看是否以 2/4 行为周期振荡；若是，
   说明网络内部就是低分辨率预测 + 双线性放大，锐度上限已定。

同时打印 ONNX 输入输出形状，并导出 448 原图与 720 回原尺寸的着色深度图，供与
论文图目视比对。变体对照 slim-SPF vs MNV4-S。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import probe_lane_geometry as lg  # noqa: E402
import probe_depthart_prod_sim as sim  # noqa: E402

ROOT = Path(__file__).parent
OUT = ROOT / "boundary_blur_out"
FY = 630.0
CAM_H = 1.25
TAU = 0.05
VARIANTS = {
    "slim": "depthart/relative_mnv4m_slim_spf_448_default.onnx",
    "mnv4s": "depthart/relative_mnv4s_448_default.onnx",
}
FRAMES = [
    ("car8m", ROOT / "frames" / "20261109_225459_p2_stable41" / "0155_raw.jpg", None, None),
    ("car13m", ROOT / "frames" / "20261110_205224_p2_stable41" / "0081_raw.jpg",
     (199, 353, 307, 429), None),
    ("taxi16m", ROOT / "frames" / "0104_raw.jpg", (602, 347, 674, 410), 361.2),
]


def io_probe() -> dict:
    info = {}
    for name, rel in VARIANTS.items():
        s = sim.session(rel)
        info[name] = {
            "in": [[i.name, list(i.shape), i.type] for i in s.get_inputs()],
            "out": [[o.name, list(o.shape), o.type] for o in s.get_outputs()],
        }
    print(json.dumps(info, indent=1, ensure_ascii=False))
    return info


def fit_ground(v, yh, excl, r_lo, r_hi, stride=2):
    h, w = v.shape
    rows = np.arange(h)[:, None]
    mask = np.broadcast_to((rows >= r_lo) & (rows <= r_hi), (h, w)).copy()
    if excl:
        x0, y0, x1, y1 = excl
        mask[max(0, y0 - 8):min(h, y1 + 8),
             max(0, x0 - 8):min(w, x1 + 8)] = False
    mask = mask[::stride, ::stride]
    ys, xs = np.nonzero(mask)
    ys, xs = ys * stride, xs * stride
    u = (xs + 0.5) / w
    A = np.stack([ys - yh, u, np.ones(len(ys))], 1)
    y = v[ys, xs]
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(4):
        resid = A @ coef - y
        mad = np.median(np.abs(resid - np.median(resid))) * 1.4826 + 1e-9
        keep = np.abs(resid - np.median(resid)) < 2.5 * mad
        coef = np.linalg.lstsq(A[keep], y[keep], rcond=None)[0]
    return coef


def transition(profile):
    """V−V_g 从 <0.5·τ 升到 >1.5·τ 的行跨度（接触线用）。"""
    lo = hi = None
    for i, dv in enumerate(profile):
        if lo is None and abs(dv) < 0.5 * TAU:
            lo = i
        if lo is not None and abs(dv) > 1.5 * TAU:
            hi = i
            break
    return None if lo is None or hi is None else hi - lo


def edge_width(v, col, r_edge, half=16):
    """跨硬边界的 10%→90% 升幅（行）。车体在 r_edge 以下，背景在上。"""
    r0, r1 = max(0, r_edge - half), min(v.shape[0], r_edge + half)
    prof = v[r0:r1, col].astype(np.float64)
    if len(prof) < 12:
        return None, None
    n = len(prof)
    i_hi = int(round(r_edge + 4)) - r0
    i_lo = int(round(r_edge - 8)) - r0
    if i_lo < 0 or i_hi >= n:
        return None, None
    v_car = float(np.median(v[int(r_edge) + 2:int(r_edge) + 9, col]))
    v_bg = float(np.median(v[int(r_edge) - 11:int(r_edge) - 3, col]))
    contrast = v_car - v_bg
    if abs(contrast) < 0.12:
        return None, contrast
    t = (prof - v_bg) / contrast
    mid = None
    for i in range(1, n - 1):
        if (t[i] - 0.5) * (t[i - 1] - 0.5) <= 0:
            mid = i
            break
    if mid is None:
        return None, contrast
    j = mid
    while j >= 0 and t[j] > 0.1:
        j -= 1
    if j < 0:
        return None, contrast
    i10 = j + 1
    j = mid
    while j < n and t[j] < 0.9:
        j += 1
    if j >= n:
        return None, contrast
    return j - i10, contrast


def upsample_period(v):
    """行向二阶差分的周期能量：内部 2×/4× 双线性放大会在周期 2/4 上出峰。"""
    d2 = np.abs(np.diff(v, 2, axis=0))[40:400, 200:1100]
    prof = d2.mean(axis=1)
    prof = prof - prof.mean()
    f = np.abs(np.fft.rfft(prof))
    freq = np.fft.rfftfreq(len(prof))
    out = {}
    for period in (2, 4, 8, 16):
        k = int(round(len(prof) / period))
        out[str(period)] = float(f[k])
    base = float(np.median(f[1:]))
    return {k: (None if base <= 0 else round(vv / base, 2)) for k, vv in out.items()}


def render(tag, depth, rgb, box, yh, s):
    d = depth.astype(np.float64)
    lo, hi = np.percentile(d, 2), np.percentile(d, 98)
    n = np.clip((d - lo) / max(1e-6, hi - lo), 0, 1)
    img = cv2.applyColorMap((n * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    vis = cv2.resize(img, (1280, 720), interpolation=cv2.INTER_NEAREST)
    x1, y1, x2, y2 = [int(round(b * s)) for b in
                      (box[0], box[1], box[2], box[3])]
    cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 255, 255), 1)
    cv2.line(vis, (0, int(round(yh * s))), (1279, int(round(yh * s))), (255, 255, 255), 1)
    cv2.imwrite(str(OUT / f"depth_{tag}.png"), np.vstack([vis,
                cv2.resize(rgb, (1280, 720))[:, :, ::-1]]))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    info = io_probe()
    sess = {k: sim.session(v) for k, v in VARIANTS.items()}
    det = sim.session("prod_perception/model.onnx")
    lane = sim.session("lane/plus_small_384x640.onnx")
    recs = []
    for name, p, box_fix, yh_fix in FRAMES:
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        g = lg.geometry(lg.lane_prob(lane, rgb))
        yh = yh_fix if yh_fix is not None else (
            g["y_v"] if g["y_v"] is not None else 360.5)
        if box_fix is not None:
            box = box_fix
        else:
            dets = sim.detect_cars(det, rgb)
            if not dets:
                print(name, "no box")
                continue
            box = max(dets, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        rec = {"name": name, "file": str(p.relative_to(ROOT)),
               "box": list(box), "yh": float(yh), "variants": {}}
        for vname, s_ in sess.items():
            out448 = s_.run(None, {"image": sim.da_prep(rgb)})[0][0, 0]
            v720 = sim.resize_ac(out448.astype(np.float64), 720, 1280)
            s = 448.0 / 720.0
            b448 = [int(box[0] * 448 / 1280), int(box[1] * s),
                    int(box[2] * 448 / 1280), int(box[3] * s)]
            c448 = fit_ground(out448.astype(np.float64), yh * s, tuple(b448),
                              int(380 * s), int(690 * s), stride=1)
            m4, a4, A4 = [float(x) for x in c448]
            c720 = fit_ground(v720, yh, box, 380, 690)
            m7, a7, A7 = [float(x) for x in c720]
            cx = (box[0] + box[2]) // 2
            u = (cx + 0.5) / 1280.0
            yb = box[3]
            z = FY * CAM_H / max(1.0, yb - yh)
            dzdr = z * z / (FY * CAM_H)
            prof720 = [float(v720[r, cx]) - (A7 + m7 * (r - yh) + a7 * u)
                       for r in range(yb + 12, yb - 16, -1)]
            prof448 = [float(out448[r, int(u * 448)]) - (A4 + m4 * (r - yh * s) + a4 * u)
                       for r in range(int(round((yb + 12) * s)),
                                      int(round((yb - 16) * s)), -1)]
            w720, w448 = transition(prof720), transition(prof448)
            # 几何预测：车内深度若恒定，V−V_g 随高度线性增长，0.05 的跨度 = 0.05/m 行
            pred720 = None if abs(m7) < 1e-9 else 0.05 / abs(m7)
            # 车内平直度：框下 10%~60% 高度带内 V 对行的斜率 / 地面斜率
            r_lo_c = int(round(yb - 0.6 * (yb - box[1])))
            col_in = slice(box[0] + int(0.35 * (box[2] - box[0])),
                           box[0] + int(0.65 * (box[2] - box[0])))
            rows_in = np.arange(r_lo_c, yb - 1)
            vb = v720[rows_in][:, col_in].mean(axis=1)
            slope_v = float(np.polyfit(rows_in - yh, vb, 1)[0])
            # 硬边界：框顶行，取框中段 21 列
            cols = [int(b448[0] + f * (b448[2] - b448[0]))
                    for f in np.linspace(0.3, 0.7, 21)]
            ew = []
            contrasts = []
            for c in cols:
                w, ct = edge_width(out448, c, int(round(box[1] * s)))
                if w is not None:
                    ew.append(w)
                if ct is not None:
                    contrasts.append(abs(ct))
            ew720 = []
            for c in [int(box[0] + f * (box[2] - box[0]))
                      for f in np.linspace(0.3, 0.7, 21)]:
                w, _ = edge_width(v720, c, int(round(box[1])))
                if w is not None:
                    ew720.append(w)
            rec["variants"][vname] = {
                "z": z, "dzdr": dzdr,
                "geom_pred_rows_720": pred720,
                "body_slope_over_ground": (None if abs(m7) < 1e-9
                                           else slope_v / m7),
                "contact_448_rows": w448, "contact_720_rows": w720,
                "contact_448_m": None if w448 is None else w448 * dzdr * 720 / 448,
                "contact_720_m": None if w720 is None else w720 * dzdr,
                "hard_448_rows_med": float(np.median(ew)) if ew else None,
                "hard_448_rows_p90": float(np.percentile(ew, 90)) if ew else None,
                "hard_720_rows_med": float(np.median(ew720)) if ew720 else None,
                "hard_contrast_med": float(np.median(contrasts)) if contrasts else None,
                "hard_n": len(ew),
                "upsample_period_rel": upsample_period(out448.astype(np.float64)),
            }
            if vname == "slim":
                render(f"{name}_448", out448, rgb, box, yh, s)
            print(f"\n{name} [{vname}] box={box} yh={yh:.1f} z={z:.1f}m")
            print(f"  contact  448={w448} rows  720={w720} rows  "
                  f"(几何预测 {None if pred720 is None else round(pred720, 1)} rows)")
            print(f"  body slope / ground slope = "
                  f"{None if abs(m7) < 1e-9 else round(slope_v / m7, 2)}")
            print(f"  hard top 448={rec['variants'][vname]['hard_448_rows_med']} rows "
                  f"720={rec['variants'][vname]['hard_720_rows_med']} rows "
                  f"(n={len(ew)}, contrast={rec['variants'][vname]['hard_contrast_med']})")
            print(f"  period   {rec['variants'][vname]['upsample_period_rel']}")
        recs.append(rec)
    (OUT / "boundary_blur.json").write_text(
        json.dumps({"io": info, "frames": recs}, indent=1, ensure_ascii=False),
        encoding="utf-8")


if __name__ == "__main__":
    main()
