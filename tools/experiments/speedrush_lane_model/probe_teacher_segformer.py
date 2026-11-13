# -*- coding: utf-8 -*-
"""大号分割教师（SegFormer-B2 车道标线）在贴脸盲区帧上的标注质量探针。

背景（2026-11-12）：维护者不接受「两次推理融合」，也不放心只给目标线一个
正例的定向微调（野线）。新提案 = 找大号专业车道线模型做**离线标注**，
再微调现役 TwinLiteNet+ Small；教师不进部署链路，推理仍是单次小模型。

本探针回答决定性的一问：教师能否给出「整幅图完整、几何一致、背景明确为
负」的车道线标注？顺带量它是否也看不见贴脸构型（看不见不代表路线失败，
目标线由亮度几何真值补；但看得见则更强）。

模型：8bits-ai/SegFormer-Lane-Marking（MIT，SegFormer-B2 级，109MB，
输出 2 类 = 背景 / 车道标线的整幅概率图）。权重与 config 在 teachers/。

自包含：不 import 业务码，不写本机绝对路径。
用法：在独立训练环境（torch + transformers + safetensors）下
      python probe_teacher_segformer.py
产物：first_look_out/teacher/ 下的原始分辨率叠图与三联对比图、summary.md
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import torch
from safetensors.torch import load_file
from transformers import SegformerConfig, SegformerForSemanticSegmentation

HERE = Path(__file__).resolve().parent
TEACHERS = HERE / "teachers"
OUT = HERE / "first_look_out" / "teacher"
OUT.mkdir(parents=True, exist_ok=True)

RAW_ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster/debug"
FRAMES = [
    ("f0204", RAW_ROOT / "speedrush/20261109_104823_manual_p2/raw/0204_raw.jpg",
     (258, 282, 265, 360), "黄昏·维护者指认帧"),
    ("f0202", RAW_ROOT / "20261110_204716/speedrush/drive/20261110_205224_p1/raw/0202_raw.jpg",
     (310, 339, 253, 360), "用户给出帧"),
    ("f0500", RAW_ROOT / "speedrush/20261109_104823_manual_p2/raw/0500_raw.jpg",
     None, "同局常规帧（无目标构型）"),
]

# 内容域 ROI，用于回归/背景判读
REG = {
    "right": (500, 635, 270, 325),   # 右侧已检出的斜白线
    "yellow": (15, 240, 215, 359),   # 左黄线走廊
    "sky": (0, 640, 0, 165),         # 天空/远景（应为纯背景）
    "asphalt": (330, 430, 280, 355),  # 中央偏右空沥青（应为纯背景）
}
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def build_model():
    cfg = SegformerConfig.from_json_file(str(TEACHERS / "segformer_lane_config.json"))
    cfg.num_labels = 2
    cfg.id2label = {0: "background", 1: "lane_marking"}
    cfg.label2id = {"background": 0, "lane_marking": 1}
    model = SegformerForSemanticSegmentation(cfg)
    sd = load_file(str(TEACHERS / "segformer_lane.safetensors"))
    missing, unexpected = model.load_state_dict(sd, strict=False)
    print(f"权重载入: tensors={len(sd)} missing={len(missing)} unexpected={len(unexpected)}")
    if missing:
        print("  missing 样例:", missing[:5])
    if unexpected:
        print("  unexpected 样例:", unexpected[:5])
    model.eval()
    return model


@torch.no_grad()
def infer(model, bgr):
    """返回内容域 640x360 的车道标线概率图。"""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    small = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    # SegFormer 总下采样 32；pad 到 384 高，用 114/255 灰边与生产 letterbox 同口径
    canvas = np.full((384, 640, 3), 114, np.uint8)
    canvas[:360] = small
    x = (canvas.astype(np.float32) / 255.0 - MEAN) / STD
    t = torch.from_numpy(x.transpose(2, 0, 1)[None])
    logits = model(pixel_values=t).logits           # (1,2,96,160)
    prob = torch.softmax(logits, dim=1)[0, 1]
    prob = torch.nn.functional.interpolate(
        prob[None, None], size=(384, 640), mode="bilinear", align_corners=False
    )[0, 0, :360].cpu().numpy()
    return prob


def stat(p, box):
    if box is None:
        return None
    x0, x1, y0, y1 = box
    q = p[y0:y1, x0:x1]
    return round(float(q.max()), 4), round(float(q.mean()), 4), int((q > 0.3).sum())


def overlay(bgr, mask, color=(0, 255, 0)):
    vis = bgr.copy()
    vis[mask] = (0.3 * vis[mask] + 0.7 * np.array(color, np.float32)).astype(np.uint8)
    return vis


def main():
    model = build_model()
    rows = []
    for tag, fp, target_box, desc in FRAMES:
        if not fp.exists():
            print(f"{tag}: MISSING {fp}")
            continue
        im = cv2.imread(str(fp))
        p = infer(model, im)
        hit = (p > 0.3).astype(np.uint8)
        big = cv2.resize(hit * 255, (1280, 720), interpolation=cv2.INTER_NEAREST) > 0

        tv = stat(p, target_box)
        rv, yv, sv, av = (stat(p, REG[k]) for k in ("right", "yellow", "sky", "asphalt"))
        rows.append(dict(tag=tag, desc=desc, target=tv, right=rv, yellow=yv,
                         sky=sv, asphalt=av, global_px=int((p > 0.3).sum()),
                         max=round(float(p.max()), 4)))
        print(f"\n{tag} [{desc}] 全图>0.3={int((p > 0.3).sum())} max={p.max():.4f}")
        print(f"  目标线 {tv} | 右侧线 {rv} | 黄线 {yv} | 天空 {sv} | 空沥青 {av}")

        # 原始分辨率标注叠图
        vis = overlay(im, big, (0, 255, 0))
        if target_box:
            x0, x1, y0, y1 = target_box
            cv2.rectangle(vis, (x0 * 2, y0 * 2), (x1 * 2, y1 * 2), (0, 0, 255), 2)
        cv2.putText(vis, f"{tag} SegFormer-B2 teacher  red=target ROI",
                    (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        cv2.imwrite(str(OUT / f"{tag}_teacher.jpg"), vis, [cv2.IMWRITE_JPEG_QUALITY, 92])
        cv2.imwrite(str(OUT / f"{tag}_prob.png"), (np.clip(p, 0, 1) * 255).astype(np.uint8))

    (OUT / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n产物:", OUT)


if __name__ == "__main__":
    main()
