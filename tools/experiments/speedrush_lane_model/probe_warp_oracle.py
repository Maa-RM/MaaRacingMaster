# -*- coding: utf-8 -*-
"""盲区重排打标可行性探针（维护者提案，2026-11-11）。

思路：用已标定的地面几何（消点/地面尺度，与生产同一套常数）把画面重排成
「同一地面、更远虚拟机位」的构型——贴脸正中的大虚线在重排域回到模型认识
的远处形态；骗出响应后按同一映射逆变换回原帧，即得盲区伪标注。

重排两种（dst→src 仿射，消点不动）：
  pull   x'=VPX+(x-VPX)*r, y'=Y_H+(y-Y_H)*r —— 等效整段路面退远 1/r 倍
         （虚线随距离同步缩小，各向同性，最接近真实远视构型）
  vscale 只压 y —— 对照组（虚线压扁，检验各向异性是否伤响应）
r=1.0 即原生对照（盲区预期 ≈0，已由盲区点测证实）。

自包含探针，不 import 业务码。用法：python probe_warp_oracle.py
产物：first_look_out/warp/summary.md、overlay_*.jpg
"""
from __future__ import annotations

import cv2
import numpy as np
import onnxruntime as ort
from pathlib import Path

HOME = Path.home()
RAW = HOME / "AppData/Roaming/MaaRacingMaster/debug/speedrush/20261109_104823_manual_p1/raw"
HERE = Path(__file__).resolve().parent
OUT = HERE / "first_look_out" / "warp"

PLUS = HERE.parent.parent.parent / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx"
ORIG = HERE / "weights/twinlitenet_orig_360x640.onnx"

VPX, Y_H = 640.7, 354.2      # 全幅（1280x720）标定值
PAD = 12                      # plus 输入的上下灰边
FRAMES = {"blind_f0204": ("0204_raw.jpg", (810, 960, 520, 719)),  # 贴脸正中大虚线（盲）
          "side_f0170": ("0170_raw.jpg", None),                   # 贴脸偏侧（原生有响应）
          "norm_f0101": ("0101_raw.jpg", None)}                   # 常规帧


def prep(rgb: np.ndarray, pad: int) -> np.ndarray:
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    if pad:
        im = cv2.copyMakeBorder(im, pad, pad, 0, 0,
                                cv2.BORDER_CONSTANT, value=(114, 114, 114))
    x = (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    return np.ascontiguousarray(x)


def lane_prob(sess, rgb: np.ndarray, pad: int) -> np.ndarray:
    names = [o.name for o in sess.get_outputs()]
    key = next((n for n in names if "ll" in n), names[-1])
    lo = sess.run([key], {sess.get_inputs()[0].name: prep(rgb, pad)})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    p = e[1] / e.sum(0)
    return p[pad:p.shape[0] - pad] if pad else p       # 内容域 360x640


def warp_mat(mode: str, r: float, W: int, H: int) -> np.ndarray:
    vpx, yh = VPX * W / 1280.0, Y_H * H / 720.0
    if mode == "pull":
        return np.float32([[1 / r, 0, vpx * (1 - 1 / r)],
                           [0, 1 / r, yh * (1 - 1 / r)]])
    return np.float32([[1, 0, 0], [0, 1 / r, yh * (1 - 1 / r)]])


def content_box(roi, mode: str, r: float, W: int, H: int, pad_px: int = 6):
    """原生 ROI → 重排域内容坐标框（外扩 pad_px 容忍响应偏移）。"""
    x0, x1, y0, y1 = roi
    vpx, yh = VPX * W / 1280.0, Y_H * H / 720.0
    if mode == "pull":
        bx0, bx1 = vpx + (x0 - vpx) * r, vpx + (x1 - vpx) * r
    else:
        bx0, bx1 = float(x0), float(x1)
    by0, by1 = yh + (y0 - yh) * r, yh + (y1 - yh) * r
    c0 = max(0, int(bx0 * 640.0 / W) - pad_px)
    c1 = min(640, int(bx1 * 640.0 / W) + pad_px)
    r0 = max(0, int(by0 * 360.0 / H) - pad_px)
    r1 = min(360, int(by1 * 360.0 / H) + pad_px)
    return c0, c1, r0, r1


def overlay(rgb, sess, roi, mode: str, r: float, native_max: float) -> Path:
    """三联图：原生+盲区框 | 重排域+响应 | 原生+逆映射伪标注。"""
    H, W = rgb.shape[:2]
    dst = cv2.warpAffine(rgb, warp_mat(mode, r, W, H), (W, H),
                         flags=cv2.INTER_LINEAR, borderValue=(114, 114, 114))
    p = lane_prob(sess, dst, PAD)
    mask = cv2.dilate((p > 0.30).astype(np.uint8), np.ones((3, 3), np.uint8))
    ys, xs = np.nonzero(mask)
    # 逆映射回原帧（pull: 消点不动反缩放；vscale: 只还原 y）
    xd, yd = xs * W / 640.0, ys * H / 360.0
    vpx, yh = VPX * W / 1280.0, Y_H * H / 720.0
    xn = vpx + (xd - vpx) / r if mode == "pull" else xd
    yn = yh + (yd - yh) / r
    keep = (yn >= 0) & (yn < H) & (xn >= 0) & (xn < W)
    canvas = np.zeros((H, W), np.uint8)
    canvas[yn[keep].astype(int), xn[keep].astype(int)] = 255
    canvas = cv2.dilate(canvas, np.ones((5, 5), np.uint8))

    vis1, vis2, vis3 = rgb.copy(), dst.copy(), rgb.copy()
    cv2.rectangle(vis1, roi[:2], roi[2:], (255, 60, 60), 3)
    cv2.putText(vis1, f"native max={native_max:.3f}", (30, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 60, 60), 2)
    c0, c1, r0, r1 = content_box(roi, mode, r, W, H)
    cv2.rectangle(vis2, (int(c0 * W / 640), int(r0 * H / 360)),
                  (int(c1 * W / 640), int(r1 * H / 360)), (60, 220, 255), 3)
    vis2[ys * H // 360, xs * W // 640] = (0, 255, 0)
    cv2.putText(vis2, f"{mode} r={r} (virtual view)", (30, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 220, 255), 2)
    vis3[canvas > 0] = (0, 255, 0)
    cv2.putText(vis3, "pseudo-label mapped back", (30, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
    panel = np.hstack([vis1, vis2, vis3])
    scale = 1920.0 / panel.shape[1]
    panel = cv2.resize(panel, (1920, int(panel.shape[0] * scale)))
    out = OUT / f"overlay_blind_{mode}_{int(r * 100):02d}.jpg"
    cv2.imwrite(str(out), cv2.cvtColor(panel, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sp = ort.InferenceSession(str(PLUS), providers=["CPUExecutionProvider"])
    so = ort.InferenceSession(str(ORIG), providers=["CPUExecutionProvider"])
    blind, ctrl = ["# 盲区重排打标可行性读数", "",
                   "| 帧 | 视图 | plus 盲区max | orig 盲区max |", "|---|---|---|---|"], \
                  ["", "## 对照帧（重排不应杀死常规响应）",
                   "| 帧 | 视图 | plus>0.3 像素 | plus max |", "|---|---|---|---|"]
    best, saved = None, {}
    for name, (fname, roi) in FRAMES.items():
        fp = RAW / fname
        if not fp.exists():
            print("缺帧:", fname)
            continue
        rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
        H, W = rgb.shape[:2]
        pp, po = lane_prob(sp, rgb, PAD), lane_prob(so, rgb, 0)
        if roi is None:
            ctrl.append(f"| {name} | native | {(pp > 0.3).sum()} | {pp.max():.3f} |")
        else:
            c0, c1, r0, r1 = content_box(roi, "pull", 1.0, W, H)
            nat_p, nat_o = pp[r0:r1, c0:c1].max(), po[r0:r1, c0:c1].max()
            blind.append(f"| {name} | native 对照 | {nat_p:.4f} | {nat_o:.4f} |")
        for mode in ("pull", "vscale"):
            for r in (0.25, 0.35, 0.5, 0.7):
                dst = cv2.warpAffine(rgb, warp_mat(mode, r, W, H), (W, H),
                                     flags=cv2.INTER_LINEAR,
                                     borderValue=(114, 114, 114))
                q, qo = lane_prob(sp, dst, PAD), lane_prob(so, dst, 0)
                if roi is None:
                    ctrl.append(f"| {name} | {mode} r={r} | {(q > 0.3).sum()} | {q.max():.3f} |")
                    continue
                c0, c1, r0, r1 = content_box(roi, mode, r, W, H)
                mp = float(q[r0:r1, c0:c1].max()) if r1 > r0 and c1 > c0 else 0.0
                mo = float(qo[r0:r1, c0:c1].max()) if r1 > r0 and c1 > c0 else 0.0
                blind.append(f"| {name} | {mode} r={r} | {mp:.4f} | {mo:.4f} |")
                if best is None or mp > best[0]:
                    best = (mp, name, roi, mode, r, float(nat_p))
    blind.append("")
    blind.append(f"最佳：{best[2]} 帧在 {best[3]} r={best[4]} 下盲区 max={best[0]:.4f}"
                 f"（原生 {best[5]:.4f}）")
    if best:
        rgb = cv2.cvtColor(cv2.imread(str(RAW / FRAMES[best[1]][0])), cv2.COLOR_BGR2RGB)
        saved["overlay"] = overlay(rgb, sp, best[2], best[3], best[4], best[5])
    (OUT / "summary.md").write_text("\n".join(blind + ctrl) + "\n", encoding="utf-8")
    print("\n".join(blind))
    print("\n".join(ctrl))
    print("overlay:", saved.get("overlay"))


main()
