# -*- coding: utf-8 -*-
"""用 SAM 把贴脸粗白虚线标成**线体掩码**（不是描边），供「专项中心学习」用。

维护者 2026-11-12 提案：叫分割/语义模型来标注，专项做中心位置的车道线学习。
实测依据（`probe_teacher_sam.py`）：SAM 是类别无关的通用分割，给一个框就能
把线体完整抠出（f0204 掩码 1746 px vs 几何真值 1734 px，边界贴合整条白线），
不受「真车底部没有车道线」的类别先验影响；而亮度几何真值只覆盖左半。

提示框来源：`fine_tune_out/target_labels/index.json` 的 bbox（亮度真值），
真值只给位置，掩码边界由 SAM 出。

用法：在 GPU 训练环境下 python fine_tune/make_sam_labels.py
产物：fine_tune_out/sam_labels/{masks,index.json,qa_sheet.jpg}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from transformers import SamModel, SamProcessor

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SAM_DIR = ROOT / "teachers" / "sam_vit_base"
SRC = ROOT / "fine_tune_out" / "target_labels"
OUT = ROOT / "fine_tune_out" / "sam_labels"
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "masks").mkdir(exist_ok=True)

PAD = 10          # 提示框外扩
MAX_AREA_RATIO = 3.0   # SAM 掩码面积 / bbox 面积 上限（超了说明溢到别处）

# 车道线的几何本质（维护者 2026-11-12 指出拼图里有几帧标错，实测坐实）：
# 误标掩码尺寸高度一致（宽 24~26、高 35~36、底边停在 y≈333），是画面底部中央
# 一个固定小物体被错框后由 SAM 抠出；真正的贴脸线一定延伸到画面底边（y≈359）
# 且高度 70 以上。以下四个闸缺一不可。
MIN_Y1 = 300      # 底边须到画面下部（不卡死触底：部分帧线在车头前就结束）
MIN_H = 60        # 纵向高度：误标物体恒为 35~36，这条足以判别
MIN_ASPECT = 1.15  # 高/宽 比下限（放宽：近场斜线横向跨度大，h/w 会低到 1.2）
MIN_AREA = 900
MAX_AREA = 3600   # 超过即糊片（实测误标糊片 7024）
MIN_BRIGHT_DELTA = 18  # 掩码内亮度中位 - 周围沥青亮度中位（排除车身/阴影）


def main():
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={dev}", flush=True)
    model = SamModel.from_pretrained(str(SAM_DIR)).to(dev).eval()
    proc = SamProcessor.from_pretrained(str(SAM_DIR))
    index = json.loads((SRC / "index.json").read_text(encoding="utf-8"))
    rows, tiles = [], []
    t0 = time.perf_counter()

    for i, r in enumerate(index, 1):
        fp = Path(r["raw"])
        if not fp.exists():
            continue
        im = cv2.imread(str(fp))
        if im is None:
            continue
        h, w = im.shape[:2]
        bb = r["bbox"]
        x0 = max(0, bb["x"] - PAD)
        y0 = max(0, bb["y"] - PAD)
        x1 = min(639, bb["x"] + bb["w"] + PAD)
        y1 = min(359, bb["y"] + bb["h"] + PAD)
        box_orig = [[[x0 * 2, y0 * 2, x1 * 2, y1 * 2]]]
        rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        inputs = proc(images=rgb, input_boxes=box_orig, return_tensors="pt")
        inputs = {k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in inputs.items()}
        with torch.no_grad():
            out = model(**inputs, multimask_output=True)
        masks = proc.image_processor.post_process_masks(
            out.pred_masks.cpu(), inputs["original_sizes"].cpu(),
            inputs["reshaped_input_sizes"].cpu())[0].squeeze()
        if masks.dim() == 2:
            masks = masks[None]
        scores = np.atleast_1d(out.iou_scores.cpu().numpy().squeeze())
        k = int(np.argmax(scores))
        m = masks[k].numpy() > 0.5
        # 缩回内容域 640x360
        ms = cv2.resize(m.astype(np.uint8) * 255, (640, 360),
                        interpolation=cv2.INTER_NEAREST) > 0
        area = int(ms.sum())
        bbox_area = max(1, bb["w"] * bb["h"])
        ratio = area / bbox_area
        ys, xs = np.nonzero(ms)
        if len(xs) == 0:
            continue
        bw = int(xs.max() - xs.min() + 1)
        bh = int(ys.max() - ys.min() + 1)
        aspect = bh / max(bw, 1)
        # 独立于几何的亮度验证：车道线必须比周围沥青亮。
        # 排除「框到车身 / 阴影 / 护栏」的误标（SAM 只会照着错框抠）。
        small = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        V = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.float32)
        bg = cv2.boxFilter(V, -1, (61, 1), normalize=True)
        delta = float(np.median((V - bg)[ms])) if area else 0.0
        ok = (MIN_AREA <= area <= MAX_AREA and ratio < MAX_AREA_RATIO
              and ys.max() >= MIN_Y1 and bh >= MIN_H and aspect >= MIN_ASPECT
              and 180 <= xs.min() and xs.max() <= 430
              and delta >= MIN_BRIGHT_DELTA)
        if not ok:
            print(f"  剔除 {r['name']} area={area} h={bh} w={bw} "
                  f"h/w={aspect:.2f} y1={int(ys.max())} brightΔ={delta:.1f}")
            continue
        cv2.imwrite(str(OUT / "masks" / f"{r['name']}.png"),
                    (ms.astype(np.uint8) * 255))
        rows.append(dict(name=r["name"], session=r["session"], fid=r["fid"],
                         raw=str(fp), mask=f"masks/{r['name']}.png",
                         area=area, bbox_area=bbox_area,
                         ratio=round(float(ratio), 2),
                         x0=int(xs.min()), x1=int(xs.max()),
                         y0=int(ys.min()), y1=int(ys.max())))
        if i % 10 == 0:
            print(f"  {i}/{len(index)}，已收 {len(rows)}，"
                  f"{time.perf_counter() - t0:.0f}s", flush=True)

    (OUT / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    # 目检拼图
    for r in rows[:12]:
        im = cv2.imread(r["raw"])
        m = cv2.imread(str(OUT / r["mask"]), 0) > 127
        big = cv2.resize(m.astype(np.uint8) * 255, (im.shape[1], im.shape[0]),
                         interpolation=cv2.INTER_NEAREST) > 0
        im[big] = (0.3 * im[big] + 0.7 * np.array([255, 255, 0], np.float32)).astype(np.uint8)
        t = cv2.resize(im, (400, 225))
        cv2.putText(t, f"{r['name'][-16:]} a={r['area']}", (4, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
        tiles.append(t)
    while len(tiles) % 3:
        tiles.append(np.zeros_like(tiles[0]))
    sheet = np.vstack([np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)])
    cv2.imwrite(str(OUT / "qa_sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"完成 {len(rows)}/{len(index)} 帧，{time.perf_counter() - t0:.0f}s")
    print("产物:", OUT)


if __name__ == "__main__":
    main()
