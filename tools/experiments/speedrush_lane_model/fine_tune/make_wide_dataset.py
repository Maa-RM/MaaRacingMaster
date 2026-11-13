# -*- coding: utf-8 -*-
"""宽域正例集：把候选池从 8 会话 56 帧扩到全部会话，SAM 精修成线体掩码。

为什么扩（2026-11-12 连帧验证结论）：
细线版在 33 帧真值有线的帧里仍有 11 帧「无轨迹 / 短轨迹」——模型侧没形成
可用轨迹。训练正例只有 20 帧是直接原因，不是结构问题。

关键约束：**验证段必须整段排除**。连帧验证用 `20261109_104823_manual_p2`
的 0180~0260，若这些帧进了训练，验证就不再独立。f0204 同样 hold out。

流程：
1. 扫全部会话（STEP=3），用亮度真值 `make_target_labels.extract` 筛候选
   （真值只给 bbox，不给最终掩码）；
2. 按会话命中数分层抽样，总量约 TARGET 帧，避免全挤在同一片段；
3. SAM 以 bbox 为提示精修成线体掩码，过与 `make_sam_labels.py` 相同的质量闸
   （高度 / 面积 / 长宽比 / 底边 / 亮度差），宁缺毋滥。

用法（GPU 环境）：python fine_tune/make_wide_dataset.py [目标帧数]
产物：fine_tune_out/sam_labels_wide/{masks,index.json,qa_sheet.jpg}
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from transformers import SamModel, SamProcessor

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import make_target_labels as mtl  # noqa: E402

SAM_DIR = ROOT / "teachers" / "sam_vit_base"
RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
OUT = ROOT / "fine_tune_out" / "sam_labels_wide"
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "masks").mkdir(exist_ok=True)

STEP = 3
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 240

# 验证段整段排除（连帧验证 + 单帧验收），绝不进训练
HOLD_SESSION = "20261109_104823_manual_p2"
HOLD_FIDS = {"0204"}                       # 单帧验收
HOLD_RANGE = (180, 260)                    # 连帧验证段

PAD = 10
MAX_AREA_RATIO = 3.0
MIN_Y1 = 300
MIN_H = 60
MIN_ASPECT = 1.15
MIN_AREA = 900
MAX_AREA = 3600
MIN_BRIGHT_DELTA = 18


def is_holdout(sess, fid):
    if sess == HOLD_SESSION:
        if fid in HOLD_FIDS:
            return True
        if fid.isdigit() and HOLD_RANGE[0] <= int(fid) <= HOLD_RANGE[1]:
            return True
    return False


def scan_candidates():
    per = {}
    for d in sorted(RAW.iterdir()):
        if not d.is_dir():
            continue
        raw = d / "raw"
        if not raw.exists():
            continue
        found = []
        for fp in sorted(raw.glob("*.jpg"))[::STEP]:
            fid = fp.stem.split("_")[0]
            if is_holdout(d.name, fid):
                continue
            im = cv2.imread(str(fp))
            if im is None:
                continue
            s = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
            m, info = mtl.extract(s)
            if m is None:
                continue
            found.append((fp, fid, info))
        if found:
            per[d.name] = found
    return per


def main():
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={dev}", flush=True)
    per = scan_candidates()
    total_hit = sum(len(v) for v in per.values())
    print(f"候选 {total_hit} 帧 / {len(per)} 会话（已排除验证段 "
          f"{HOLD_SESSION} {HOLD_RANGE[0]}~{HOLD_RANGE[1]}）")

    picked = []
    for sess, found in sorted(per.items()):
        quota = max(3, round(len(found) * TARGET / max(1, total_hit)))
        quota = min(quota, len(found))
        idx = np.linspace(0, len(found) - 1, quota).round().astype(int)
        take = [found[int(i)] for i in idx]
        picked += [(sess, fp, fid, info) for fp, fid, info in take]
        print(f"  {sess}: 命中 {len(found)} → 取 {len(take)}")
    print(f"抽样合计 {len(picked)} 帧，开始 SAM 精修")

    model = SamModel.from_pretrained(str(SAM_DIR)).to(dev).eval()
    proc = SamProcessor.from_pretrained(str(SAM_DIR))
    rows, tiles = [], []
    t0 = time.perf_counter()

    for i, (sess, fp, fid, bb) in enumerate(picked, 1):
        im = cv2.imread(str(fp))
        if im is None:
            continue
        x0 = max(0, bb["x"] - PAD)
        y0 = max(0, bb["y"] - PAD)
        x1 = min(639, bb["x"] + bb["w"] + PAD)
        y1 = min(359, bb["y"] + bb["h"] + PAD)
        rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        inputs = proc(images=rgb, input_boxes=[[[x0 * 2, y0 * 2, x1 * 2, y1 * 2]]],
                      return_tensors="pt")
        inputs = {k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in inputs.items()}
        with torch.no_grad():
            out = model(**inputs, multimask_output=True)
        masks = proc.image_processor.post_process_masks(
            out.pred_masks.cpu(), inputs["original_sizes"].cpu(),
            inputs["reshaped_input_sizes"].cpu())[0].squeeze()
        if masks.dim() == 2:
            masks = masks[None]
        scores = np.atleast_1d(out.iou_scores.cpu().numpy().squeeze())
        m = masks[int(np.argmax(scores))].numpy() > 0.5
        ms = cv2.resize(m.astype(np.uint8) * 255, (640, 360),
                        interpolation=cv2.INTER_NEAREST) > 0
        area = int(ms.sum())
        ys, xs = np.nonzero(ms)
        if len(xs) == 0:
            continue
        bbox_area = max(1, bb["w"] * bb["h"])
        ratio = area / bbox_area
        bw = int(xs.max() - xs.min() + 1)
        bh = int(ys.max() - ys.min() + 1)
        aspect = bh / max(bw, 1)
        small = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        V = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.float32)
        bg = cv2.boxFilter(V, -1, (61, 1), normalize=True)
        delta = float(np.median((V - bg)[ms])) if area else 0.0
        ok = (MIN_AREA <= area <= MAX_AREA and ratio < MAX_AREA_RATIO
              and ys.max() >= MIN_Y1 and bh >= MIN_H and aspect >= MIN_ASPECT
              and 180 <= xs.min() and xs.max() <= 430
              and delta >= MIN_BRIGHT_DELTA)
        if not ok:
            continue
        name = f"{sess}_{fid}"
        cv2.imwrite(str(OUT / "masks" / f"{name}.png"),
                    (ms.astype(np.uint8) * 255))
        rows.append(dict(name=name, session=sess, fid=fid, raw=str(fp),
                         mask=f"masks/{name}.png", area=area, bbox_area=bbox_area,
                         ratio=round(float(ratio), 2), x0=int(xs.min()),
                         x1=int(xs.max()), y0=int(ys.min()), y1=int(ys.max())))
        if i % 40 == 0:
            print(f"  {i}/{len(picked)}，已收 {len(rows)}，"
                  f"{time.perf_counter() - t0:.0f}s", flush=True)

    (OUT / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    # 目检拼图：均匀抽 12 张（不只取前 12，避免只看同一会话）
    if rows:
        pick = [rows[int(i)] for i in
                np.linspace(0, len(rows) - 1, min(12, len(rows))).round().astype(int)]
        for r in pick:
            im = cv2.imread(r["raw"])
            m = cv2.imread(str(OUT / r["mask"]), 0) > 127
            big = cv2.resize(m.astype(np.uint8) * 255, (im.shape[1], im.shape[0]),
                             interpolation=cv2.INTER_NEAREST) > 0
            im[big] = (0.3 * im[big] + 0.7 * np.array([255, 255, 0],
                                                      np.float32)).astype(np.uint8)
            t = cv2.resize(im, (400, 225))
            cv2.putText(t, f"{r['name'][-18:]} a={r['area']}", (4, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
            tiles.append(t)
        while len(tiles) % 3:
            tiles.append(np.zeros_like(tiles[0]))
        sheet = np.vstack([np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)])
        cv2.imwrite(str(OUT / "qa_sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])

    print(f"完成 {len(rows)}/{len(picked)} 帧通过质量闸，"
          f"{time.perf_counter() - t0:.0f}s")
    print("产物:", OUT)


if __name__ == "__main__":
    main()
