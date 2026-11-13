# -*- coding: utf-8 -*-
"""微调 TwinLiteNetPlus(small) 车道头：移植权重起点 + 游戏伪标签。

口径与上游 train_singletask 对齐：
- 输入 letterbox 384x640（114 灰边）/255，标签 640x360 灰度>1 取前景，双通道目标；
- 损失 Tversky(α=0.9, γ=1.333) + Focal(α=0.25, γ=2.0)，在裁掉上下 12 行灰边后计算
  （loss.py SigleLoss 口径，超参取上游 hyperparameters/twinlitev2_hyper.yaml）；
- 冻结 encoder/CAAM/路面头与所有冻结段的 BatchNorm 统计（.eval()），只训
  up_1_ll / up_2_ll / out_ll（官方 issue #7 作者建议的解冻范围）；
- AdamW lr=1e-4 poly 衰减，30 轮，翻转 p=0.5 + HSV p=0.5（其余增广依赖
  albumentations/skimage，不引入，放弃）；
- 每轮在留出会话（095756_p2）上算车道类 IoU/像素准确率，按最优存 best。

自包含（不 import 业务码）。用法：python train.py [epochs]
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UP = ROOT / "upstream"
DATA = ROOT / "bdd100k"
OUT = ROOT / "fine_tune_out"
INIT = ROOT / "weights/plus_small_ft_init.pth"

_mpl = types.ModuleType("matplotlib")
_mpl.pyplot = types.ModuleType("matplotlib.pyplot")
sys.modules.setdefault("matplotlib", _mpl)
sys.modules.setdefault("matplotlib.pyplot", _mpl.pyplot)
sys.path.insert(0, str(UP))

import cv2  # noqa: E402
import torch  # noqa: E402
import torch.nn as nn  # noqa: E402

from model.model import TwinLiteNetPlus  # noqa: E402
from loss import TverskyLoss, FocalLossSeg  # noqa: E402  # 上游官方损失

MAX_EPOCHS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DEV = sys.argv[2] if len(sys.argv) > 2 else ("cuda" if torch.cuda.is_available() else "cpu")
PRESET = sys.argv[3] if len(sys.argv) > 3 else "heads"      # heads | caam | full
ALPHA2 = float(sys.argv[4]) if len(sys.argv) > 4 else 0.9   # Tversky 漏检权重 = 1-ALPHA2
LAM_NEAR = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0  # 近带辅助损失权重（0=关）
BATCH = 16 if DEV == "cuda" else 8
LR = 1e-4
WD = 5e-4
SEED = 0
TRAIN_HEADS = ("up_1_ll", "up_2_ll", "out_ll")
TRAIN_CAAM = ("caam", "conv_caam") + TRAIN_HEADS


def letterbox(im: np.ndarray, size=(384, 640)) -> np.ndarray:
    H, W = im.shape[:2]
    r = min(size[0] / H, size[1] / W)
    nw, nh = int(round(W * r)), int(round(H * r))
    im = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
    dh, dw = size[0] - nh, size[1] - nw
    top, bot = dh // 2, dh - dh // 2
    left, right = dw // 2, dw - dw // 2
    return cv2.copyMakeBorder(im, top, bot, left, right, cv2.BORDER_CONSTANT,
                              value=(114, 114, 114))


def augment_hsv(img: np.ndarray) -> None:
    r = np.random.uniform(-1, 1, 3) * [0.015, 0.7, 0.4] + 1
    hue, sat, val = cv2.split(cv2.cvtColor(img, cv2.COLOR_BGR2HSV))
    x = np.arange(256, dtype=np.int16)
    lut = [((x * r[0]) % 180).astype(np.uint8), np.clip(x * r[1], 0, 255).astype(np.uint8),
           np.clip(x * r[2], 0, 255).astype(np.uint8)]
    hs = cv2.merge((cv2.LUT(hue, lut[0]), cv2.LUT(sat, lut[1]), cv2.LUT(val, lut[2])))
    cv2.cvtColor(hs, cv2.COLOR_HSV2BGR, dst=img)


class LaneSet(torch.utils.data.Dataset):
    def __init__(self, split: str, train: bool):
        self.names = sorted((DATA / "images" / split).glob("*.jpg"))
        self.train = train

    def __len__(self):
        return len(self.names)

    def __getitem__(self, i: int):
        fp = self.names[i]
        img = cv2.imread(str(fp))
        lbl = cv2.imread(str(fp).replace("images", "lane_line_annotations")
                         .replace(".jpg", ".png"), 0)
        if self.train:
            if random.random() < 0.5:
                img = np.ascontiguousarray(np.fliplr(img))
                lbl = np.ascontiguousarray(np.fliplr(lbl))
            if random.random() < 0.5:
                augment_hsv(img)
        img = letterbox(img)
        lbl = cv2.resize(lbl, (640, 360))
        fg = (lbl > 1).astype(np.float32)
        target = np.stack([1.0 - fg, fg])          # [2,360,640] 背景在前
        chw = np.ascontiguousarray(img[:, :, ::-1].transpose(2, 0, 1))
        return torch.from_numpy(chw), torch.from_numpy(target)


def val_pass(model, loader) -> tuple[float, float, float, float]:
    model.eval()
    tp = fp = fn = 0
    tp_n = fp_n = fn_n = 0
    with torch.no_grad():
        for img, tgt in loader:
            img, tgt = img.to(DEV), tgt.to(DEV)
            out_da, out_ll = model(img.float() / 255.0)
            pred = out_ll[:, :, 12:-12].argmax(1)
            gt = tgt.argmax(1)
            tp += int(((pred == 1) & (gt == 1)).sum())
            fp += int(((pred == 1) & (gt == 0)).sum())
            fn += int(((pred == 0) & (gt == 1)).sum())
            pred_n, gt_n = pred[:, 300:], gt[:, 300:]      # 近带专项（行 300-360）
            tp_n += int(((pred_n == 1) & (gt_n == 1)).sum())
            fp_n += int(((pred_n == 1) & (gt_n == 0)).sum())
            fn_n += int(((pred_n == 0) & (gt_n == 1)).sum())
    iou = tp / max(1, tp + fp + fn)
    acc = tp / max(1, tp + fn)
    iou_near = tp_n / max(1, tp_n + fp_n + fn_n)
    acc_near = tp_n / max(1, tp_n + fn_n)
    return iou, acc, iou_near, acc_near


def main():
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.set_num_threads(os.cpu_count() or 8)
    OUT.mkdir(exist_ok=True)
    log_path = OUT / "train_log.txt"
    log = open(log_path, "a", encoding="utf-8")

    def say(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    model = TwinLiteNetPlus(types.SimpleNamespace(config="small"))
    sd = torch.load(INIT, map_location="cpu", weights_only=True)
    model.load_state_dict(sd)
    model.to(DEV)
    say(f"起点权重载入: {INIT.name}，设备 {DEV}")

    heads = []
    if PRESET == "heads":
        trainable_names = TRAIN_HEADS
    elif PRESET == "caam":
        trainable_names = TRAIN_CAAM
    else:
        trainable_names = None      # full：整网解冻
    for name, mod in model.named_children():
        trainable = trainable_names is None or name in trainable_names
        for p in mod.parameters():
            p.requires_grad_(trainable)
        if trainable:
            heads.append(name)
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    say(f"可训练参数: {n_train}（{heads}），其余冻结；Tversky α2={ALPHA2}")

    tversky = TverskyLoss(mode="multiclass", alpha=ALPHA2, beta=1 - ALPHA2,
                          gamma=1.33333333333, from_logits=True)
    tversky_near = TverskyLoss(mode="multiclass", alpha=0.7, beta=0.3,
                               gamma=1.33333333333, from_logits=True)
    focal = FocalLossSeg(mode="multiclass", alpha=0.25, gamma=2.0)

    train_ld = torch.utils.data.DataLoader(
        LaneSet("train", True), batch_size=BATCH, shuffle=True, num_workers=2,
        drop_last=True)
    val_ld = torch.utils.data.DataLoader(LaneSet("val", False), batch_size=BATCH)

    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=LR, betas=(0.9, 0.999), eps=1e-8, weight_decay=WD)

    best = -1.0
    for epoch in range(MAX_EPOCHS):
        lr_e = round(LR * (1 - epoch / MAX_EPOCHS) ** 1.5, 8)
        for g in opt.param_groups:
            g["lr"] = lr_e
        model.train()
        for name, mod in model.named_children():   # 冻结段 BN 统计一并冻结
            if trainable_names is not None and name not in trainable_names:
                mod.eval()
        t0 = time.perf_counter()
        tot = n_it = 0
        for it, (img, tgt) in enumerate(train_ld):
            img, tgt = img.to(DEV), tgt.to(DEV)
            opt.zero_grad()
            out_da, out_ll = model(img.float() / 255.0)
            seg = tgt.argmax(1)
            out_c = out_ll[:, :, 12:-12]      # 384→360，与 360 行标签对齐（上游口径）
            gt = seg
            loss = tversky(out_c, gt) + focal(out_c, gt)
            if LAM_NEAR > 0:
                # 近带专属目标项：近带只占全图像素 0.2%，全图平均会把它的梯度稀释
                # 到噪音级（实测训练帧自身都拟合不上）；这里给它独立的一份
                # Tversky(α=0.7 偏召回)+Focal，误报由提取层几何门把关。
                out_n, gt_n = out_c[:, :, 252:], gt[:, 252:]
                loss = loss + LAM_NEAR * (tversky_near(out_n, gt_n)
                                          + focal(out_n, gt_n))
            loss.backward()
            opt.step()
            tot += float(loss)
            n_it += 1
            if it % 20 == 0:
                say(f"  e{epoch} it{it}/{len(train_ld)} loss={float(loss):.4f}")
        iou, acc, iou_near, acc_near = val_pass(model, val_ld)
        say(f"epoch {epoch}: lr={lr_e} loss={tot / max(1, n_it):.4f} "
            f"val_IoU={iou:.4f} val_acc={acc:.4f} "
            f"near_IoU={iou_near:.4f} near_acc={acc_near:.4f} "
            f"({time.perf_counter() - t0:.0f}s)")
        state = {"epoch": epoch, "val_IoU": iou, "val_IoU_near": iou_near,
                 "device": DEV, "preset": PRESET, "alpha2": ALPHA2,
                 "lam_near": LAM_NEAR,
                 "state_dict": {k: v.cpu() for k, v in model.state_dict().items()}}
        torch.save(state, OUT / f"ft_small_{PRESET}_last.pth")
        if iou + iou_near > best:
            best = iou + iou_near
            torch.save(state, OUT / f"ft_small_{PRESET}_best.pth")
            say(f"  best 更新: val_IoU={best:.4f}")
    say(f"训练结束，best val_IoU={best:.4f}")
    log.close()


if __name__ == "__main__":
    main()
