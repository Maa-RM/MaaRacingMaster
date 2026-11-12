# -*- coding: utf-8 -*-
"""用大号离线教师（SegFormer-B2 车道标线分割）给游戏帧打**整幅**软标签。

与上一轮定向微调标签（`make_target_labels.py`）的根本区别：

- 旧标签只给「贴脸粗白虚线」一个正例，其余区域用现役模型自己的输出做
  「蒸馏保持」——没有明确的负例，模型学成「看见亮纹理就吐线」，原始分辨率
  目检在天空/空沥青出现大片野线（2026-11-12 否决点）。
- 本标签由**独立的大号教师**给出整幅 640x360 概率图：所有真实车道线为正例，
  天空/沥青/护栏/HUD 被明确判为负。教师只在离线跑一次，不进部署链路。
- 教师的标尺不依赖学生：SegFormer-B2 与 TwinLiteNet+ 无共同权重、无共同
  训练流程，满足实验区「标尺独立于待验证对象」。

教师：8bits-ai/SegFormer-Lane-Marking（MIT），权重在 teachers/（gitignored）。
帧源：bdd100k/images/{train,val}（898 帧，gitignored）。
产物：fine_tune_out/teacher_labels/{train,val}/*.png（0..255 概率）、index.json、
      qa_sheet.jpg（分批抽检拼图）、qa_metrics.csv。

自包含，不 import 业务码，不写本机绝对路径。
用法：在独立训练环境（torch + transformers + safetensors）下
      python fine_tune/make_teacher_labels.py
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from safetensors.torch import load_file
from transformers import SegformerConfig, SegformerForSemanticSegmentation

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEACHERS = ROOT / "teachers"
SRC = ROOT / "bdd100k" / "images"
OUT = ROOT / "fine_tune_out" / "teacher_labels"
OUT.mkdir(parents=True, exist_ok=True)

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
BATCH = 8
THRESH = 0.30

# 帧准入：开局/阶段起始/转场帧剔除（与金标准审计同口径）
MIN_FID = 40


def build_model(device):
    cfg = SegformerConfig.from_json_file(str(TEACHERS / "segformer_lane_config.json"))
    cfg.num_labels = 2
    cfg.id2label = {0: "background", 1: "lane_marking"}
    cfg.label2id = {"background": 0, "lane_marking": 1}
    model = SegformerForSemanticSegmentation(cfg)
    sd = load_file(str(TEACHERS / "segformer_lane.safetensors"))
    missing, unexpected = model.load_state_dict(sd, strict=False)
    if missing or unexpected:
        raise RuntimeError(f"权重不匹配 missing={missing[:3]} unexpected={unexpected[:3]}")
    return model.eval().to(device)


@torch.no_grad()
def infer_batch(model, frames, device):
    """frames: list of 640x360 BGR content -> list of 360x640 float32 prob."""
    canvas = np.empty((len(frames), 3, 384, 640), np.float32)
    for i, c in enumerate(frames):
        rgb = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)
        pad = np.full((384, 640, 3), 114, np.uint8)
        pad[:360] = rgb
        canvas[i] = ((pad.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)
    t = torch.from_numpy(canvas).to(device)
    logits = model(pixel_values=t).logits
    prob = torch.softmax(logits, dim=1)[:, 1:2]
    prob = torch.nn.functional.interpolate(
        prob, size=(384, 640), mode="bilinear", align_corners=False
    )[:, 0, :360]
    return [p.cpu().numpy() for p in prob]


def frame_ok(name, content):
    """返回 (是否准入, 原因)。开局/转场/大面积遮挡帧不入训练集。"""
    parts = name.split("_")
    try:
        fid = int(parts[-2])
    except (ValueError, IndexError):
        fid = -1
    if 0 <= fid < MIN_FID:
        return False, f"开局帧 fid={fid}"
    hsv = cv2.cvtColor(content, cv2.COLOR_BGR2HSV)
    H, S = hsv[:, :, 0], hsv[:, :, 1]
    blue = float(((H[200:] > 90) & (H[200:] < 130) & (S[200:] > 140)).mean())
    if blue > 0.08:
        return False, f"转场/特效 blue={blue:.3f}"
    return True, ""


def main():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={device}", flush=True)
    model = build_model(device)
    index, metrics = [], []
    t0 = time.perf_counter()

    for split in ("train", "val"):
        (OUT / split).mkdir(exist_ok=True)
        files = sorted((SRC / split).glob("*.jpg"))
        print(f"[{split}] {len(files)} 帧", flush=True)
        buf, names = [], []
        accepted = rejected = 0

        def flush():
            nonlocal buf, names, accepted
            if not buf:
                return
            probs = infer_batch(model, buf, device)
            for nm, im, p in zip(names, buf, probs):
                hit = p > THRESH
                sky = hit[:165].mean()
                # 教师在天空区出现连片响应 = 转场/特效叠影污染，整帧剔除。
                # 判据来自教师输出本身，不另造像素启发式（太阳会被误判）。
                if sky > 0.001:
                    continue
                # 教师输出与生产同口径：内容域 640x360，0..255 存概率
                png = (np.clip(p, 0, 1) * 255).astype(np.uint8)
                cv2.imwrite(str(OUT / split / f"{nm}.png"), png)
                road = hit[165:].mean()
                index.append(dict(split=split, name=nm,
                                  img=f"../../bdd100k/images/{split}/{nm}.jpg",
                                  label=f"teacher_labels/{split}/{nm}.png",
                                  pos=int(hit.sum()), sky=round(float(sky), 5),
                                  road=round(float(road), 5),
                                  pmax=round(float(p.max()), 4)))
                metrics.append(dict(split=split, name=nm, pos=int(hit.sum()),
                                    sky=round(float(sky), 5), road=round(float(road), 5),
                                    pmax=round(float(p.max()), 4)))
                accepted += 1
            buf, names = [], []

        for fp in files:
            nm = fp.stem
            ok, why = frame_ok(nm, cv2.resize(cv2.imread(str(fp)), (640, 360)))
            if not ok:
                rejected += 1
                continue
            im = cv2.imread(str(fp))
            if im is None:
                continue
            content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
            buf.append(content)
            names.append(nm)
            if len(buf) >= BATCH:
                flush()
        flush()
        print(f"  [{split}] 准入 {accepted}，剔除 {rejected}", flush=True)

    (OUT / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    with (OUT / "qa_metrics.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["split", "name", "pos", "sky", "road", "pmax"])
        w.writeheader()
        w.writerows(metrics)

    # 抽检拼图：低/中/高响应各取若干，叠到原帧上供原始分辨率目检
    picks = []
    ordered = sorted(index, key=lambda r: r["pos"])
    for q in (0.02, 0.15, 0.35, 0.55, 0.75, 0.92, 0.99):
        picks.append(ordered[min(len(ordered) - 1, int(q * len(ordered)))])
    tiles = []
    for r in picks:
        im = cv2.imread(str((OUT / r["img"]).resolve()))
        lab = cv2.imread(str(OUT / r["split"] / f"{r['name']}.png"), 0)
        if im is None or lab is None:
            continue
        t = cv2.resize(im, (400, 225))
        big = cv2.resize((lab > int(THRESH * 255)).astype(np.uint8) * 255,
                         (400, 225), interpolation=cv2.INTER_NEAREST) > 0
        t[big] = (0.3 * t[big] + 0.7 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
        cv2.putText(t, f"{r['name'][:22]} pos={r['pos']}", (5, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        tiles.append(t)
    while len(tiles) % 3:
        tiles.append(np.zeros_like(tiles[0]))
    sheet = np.vstack([np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)])
    cv2.imwrite(str(OUT / "qa_sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])

    pos = np.array([m["pos"] for m in metrics])
    sky = np.array([m["sky"] for m in metrics])
    print(f"完成 {len(index)} 帧，{time.perf_counter() - t0:.0f}s")
    print(f"正例像素 p10/p50/p90 = {np.percentile(pos, [10, 50, 90]).round(0)}，"
          f"天空假阳性帧数 {int((sky > 0.001).sum())}")
    print("产物:", OUT)


if __name__ == "__main__":
    main()
