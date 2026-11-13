# -*- coding: utf-8 -*-
"""用 UnLanedet 的 SCNN（ResNet18，TuSimple，6 条车道线实例分割）当离线教师，
在贴脸盲区帧上量它是否看得见、以及输出是线体还是描边。

为什么换它：SegFormer 那个社区权重实测在**描边**（只压线的左右边缘，线体
不覆盖），把它的整幅输出蒸馏进小模型会连标注风格一起复制（2026-11-12 结论）。
SCNN 是 TuSimple 的 6 类实例分割，标注是点列转成的实心线，口径上是线体。

结构（与 UnLanedet 源码一致，自包含实现，不依赖 detectron2）：
  ResNet18(replace_stride_with_dilation=[F,T,T], out_conv 512->128)
  -> SCNN_AGG(四方向循环卷积) -> conv8(128->7) -> 双线性上采样到 368x640
预处理（config/scnn/resnet18_tusimple.py）：
  cut_height=160、resize 640x368、BGR 减 ImageNet 均值、std=1、不除 255。

用法：在 GPU 训练环境下 python probe_teacher_scnn.py
产物：first_look_out/teacher_scnn/ 下原始分辨率叠图与读数
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
TEACHERS = HERE / "teachers"
OUT = HERE / "first_look_out" / "teacher_scnn"
OUT.mkdir(parents=True, exist_ok=True)

RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug"
FRAMES = [
    ("f0204", RAW / "speedrush/20261109_104823_manual_p2/raw/0204_raw.jpg",
     (258, 282, 265, 360), "黄昏·维护者指认"),
    ("f0202", RAW / "20261110_204716/speedrush/drive/20261110_205224_p1/raw/0202_raw.jpg",
     (310, 339, 253, 360), "用户给出帧"),
    ("f0500", RAW / "speedrush/20261109_104823_manual_p2/raw/0500_raw.jpg",
     None, "同局常规帧"),
]
REG = {"right": (500, 635, 270, 325), "yellow": (15, 240, 215, 359),
       "sky": (0, 640, 0, 165)}
CUT, IH, IW = 160, 368, 640
MEAN = np.array([103.939, 116.779, 123.68], np.float32)  # BGR


class SCNNAgg(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv_d = nn.Conv2d(128, 128, (1, 9), padding=(0, 4), bias=False)
        self.conv_u = nn.Conv2d(128, 128, (1, 9), padding=(0, 4), bias=False)
        self.conv_r = nn.Conv2d(128, 128, (9, 1), padding=(4, 0), bias=False)
        self.conv_l = nn.Conv2d(128, 128, (9, 1), padding=(4, 0), bias=False)

    def forward(self, x):
        x = x.clone()
        for i in range(1, x.shape[2]):
            x[..., i:i + 1, :] = x[..., i:i + 1, :].clone() + F.relu(
                self.conv_d(x[..., i - 1:i, :].clone()))
        for i in range(x.shape[2] - 2, 0, -1):
            x[..., i:i + 1, :] = x[..., i:i + 1, :].clone() + F.relu(
                self.conv_u(x[..., i + 1:i + 2, :].clone()))
        for i in range(1, x.shape[3]):
            x[..., i:i + 1] = x[..., i:i + 1].clone() + F.relu(
                self.conv_r(x[..., i - 1:i].clone()))
        for i in range(x.shape[3] - 2, 0, -1):
            x[..., i:i + 1] = x[..., i:i + 1].clone() + F.relu(
                self.conv_l(x[..., i + 1:i + 2].clone()))
        return x


def conv3x3(inp, outp, stride=1, dilation=1):
    return nn.Conv2d(inp, outp, 3, stride=stride,
                     padding=dilation, dilation=dilation, bias=False)


class BasicBlock(nn.Module):
    """与 UnLanedet 一致：BasicBlock 支持 dilation（torchvision 版不支持）。"""
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None, dilation=1):
        super().__init__()
        self.conv1 = conv3x3(inplanes, planes, stride, dilation)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes, dilation=dilation)
        self.bn2 = nn.BatchNorm2d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x
        out = self.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        if self.downsample is not None:
            identity = self.downsample(x)
        return self.relu(out + identity)


class ResNet18Dilated(nn.Module):
    """ResNet18，layer3/4 用膨胀卷积替代下采样（输出 stride 8）。"""

    def __init__(self, rswd=(False, True, True), chans=(64, 128, 256, 512)):
        super().__init__()
        self.dilation = 1
        self.inplanes = 64
        self.conv1 = nn.Conv2d(3, 64, 7, stride=2, padding=3, bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(3, stride=2, padding=1)
        # 与 config 一致：rswd=[False, True, True] 作用于 layer2/3/4
        self.layer1 = self._make_layer(chans[0], 2, 1, False)
        self.layer2 = self._make_layer(chans[1], 2, 2, rswd[0])
        self.layer3 = self._make_layer(chans[2], 2, 2, rswd[1])
        self.layer4 = self._make_layer(chans[3], 2, 2, rswd[2])

    def _make_layer(self, planes, blocks, stride, dilate):
        downsample = None
        prev_dilation = self.dilation
        if dilate:
            self.dilation *= stride
            stride = 1
        if stride != 1 or self.inplanes != planes:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes, 1, stride=stride, bias=False),
                nn.BatchNorm2d(planes))
        layers = [BasicBlock(self.inplanes, planes, stride, downsample,
                             dilation=prev_dilation if dilate else 1)]
        self.inplanes = planes
        for _ in range(1, blocks):
            layers.append(BasicBlock(self.inplanes, planes,
                                     dilation=self.dilation if dilate else 1))
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.maxpool(self.relu(self.bn1(self.conv1(x))))
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        return self.layer4(x)


class SCNNTeacher(nn.Module):
    """键名对齐 UnLanedet: backbone.model.* / backbone.out.* / aggregator.* / head.decoder.conv8.*"""

    def __init__(self, num_classes=7):
        super().__init__()
        self.backbone_model = ResNet18Dilated()
        self.out = nn.Conv2d(512, 128, 1, bias=False)
        self.aggregator = SCNNAgg()
        self.conv8 = nn.Conv2d(128, num_classes, 1)

    def load(self, path):
        d = torch.load(path, map_location="cpu", weights_only=False)
        sd = d["model"]
        own = self.state_dict()
        mapping = {}
        for k, v in sd.items():
            if k.startswith("backbone.model."):
                mapping[k.replace("backbone.model.", "backbone_model.")] = v
            elif k.startswith("backbone.out."):
                mapping[k.replace("backbone.out.", "out.")] = v
            elif k.startswith("aggregator."):
                mapping[k] = v
            elif k.startswith("head.decoder.conv8."):
                mapping[k.replace("head.decoder.conv8.", "conv8.")] = v
        missing = [k for k in own if k not in mapping]
        used = {k: v for k, v in mapping.items() if k in own}
        own.update(used)
        self.load_state_dict(own)
        print(f"  权重载入 {len(used)}/{len(own)}，未匹配 {len(missing)}：{missing[:5]}")

    def forward(self, x):
        m = self.backbone_model
        x = m.conv1(x); x = m.bn1(x); x = m.relu(x); x = m.maxpool(x)
        x = m.layer1(x); x = m.layer2(x); x = m.layer3(x); x = m.layer4(x)
        x = self.out(x)
        x = self.aggregator(x)
        x = self.conv8(x)
        return F.interpolate(x, size=(IH, IW), mode="bilinear", align_corners=False)


def preprocess(bgr):
    h, w = bgr.shape[:2]
    crop = bgr[CUT:, :, :] if h > CUT else bgr
    r = cv2.resize(crop, (IW, IH), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    # 实测口径：先 /255 再减 mean/255。直接减 0-255 量级的均值会让 logits
    # 爆到 ±500、softmax 全判背景（2026-11-12 调试：前景像素 0 vs 15694）。
    r = r / 255.0 - MEAN / 255.0
    return torch.from_numpy(r.transpose(2, 0, 1)[None])


@torch.no_grad()
def lane_prob(model, bgr, dev):
    logits = model(preprocess(bgr).to(dev))          # (1,7,368,640)
    prob = torch.softmax(logits, dim=1)[0]           # 7 类
    any_lane = prob[1:].sum(0)                       # 任意一条线的概率
    p = F.interpolate(any_lane[None, None], size=(720, 1280),
                      mode="bilinear", align_corners=False)[0, 0]
    p = p.cpu().numpy()
    # 顶部被裁掉的 160 行无输出，置 0
    p[:CUT] = 0.0
    return cv2.resize(p, (640, 360), interpolation=cv2.INTER_AREA)


def stat(p, box):
    if box is None:
        return None
    x0, x1, y0, y1 = box
    q = p[y0:y1, x0:x1]
    return round(float(q.max()), 4), int((q > .3).sum())


def main():
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = SCNNTeacher().to(dev).eval()
    model.load(str(TEACHERS / "scnn_model_best_tusimple.pth"))
    print(f"device={dev}")
    rows = []
    for tag, fp, box, desc in FRAMES:
        if not fp.exists():
            print(tag, "MISSING")
            continue
        im = cv2.imread(str(fp))
        p = lane_prob(model, im, dev)
        rows.append(dict(tag=tag, desc=desc, target=stat(p, box),
                         right=stat(p, REG["right"]), yellow=stat(p, REG["yellow"]),
                         sky=stat(p, REG["sky"]), glob=int((p > .3).sum()),
                         pmax=round(float(p.max()), 4)))
        print(f"\n{tag} [{desc}] max={p.max():.4f} >0.3={int((p > .3).sum())}")
        print(f"  目标 {stat(p, box)} | 右侧 {stat(p, REG['right'])} | "
              f"黄线 {stat(p, REG['yellow'])} | 天空 {stat(p, REG['sky'])}")
        vis = im.copy()
        big = cv2.resize((p > .3).astype(np.uint8) * 255, (1280, 720),
                         interpolation=cv2.INTER_NEAREST) > 0
        vis[big] = (0.3 * vis[big] + 0.7 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
        if box:
            x0, x1, y0, y1 = box
            cv2.rectangle(vis, (x0 * 2, y0 * 2), (x1 * 2, y1 * 2), (0, 0, 255), 2)
        cv2.putText(vis, f"{tag} SCNN(ResNet18,TuSimple) red=target ROI",
                    (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2)
        cv2.imwrite(str(OUT / f"{tag}_scnn.jpg"), vis, [cv2.IMWRITE_JPEG_QUALITY, 92])
        cv2.imwrite(str(OUT / f"{tag}_prob.png"), (np.clip(p, 0, 1) * 255).astype(np.uint8))
    import json
    (OUT / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print("\n产物:", OUT)


if __name__ == "__main__":
    main()
