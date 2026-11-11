# -*- coding: utf-8 -*-
"""分步计时 + 同帧渲染对照（2026-11-11）：ZipDepth / MoGe / DepthART / yolo 系全挡。

计时口径：DML 全链逐小步（perf_counter，warmup 3 + 计时 10 passes 取 p50）——
①预处理（缩放/letterbox + 归一化 + NCHW）；②推理（session.run，DML）；
③后处理（近=小换算 + 放大回 720×1280 + 标定换算）。另列模型文件体积。

渲染：同一张 raw（0096，中段）× 7 模型，同一校准（米制直读 / 地面行回归
两点仿射）、同一值域与颜色语义（与对比图一致）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import sys
sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402
import render_calibrated as rc  # noqa: E402

ROOT = Path(__file__).parent
FRAME = "0096"
OUT_PNG = ROOT / "bakeoff_out" / "same_frame_compare.png"
OUT_JSON = ROOT / "bakeoff_out" / "step_timing.json"
WARMUP, PASSES = 3, 10
CELL_W, CELL_H = 426, 240


def t_p50(xs: list[float]) -> float:
    return float(np.percentile(xs, 50)) * 1000.0  # ms


def main() -> None:
    p = sorted(rc.FRAMES_DIR.glob(f"{FRAME}*_raw.jpg"))[0]
    rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)

    sess = {
        "zipdepth": pb.make_session("zipdepth/zipdepth_fp16_opset17.onnx"),
        "moge_prod": pb.make_session("prod_moge_q4f16.onnx",
                                     free_dims={"batch_size": 1, "height": 720,
                                                "width": 1280}),
        "moge2": pb.make_session("moge2_vits/model_fp32.onnx",
                                 free_dims={"batch_size": 1, "height": 720,
                                            "width": 1280}),
        "depthart": pb.make_session("depthart/relative_mnv4s_448_default.onnx"),
        "yolo26n": pb.make_session("yolo26_depth/yolo26n-depth.onnx"),
        "yolo26s": pb.make_session("yolo26_depth/yolo26s-depth.onnx"),
        "yolo26m": pb.make_session("yolo26_depth/yolo26m-depth.onnx"),
    }

    def pipeline_zipdepth():
        s = sess["zipdepth"]

        def pre(fr):
            im = cv2.resize(fr, (800, 448), interpolation=cv2.INTER_LINEAR)
            return (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]

        def infer(blob):
            return s.run(None, {"pixel_values": np.ascontiguousarray(blob)})[0][0, 0]

        def post(d):
            v = pb.to_near_small(d)
            v = pb.resize_to(v, (720, 1280))
            ab = rc.affine_from_ground(v)
            z = ab[0] * v + ab[1]
            z[~np.isfinite(v)] = np.nan
            return z

        return pre, infer, post, "zipdepth/zipdepth_fp16_opset17.onnx"

    def pipeline_moge(key: str, coef: float):
        s = sess[key]

        def pre(fr):
            return pb._nchw(fr, (720, 1280))

        def infer(blob):
            feed = {"image": blob}
            if any(i.name == "num_tokens" for i in s.get_inputs()):
                feed["num_tokens"] = np.array(
                    pb.MOGE_TOKEN_CACHE.get(id(s), 1032), np.int64)
            return s.run(None, feed)

        def post(out):
            pts = out[0][0]
            mask = out[2][0] if len(out) > 2 else np.ones(pts.shape[:2], bool)
            scale = float(out[3][0]) if len(out) > 3 else 1.0
            z = pts[..., 2] * scale * pb.DEPTH_SCALE
            ok = (mask > 0.5) & np.isfinite(z)
            z = rc._masked_resize(z, ok)
            return z * coef

        return pre, infer, post, (
            "prod_moge_q4f16.onnx" if key == "moge_prod"
            else "moge2_vits/model_fp32.onnx")

    def pipeline_depthart():
        s = sess["depthart"]

        def pre(fr):
            return pb._nchw(fr, (448, 448))

        def infer(blob):
            return s.run(None, {"image": np.ascontiguousarray(blob)})[0][0, 0]

        def post(d):
            v = pb.to_near_small(d)
            v = pb.resize_to(v, (720, 1280))
            ab = rc.affine_from_ground(v)
            z = ab[0] * v + ab[1]
            z[~np.isfinite(v)] = np.nan
            return z

        return pre, infer, post, "depthart/relative_mnv4s_448_default.onnx"

    def pipeline_yolo(key: str):
        s = sess[key]

        def pre(fr):  # 768×768 letterbox：0.6 缩放 → 768×432 有效区 + 114 灰边
            im = cv2.resize(fr, (768, 432), interpolation=cv2.INTER_AREA)
            x = im.astype(np.float32) / 255.0
            blob = np.full((1, 3, 768, 768), 114.0 / 255.0, np.float32)
            blob[0, :, :432, :] = x.transpose(2, 0, 1)
            return blob

        def infer(blob):
            return s.run(None, {"images": np.ascontiguousarray(blob)})[0][0, 0]

        def post(d):
            d_eff = pb._unpad_map(d, (720, 1280), (768, 768), 0, 0)
            v = pb.to_near_small(d_eff)
            v = pb.resize_to(v, (720, 1280))
            ab = rc.affine_from_ground(v)
            z = ab[0] * v + ab[1]
            z[~np.isfinite(v)] = np.nan
            return z

        return pre, infer, post, f"yolo26_depth/{key}-depth.onnx"

    entries = [
        ("ZipDepth fp16", pipeline_zipdepth()),
        ("MoGe 生产 q4f16", pipeline_moge("moge_prod", 1.0)),
        ("MoGe-2 ViT-S", pipeline_moge("moge2", rc.COEF_MOGE2)),
        ("DepthART MNV4-S", pipeline_depthart()),
        ("yolo26n", pipeline_yolo("yolo26n")),
        ("yolo26s", pipeline_yolo("yolo26s")),
        ("yolo26m", pipeline_yolo("yolo26m")),
    ]

    # 计时
    timing = {}
    maps = {}
    for name, (pre, infer, post, weight) in entries:
        # 全链 warmup（含 num_tokens 探测缓存）
        for _ in range(WARMUP):
            post(infer(pre(rgb)))
        pre_t, infer_t, post_t, tot_t = [], [], [], []
        for _ in range(PASSES):
            t0 = time.perf_counter()
            blob = pre(rgb)
            t1 = time.perf_counter()
            out = infer(blob)
            t2 = time.perf_counter()
            m = post(out)
            t3 = time.perf_counter()
            pre_t.append(t1 - t0)
            infer_t.append(t2 - t1)
            post_t.append(t3 - t2)
            tot_t.append(t3 - t0)
        maps[name] = m
        size_mb = os.path.getsize(ROOT / "weights" / weight) / 1e6
        timing[name] = {
            "weight_file": weight, "model_size_mb": round(size_mb, 1),
            "pre_ms": round(t_p50(pre_t), 2), "infer_ms": round(t_p50(infer_t), 2),
            "post_ms": round(t_p50(post_t), 2), "total_ms": round(t_p50(tot_t), 2),
        }
        print(f"{name:16s} 体积 {size_mb:6.1f}MB  预处理 {t_p50(pre_t):6.2f}ms  "
              f"推理 {t_p50(infer_t):7.2f}ms  后处理 {t_p50(post_t):5.2f}ms  "
              f"全链 {t_p50(tot_t):7.2f}ms")

    # 渲染（同一值域与颜色语义，RGB + 7 模型单行）
    allv = np.concatenate([m[np.isfinite(m)].ravel() for m in maps.values()])
    lo = float(np.percentile(allv, 2))
    hi = min(float(np.percentile(allv, 98)), 45.0)
    cols = ["RGB 原图"] + [n for n, _ in entries]
    grid = Image.new("RGB", (CELL_W * len(cols) + 10, CELL_H + 44 + 10),
                     (24, 24, 24))
    dr = ImageDraw.Draw(grid)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
    small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 12)
    grid.paste(Image.fromarray(rgb), (5, 5))
    dr.text((9, 5 + CELL_H + 4), f"帧{FRAME} RGB 原图", font=font,
            fill=(255, 255, 255))
    for c, (name, _) in enumerate(entries, start=1):
        img = rc.to_color(maps[name], lo, hi)
        img = cv2.resize(img, (CELL_W, CELL_H), interpolation=cv2.INTER_AREA)
        x = 5 + c * CELL_W
        grid.paste(Image.fromarray(img), (x, 5))
        t = timing[name]
        dr.text((x + 4, 5 + CELL_H + 4), name, font=font, fill=(255, 255, 255))
        dr.text((x + 4, 5 + CELL_H + 24),
                f"{t['model_size_mb']}MB · 全链 {t['total_ms']}ms "
                f"(预 {t['pre_ms']}/推 {t['infer_ms']}/后 {t['post_ms']})",
                font=small, fill=(190, 200, 220))
    OUT_PNG.parent.mkdir(exist_ok=True)
    grid.save(str(OUT_PNG))
    print("saved:", OUT_PNG)
    OUT_JSON.write_text(json.dumps(timing, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    print("saved:", OUT_JSON)


if __name__ == "__main__":
    main()
