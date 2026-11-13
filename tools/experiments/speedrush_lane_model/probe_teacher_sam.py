# -*- coding: utf-8 -*-
"""用 SAM（分割基础模型）标注贴脸粗白虚线：不依赖「车道线」类别先验。

为什么换 SAM（维护者 2026-11-12 提案「叫分割甚至语义模型来标注，专项做
中心位置的车道线学习」）：
- 车道线专精模型（SegFormer 社区权重 / SCNN-TuSimple）实测要么描边、要么
  看不见贴脸构型——因为真车数据里底部中央没有这种线，类别先验把它们带偏。
- SAM 是类别无关的通用分割：给一个框，它把框内物体抠出来，不知道也不关心
  「这是不是车道线」，所以不会被上述先验影响。
- 提示框来自已有的亮度几何真值（make_target_labels.py 的 extract），
  真值只用来给位置，掩码边界由 SAM 自己出。

用法：在 GPU 训练环境下 python probe_teacher_sam.py
产物：first_look_out/teacher_sam/ 下掩码叠图与对照
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import torch
from transformers import SamModel, SamProcessor

HERE = Path(__file__).resolve().parent
SAM_DIR = HERE / "teachers" / "sam_vit_base"
OUT = HERE / "first_look_out" / "teacher_sam"
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(HERE / "fine_tune"))
import make_target_labels as mtl  # noqa: E402  复用亮度几何真值

RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug"
FRAMES = [
    ("f0204", RAW / "speedrush/20261109_104823_manual_p2/raw/0204_raw.jpg",
     (258, 282, 265, 360), "黄昏·维护者指认"),
    ("f0202", RAW / "20261110_204716/speedrush/drive/20261110_205224_p1/raw/0202_raw.jpg",
     (310, 339, 253, 360), "用户给出帧"),
]


def box_from_mask(mask, pad=6):
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return (max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad),
            int(xs.max()) + pad, int(ys.max()) + pad)


def main():
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={dev}", flush=True)
    model = SamModel.from_pretrained(str(SAM_DIR)).to(dev).eval()
    proc = SamProcessor.from_pretrained(str(SAM_DIR))

    for tag, fp, fallback_box, desc in FRAMES:
        if not fp.exists():
            print(tag, "MISSING")
            continue
        im = cv2.imread(str(fp))
        h, w = im.shape[:2]
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)

        # 几何真值给框（内容域）；取不到就用已知 ROI
        gm, info = mtl.extract(content)
        if gm is not None:
            b = box_from_mask(gm > 0, pad=8)
        else:
            b = None
        if b is None:
            x0, x1, y0, y1 = fallback_box
            b = (x0 - 8, y0 - 8, x1 + 8, y1 + 8)
        print(f"\n{tag} [{desc}] 提示框(内容域)={b} 几何真值={info}", flush=True)

        # 内容域坐标 -> 原图坐标（×2）
        box_orig = [[[b[0] * 2, b[1] * 2, b[2] * 2, b[3] * 2]]]
        rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        inputs = proc(images=rgb, input_boxes=box_orig, return_tensors="pt")
        inputs = {k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in inputs.items()}
        with torch.no_grad():
            out = model(**inputs, multimask_output=True)
        masks = proc.image_processor.post_process_masks(
            out.pred_masks.cpu(), inputs["original_sizes"].cpu(),
            inputs["reshaped_input_sizes"].cpu())[0]
        masks = masks.squeeze()                            # -> (3,H,W)
        if masks.dim() == 2:
            masks = masks[None]
        scores = out.iou_scores.cpu().numpy().squeeze()    # -> (3,)
        scores = np.atleast_1d(scores)
        k = int(np.argmax(scores))
        m = masks[k].numpy() > 0.5
        print(f"  候选 IoU 分数 {np.round(scores, 3)}，选第 {k} 个；"
              f"掩码像素 {int(m.sum())}", flush=True)

        # 掩码缩回内容域，供与几何真值对照
        m_small = cv2.resize(m.astype(np.uint8) * 255, (640, 360),
                             interpolation=cv2.INTER_NEAREST) > 0
        ys, xs = np.nonzero(m_small)
        if len(xs):
            print(f"  内容域掩码 x {xs.min()}-{xs.max()}, y {ys.min()}-{ys.max()}, "
                  f"像素 {len(xs)}", flush=True)

        # 三联：原图 | 几何真值(绿) | SAM 掩码(青)
        p1 = im.copy()
        if gm is not None:
            gg = cv2.resize((gm > 0).astype(np.uint8) * 255, (w, h),
                            interpolation=cv2.INTER_NEAREST) > 0
            p1[gg] = (0.3 * p1[gg] + 0.7 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
        cv2.putText(p1, "geometry truth (brightness)", (12, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        p2 = im.copy()
        p2[m] = (0.3 * p2[m] + 0.7 * np.array([255, 255, 0], np.float32)).astype(np.uint8)
        cv2.putText(p2, f"SAM mask (iou={scores[k]:.3f})", (12, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        p3 = im.copy()
        if gm is not None:
            p3[gg] = (0.3 * p3[gg] + 0.7 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
        p3[m] = (0.3 * p3[m] + 0.7 * np.array([255, 255, 0], np.float32)).astype(np.uint8)
        cv2.putText(p3, "overlay green=truth cyan=SAM", (12, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        sheet = np.hstack([cv2.resize(p, (640, 360)) for p in (p1, p2, p3)])
        cv2.imwrite(str(OUT / f"{tag}_sam.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])

        # 放大近场（内容域 x200-380, y240-360）
        z = []
        for p in (p1, p2):
            c = p[480:720, 400:760].copy()
            z.append(cv2.resize(c, (c.shape[1] * 2, c.shape[0] * 2),
                                interpolation=cv2.INTER_LINEAR))
        cv2.imwrite(str(OUT / f"{tag}_sam_zoom.jpg"), np.hstack(z),
                    [cv2.IMWRITE_JPEG_QUALITY, 92])

    print("\n产物:", OUT)


if __name__ == "__main__":
    main()
