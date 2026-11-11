# -*- coding: utf-8 -*-
"""校准对齐对比图（2026-11-11 锚落定后）：米制直读 + 两点仿射，共享值域。

锚与口径（本日落定）：
- 权威锚：保险杠相机离地高 1.25m（维护者权威，RULES）；地面深度金标
  z_gold(y) = FX·1.25/(y−Y_H)，FX 取 gate0 的 670.7（全幅 px），Y_H=354.2（720 行）。
- 米制模型（米制直读）：
  - MoGe 生产 q4f16：适配器已含 depth_scale 0.893（生产同链，无额外系数）；
  - moge2_vits_fp32：适配器 0.893 → ×0.829（516 金标行）；
  - monodepth2 ms640：disp→depth ×5.4 立体基线 → ×0.059。
- 相对模型（换后端，两点仿射）：近=小换算后 V 随深度递增，地面行回归
  z = a·V + b（行 430~670，逐行中位对 z_gold 最小二乘——两点仿射的稳健版，
  顺带抗车占位）：yolo26n/s/m、ZipDepth、DepthART MNV4-S、MiDaS。
- 渲染：全部模型共享 p2~p98 米制值域（两组图同一值域，可互相对比）、
  平方根非线性映射（中近场拉开）、近=亮橙远=深蓝紫、无效=灰；turbo 色图。
- 淘汰（维护者 2026-11-11 目检定案）：monodepth2、Lite-Mono 效果太差
  （校准后结构糊/极端纵向畸变）——仍渲染存档，不进候选。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import sys
sys.path.insert(0, str(Path(__file__).parent))
import probe_bakeoff as pb  # noqa: E402

ROOT = Path(__file__).parent
FRAMES_DIR = ROOT / "frames" / "20261111_104434_p1"
OUT_MAIN = ROOT / "bakeoff_out" / "calibrated_compare.png"
OUT_REST = ROOT / "bakeoff_out" / "calibrated_compare_rest.png"

FX = 670.7          # gate0 notes：MoGe fx≈670（全幅 px）
CAM_H = 1.25        # 权威锚：保险杠相机离地高（m）
Y_H = 354.2         # 地平线行（720 行口径）
COEF_MOGE2 = 0.829      # moge2 金标系数（516 行，锚 1.25m）
COEF_MONO = 0.059       # monodepth2 系数（×5.4 立体基线后，锚 1.25m）
FIT_ROWS = (430, 670, 5)    # 地面行回归范围（720 行口径）
FRAMES = ["0089", "0096", "0103"]   # 远 / 中 / 近
CELL_W, CELL_H = 426, 240     # 单格渲染尺寸

GRID_MAIN = ["MoGe-2 ViT-S", "monodepth2", "yolo26n", "Lite-Mono"]
GRID_REST = ["MoGe 生产 q4f16", "yolo26s", "yolo26m", "ZipDepth fp16",
             "DepthART MNV4-S", "MiDaS small"]


def z_gold_row(y: np.ndarray) -> np.ndarray:
    return FX * CAM_H / (y - Y_H)


def affine_from_ground(v: np.ndarray) -> tuple[float, float] | None:
    """地面行回归：逐行中位 V 对 z_gold 最小二乘 → z = a·V + b（a>0 才可信）。"""
    y0, y1, st = FIT_ROWS
    ys, vs = [], []
    for y in range(y0, y1, st):
        row = v[y]
        row = row[np.isfinite(row)]
        if row.size < 100:
            continue
        ys.append(y)
        vs.append(float(np.median(row)))
    if len(ys) < 8:
        return None
    ys, vs = np.array(ys, float), np.array(vs)
    zg = z_gold_row(ys)
    if vs.max() - vs.min() < 1e-6:
        return None
    a = float(np.cov(vs, zg)[0, 1] / np.var(vs))
    b = float(zg.mean() - a * vs.mean())
    if not np.isfinite(a) or a <= 0:
        return None
    return a, b


def mono_metric(sess, rgb: np.ndarray) -> np.ndarray:
    """monodepth2：640x192 官方口径 → depth ×5.4 → ×0.059 金标系数。"""
    im = cv2.resize(rgb, (640, 192), interpolation=cv2.INTER_AREA)
    x = (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    disp = sess.run(None, {"images": np.ascontiguousarray(x)})[0][0, 0]
    depth = (1.0 / (0.01 + 0.98 * disp)) * 5.4 * COEF_MONO
    return pb.resize_to(depth, (720, 1280))


def _masked_resize(z: np.ndarray, valid: np.ndarray) -> np.ndarray:
    z = pb.resize_to(z, (720, 1280))
    z[~(pb.resize_to(valid.astype(np.float32), (720, 1280)) > 0.5)] = np.nan
    return z


def to_color(m: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """共享值域 → turbo（近=亮远=暗），无效=灰。平方根非线性映射：中近场
    （关注带）拉开、远场平滑压缩；近场端压到 0.88 亮橙带（turbo 顶端 255 是
    深红非亮色）。色图在二维全幅上算——若喂一维掩码数组，输出 (1,N,3)，
    之后的通道反转会错反像素顺序轴（颜色既不转 RGB 又被分配错像素）。"""
    ok = np.isfinite(m)
    t = np.zeros(m.shape, np.float32)
    t[ok] = np.clip((m[ok] - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    t = (1.0 - np.sqrt(t)) * 0.88
    col = cv2.applyColorMap((t * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    col[~ok] = (160, 160, 160)
    return col[:, :, ::-1]  # BGR → RGB（全幅一次反转）


def main() -> None:
    sess = {
        "moge2": pb.make_session("moge2_vits/model_fp32.onnx",
                                 free_dims={"batch_size": 1, "height": 720,
                                            "width": 1280}),
        "moge_prod": pb.make_session("prod_moge_q4f16.onnx",
                                     free_dims={"batch_size": 1, "height": 720,
                                                "width": 1280}),
        "mono": pb.make_session("monodepth2/mono+stereo_640x192.onnx"),
        "yolo26n": pb.make_session("yolo26_depth/yolo26n-depth.onnx"),
        "yolo26s": pb.make_session("yolo26_depth/yolo26s-depth.onnx"),
        "yolo26m": pb.make_session("yolo26_depth/yolo26m-depth.onnx"),
        "zipdepth": pb.make_session("zipdepth/zipdepth_fp16_opset17.onnx"),
        "depthart": pb.make_session("depthart/relative_mnv4s_448_default.onnx"),
        "midas": pb.make_session("midas/model-small.onnx"),
    }
    lm_enc = pb.make_session("lite_mono_base_640x192/encoder.onnx")
    lm_dec = pb.make_session("lite_mono_base_640x192/decoder.onnx")

    def raw_of(name: str, rgb: np.ndarray):
        if name.startswith("yolo26"):
            return pb.run_yolo26(sess[name], rgb)
        if name == "ZipDepth fp16":
            return pb.run_zipdepth(sess["zipdepth"], rgb)
        if name == "DepthART MNV4-S":
            return pb.run_depthart(sess["depthart"], rgb, False)
        if name == "MiDaS small":
            return pb.run_midas(sess["midas"], rgb)
        if name == "Lite-Mono":
            return pb.run_lite_mono(lm_enc, lm_dec, rgb, (192, 640))
        raise KeyError(name)

    def metric_of(name: str, rgb: np.ndarray) -> tuple[np.ndarray, str]:
        """返回 (720×1280 米制图 with NaN, 标注)。"""
        if name == "MoGe-2 ViT-S":
            z, valid = pb.run_moge(sess["moge2"], rgb)
            return _masked_resize(z, valid) * COEF_MOGE2, "米制直读 ×0.829"
        if name == "MoGe 生产 q4f16":
            z, valid = pb.run_moge(sess["moge_prod"], rgb)
            return _masked_resize(z, valid), "米制直读（生产 ×0.893）"
        if name == "monodepth2":
            return mono_metric(sess["mono"], rgb), "米制直读 ×0.059"
        # 相对模型：地面行回归两点仿射
        v_raw, valid = raw_of(name, rgb)
        v = pb.to_near_small(v_raw)
        v = pb.resize_to(v, (720, 1280))
        ab = affine_from_ground(v)
        if ab is None:
            return np.full((720, 1280), np.nan), "标定失败"
        a, b = ab
        z = a * v + b
        z[~np.isfinite(v)] = np.nan
        return z, f"两点仿射 a={a:.2f} b={b:.1f}"

    # 全模型 × 全帧的图（两组共享值域）
    all_names = GRID_MAIN + GRID_REST
    maps, metas = {}, {}
    for seq in FRAMES:
        p = sorted(FRAMES_DIR.glob(f"{seq}*_raw.jpg"))[0]
        rgb = cv2.cvtColor(cv2.imread(str(p)), cv2.COLOR_BGR2RGB)
        maps[(seq, "RGB 原图")] = rgb.astype(np.float32) / 255.0
        metas[(seq, "RGB 原图")] = "原图"
        for name in all_names:
            m, meta = metric_of(name, rgb)
            maps[(seq, name)] = m
            metas[(seq, name)] = meta
            print(f"[{seq}] {name}: 全图中位 "
                  f"{np.nanmedian(m):.2f}m  {meta}")

    allv = np.concatenate([m[np.isfinite(m)].ravel()
                           for k, m in maps.items() if k[1] != "RGB 原图"])
    lo = float(np.percentile(allv, 2))
    # 远端物理截断：仿射模型的虚大读数（DepthART/MiDaS 可到 ~108m）会把共享
    # 值域拉宽、压扁中近场梯度；巡航场景对比-interest 之外的截到 45m 远端。
    hi = min(float(np.percentile(allv, 98)), 45.0)
    print(f"共享值域 = [{lo:.2f}, {hi:.2f}]m（两组图同一值域，远端截断 45m）")

    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 15)
    small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 12)

    def render_grid(names: list[str], out: Path) -> None:
        cols = ["RGB 原图"] + names
        grid = Image.new("RGB", (CELL_W * len(cols) + 10,
                                 (CELL_H + 44) * len(FRAMES) + 10),
                         (24, 24, 24))
        dr = ImageDraw.Draw(grid)
        for r, seq in enumerate(FRAMES):
            for c, name in enumerate(cols):
                m = maps[(seq, name)]
                if name == "RGB 原图":
                    img = (np.clip(m, 0, 1) * 255).astype(np.uint8)
                else:
                    img = to_color(m, lo, hi)
                img = cv2.resize(img, (CELL_W, CELL_H),
                                 interpolation=cv2.INTER_AREA)
                x, y = 5 + c * CELL_W, 5 + r * (CELL_H + 44)
                grid.paste(Image.fromarray(img), (x, y))
                dr.text((x + 4, y + CELL_H + 4), f"帧{seq} {name}", font=font,
                        fill=(255, 255, 255))
                dr.text((x + 4, y + CELL_H + 24), metas[(seq, name)],
                        font=small, fill=(190, 200, 220))
        out.parent.mkdir(exist_ok=True)
        grid.save(str(out))
        print("saved:", out)

    render_grid(GRID_MAIN, OUT_MAIN)
    render_grid(GRID_REST, OUT_REST)


if __name__ == "__main__":
    main()
