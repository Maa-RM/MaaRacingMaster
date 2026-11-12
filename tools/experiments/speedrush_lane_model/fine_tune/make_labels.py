# -*- coding: utf-8 -*-
"""伪标签生成：原生响应 ∪ 虚拟机位重排回标 → 灰度掩码（灰度 255=前景）。

流水线（每帧）：
  1) 原生推理（现役 plus_small，/255 + letterbox 384x640）→ 内容域概率 360x640；
  2) 重排推理：pull r=0.5（消点不动、横纵同缩，等效整段路面退远一倍）→ 概率图
     按同一映射逐行逆映射回原帧行（行像素一一对应，不插值）；
  3) 逐行成簇（prob>0.5 连续段取质心/宽度），原生优先；原生该行无簇时吃重排回标
     （仅近带 row≥240，且与原生簇横向距 >15px 才补），宽度按 1/r 还原；
  4) 渲染细掩码：每行画 [xc-w/2, xc+w/2]，宽上限 80px（640 尺度），行带 180~360；
     HUD 区（底部中央头像框）剔除。

目录镜像 BDD100K 结构（上游加载器约定：images/train|val + lane_line_annotations/
train|val，同名 png）。数据切分按会话：训练=1109 三场，验证=095756_p2 整场留出。

自包含探针（不 import 业务码）。用法：python make_labels.py [--sheet-only]
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODEL = HERE.parents[3] / "maaracing_master/plugins/speedrush/resources/models/lane/plus_small_384x640.onnx"
RAW_ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
OUT = ROOT / "bdd100k"
SHEET_DIR = ROOT / "fine_tune_out" / "label_sheets"

PAD = 12
VPX_C, YH_C = 320.35, 177.1          # 内容域（640x360）消点与地平线
R_PULL_LIST = (0.5, 0.6, 0.7)        # 多档缩放取并集：单档响应帧间不稳（2026-11-12）
FT_MODEL = HERE.parents[3] / "tools/experiments/speedrush_lane_model/weights/plus_small_ftfull_384x640.onnx"
PROB_T = 0.50                        # 伪标签取高置信
ROW_HI, ROW_LO = 360, 180            # 行带（内容域）
W_MAX = 80.0
HUD_BOX = (283, 337, 322, 360)       # 底部中央头像/里程牌，不产出标签
SPLIT = {"train": ("20261109_104823_manual_p1", "20261109_104823_manual_p2",
                   "20261109_095756_manual_p1",
                   "20261107_203433_manual_p1", "20261107_203433_manual_p2",
                   "20261107_215421_manual_p1", "20261108_133512_manual_p1"),
         "val": ("20261109_095756_manual_p2",)}
STEP = 6                             # 抽帧步长（相邻帧高度相关）

_sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
_in_name = _sess.get_inputs()[0].name
_out_names = [o.name for o in _sess.get_outputs()]
_ll_key = next(n for n in _out_names if "ll" in n)
_ft = ort.InferenceSession(str(FT_MODEL), providers=["CPUExecutionProvider"]) \
    if FT_MODEL.exists() else None          # 第三证人：上轮微调模型（原生近场已可用）


def lane_prob(rgb: np.ndarray, sess=None) -> np.ndarray:
    s = sess or _sess
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, PAD, PAD, 0, 0, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    x = np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
    lo = s.run([_ll_key], {s.get_inputs()[0].name: x})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    return (e / e.sum(0))[1][PAD:-PAD]


def warp_pull(rgb: np.ndarray, r: float) -> np.ndarray:
    H, W = rgb.shape[:2]
    vpx, yh = VPX_C * W / 640.0, YH_C * H / 360.0
    M = np.float32([[1 / r, 0, vpx * (1 - 1 / r)],
                    [0, 1 / r, yh * (1 - 1 / r)]])
    # M 按 dst→src 语义使用：必须带 WARP_INVERSE_MAP，否则 OpenCV 把它当
    # src→dst 求逆，得到的是中央 2x 放大而非退远压缩（2026-11-12 教训）。
    return cv2.warpAffine(rgb, M, (W, H),
                          flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderValue=(114, 114, 114))


def clusters(row: np.ndarray, thr: float) -> list[tuple[float, float, float]]:
    """单行概率 → [(质心x, 宽度, 峰值)]，宽度为像素截断。"""
    idx = np.flatnonzero(row > thr)
    if len(idx) == 0:
        return []
    splits = np.flatnonzero(np.diff(idx) > 1) + 1
    out = []
    for seg in np.split(idx, splits):
        if len(seg) == 0:
            continue
        p = row[seg]
        out.append((float((seg * p).sum() / p.sum()), float(len(seg)), float(p.max())))
    return out


def map_row_back(y: float) -> float:
    """原帧内容行 y → 重排域内容行（pull 正向映射）。"""
    return YH_C + (y - YH_C) * R_PULL


def map_x_back(x_d: float) -> float:
    return VPX_C + (x_d - VPX_C) / R_PULL


def build_frame(rgb: np.ndarray) -> tuple[np.ndarray, dict]:
    """一帧 → (640x360 掩码, 统计)。标签 = 原生响应 ∪ 多档重排回标 ∪ 上轮微调模型近场。"""
    prob_n = lane_prob(rgb)
    prob_ws = [(r, lane_prob(warp_pull(rgb, r))) for r in R_PULL_LIST]
    prob_ft = lane_prob(rgb, _ft) if _ft is not None else None
    mask = np.zeros((360, 640), np.uint8)
    n_native = n_warp = 0
    hx0, hx1, hy0, hy1 = HUD_BOX
    for y in range(ROW_LO, ROW_HI):
        got = clusters(prob_n[y], PROB_T)
        n_native += len(got)
        if y >= 240:                         # 近带：原生盲，吃重排回标（多档并集）
            for r, prob_w in prob_ws:
                yw = int(round(YH_C + (y - YH_C) * r))
                if not (0 <= yw < 360):
                    continue
                for xc_d, w_d, pk in clusters(prob_w[yw], PROB_T):
                    xc = VPX_C + (xc_d - VPX_C) / r
                    if not (0 <= xc < 640):
                        continue
                    if any(abs(xc - g[0]) < 15.0 for g in got):
                        continue
                    got.append((xc, min(w_d / r, W_MAX), pk))
                    n_warp += 1
        if prob_ft is not None and y >= 252:  # 第三证人：上轮微调模型（原生近场）
            for xc, w_d, pk in clusters(prob_ft[y], PROB_T):
                if any(abs(xc - g[0]) < 15.0 for g in got):
                    continue
                got.append((xc, min(w_d, W_MAX), pk))
                n_warp += 1
        for xc, w, _ in got:
            w = min(w, W_MAX)
            x0 = max(0, int(xc - w / 2))
            x1 = min(640, int(xc + w / 2) + 1)
            if y >= hy0 and hx0 <= xc <= hx1:
                continue                     # HUD 头像框内不出标
            mask[y, x0:x1] = 255
    return mask, {"native_clusters": n_native, "warp_clusters": n_warp}


def sheet(frames: list[Path], masks: list[np.ndarray], title: str, stride: int = 12) -> Path:
    SHEET_DIR.mkdir(parents=True, exist_ok=True)
    tiles = []
    for fp, mk in zip(frames, masks):
        rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
        small = cv2.resize(rgb, (640, 360))
        m3 = cv2.dilate(mk, np.ones((3, 3), np.uint8))
        small[m3 > 0] = (0, 255, 0)
        cv2.putText(small, fp.name[:28], (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1)
        tiles.append(small)
    rows = []
    for i in range(0, len(tiles), 4):
        chunk = tiles[i:i + 4]
        while len(chunk) < 4:
            chunk.append(np.zeros_like(tiles[0]))
        rows.append(np.hstack(chunk))
    img = np.vstack(rows)
    out = SHEET_DIR / f"{title}.jpg"
    cv2.imwrite(str(out), cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-only", action="store_true")
    args = ap.parse_args()
    stats = {s: {"frames": 0, "native_clusters": 0, "warp_clusters": 0,
                 "labeled_rows": 0} for s in SPLIT}
    sheets_frames: dict[str, list] = {"train": [], "val": []}
    sheets_masks: dict[str, list] = {"train": [], "val": []}
    for split, sessions in SPLIT.items():
        img_dir = OUT / "images" / split
        lbl_dir = OUT / "lane_line_annotations" / split
        img_dir.mkdir(parents=True, exist_ok=True)
        lbl_dir.mkdir(parents=True, exist_ok=True)
        st = stats[split]
        for skey in sessions:
            raw = RAW_ROOT / skey / "raw"
            for fp in sorted(raw.glob("*.jpg"))[::STEP]:
                name = f"{skey[9:17]}_{skey[-2:]}_{fp.stem}"
                jpg = img_dir / f"{name}.jpg"
                png = lbl_dir / f"{name}.png"
                if args.sheet_only and png.exists():
                    mk = cv2.imread(str(png), 0)
                else:
                    rgb = cv2.cvtColor(cv2.imread(str(fp)), cv2.COLOR_BGR2RGB)
                    mk, info = build_frame(rgb)
                    if int((mk > 0).any(1).sum()) < 8:
                        continue        # 毒样本过滤：画面有线但掩码近空（如座舱过渡帧）
                    cv2.imwrite(str(png), mk)
                    if not jpg.exists():
                        shutil.copy2(fp, jpg)
                    st["frames"] += 1
                    st["native_clusters"] += info["native_clusters"]
                    st["warp_clusters"] += info["warp_clusters"]
                st["labeled_rows"] += int((mk > 0).any(1).sum())
                if len(sheets_frames[split]) < 36:
                    sheets_frames[split].append(jpg)
                    sheets_masks[split].append(mk)
    print("== 数据与抽检 sheet ==")
    for split in ("train", "val"):
        n = len(list((OUT / "images" / split).glob("*.jpg")))
        m = len(list((OUT / "lane_line_annotations" / split).glob("*.png")))
        print(f"{split}: 图 {n} / 标 {m}")
        if sheets_frames[split]:
            p = sheet(sheets_frames[split], sheets_masks[split], f"{split}_sample")
            print("  sheet:", p)
    for split, st in stats.items():
        fr = max(1, st["frames"])
        print(f"{split}: 本跑 {st['frames']} 帧；原生簇/帧 {st['native_clusters']/fr:.1f}；"
              f"重排簇/帧 {st['warp_clusters']/fr:.1f}；有标签行/帧 {st['labeled_rows']/fr:.0f}")


if __name__ == "__main__":
    main()
