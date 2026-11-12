# -*- coding: utf-8 -*-
"""专项中心学习：只用 SAM 线体标签，在**中心区域**训练现役 TwinLiteNet+ Small。

维护者 2026-11-12 提案：「叫分割甚至语义模型来标注，专项做中心位置的车道线学习」。

与前几版的根本区别（也是针对野线的设计）：
- 标签来自 SAM（类别无关的通用分割）的**线体掩码**，不是描边、不是模型自举；
- **损失只在中心 ROI 内计算**（内容域 x 170~440、y 240~360），ROI 外完全不
  参与。前几版整图迁移导致「线变粗成糊带 + 天空野线」，这版从损失层面就不
  让模型去动 ROI 外的东西；
- 正负例成对：正例帧 = SAM 有目标线（ROI 内掩码为正、其余为负）；
  负例帧 = 常规帧（ROI 内全为负），防止模型学成「中心永远有线」。

用法：在 GPU 训练环境下 python fine_tune/train_center_specialist.py [steps]
产物：fine_tune_out/center_best.pth、center_log.txt
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UP = ROOT / "upstream"
SAM_LABELS = ROOT / "fine_tune_out" / "sam_labels"
BDD = ROOT / "bdd100k" / "images" / "train"
OUT = ROOT / "fine_tune_out"
INIT = ROOT / "weights" / "plus_small_ft_init.pth"
NATIVE = (ROOT.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
          / "plus_small_384x640.onnx")

RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug"
EVAL = [
    ("f0204", RAW / "speedrush/20261109_104823_manual_p2/raw/0204_raw.jpg",
     (258, 282, 265, 360)),
    ("f0202", RAW / "20261110_204716/speedrush/drive/20261110_205224_p1/raw/0202_raw.jpg",
     (310, 339, 253, 360)),
    ("f0500", RAW / "speedrush/20261109_104823_manual_p2/raw/0500_raw.jpg", None),
]
# 中心 ROI：目标线可能出现的横向范围 + 近场纵向范围
ROI = (170, 440, 240, 360)
REG = {"right": (500, 635, 270, 325), "yellow": (15, 240, 215, 359),
       "sky": (0, 640, 0, 165)}
TRAIN_HEADS = ("up_1_ll", "up_2_ll", "out_ll")
STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 150
BATCH = 8
LR = 3e-4
SEED = 0
N_NEG = 120

_mpl = types.ModuleType("matplotlib")
_mpl.pyplot = types.ModuleType("matplotlib.pyplot")
sys.modules.setdefault("matplotlib", _mpl)
sys.modules.setdefault("matplotlib.pyplot", _mpl.pyplot)
sys.path.insert(0, str(UP))

import onnxruntime as ort  # noqa: E402
import torch  # noqa: E402
from model.model import TwinLiteNetPlus  # noqa: E402


def letterbox(content):
    im = cv2.copyMakeBorder(content, 12, 12, 0, 0,
                            cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1))


def roi_mask():
    m = np.zeros((360, 640), np.float32)
    x0, x1, y0, y1 = ROI
    m[y0:y1, x0:x1] = 1.0
    return m


def native_prob(sess, key, content):
    x = letterbox(content)[None]
    lo = sess.run([key], {sess.get_inputs()[0].name: x})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    return (e / e.sum(0))[1][12:-12].astype(np.float32)


@torch.no_grad()
def model_prob(model, content):
    dev = next(model.parameters()).device
    x = torch.from_numpy(letterbox(content)[None]).to(dev)
    lo = model(x)[1][:, :, 12:-12]
    return torch.softmax(lo, dim=1)[0, 1].cpu().numpy()


def roi_stat(p, box):
    if box is None:
        return None
    x0, x1, y0, y1 = box
    q = p[y0:y1, x0:x1]
    return round(float(q.max()), 4), int((q > .30).sum())


def set_modes(model, training):
    for name, mod in model.named_children():
        if name in TRAIN_HEADS:
            mod.train(training)
        else:
            mod.eval()


def main():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rng = np.random.RandomState(SEED)
    log = open(OUT / "center_log.txt", "w", encoding="utf-8")

    def say(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    index = json.loads((SAM_LABELS / "index.json").read_text(encoding="utf-8"))
    items = []
    for r in index:
        im = cv2.imread(r["raw"])
        lab = cv2.imread(str(SAM_LABELS / r["mask"]), 0)
        if im is None or lab is None:
            continue
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        items.append((content, (lab > 127).astype(np.float32)))
    say(f"正例帧 {len(items)}（SAM 线体标签）")

    # 负例帧：常规帧，中心 ROI 内无目标线
    import make_target_labels as mtl
    neg = 0
    for fp in sorted(BDD.glob("*.jpg"))[::7]:
        if neg >= N_NEG:
            break
        im = cv2.imread(str(fp))
        if im is None:
            continue
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        m, _ = mtl.extract(content)
        if m is not None:
            continue                      # 含目标构型，不能当纯负例
        x0, x1, y0, y1 = ROI
        if (content[y0:y1, x0:x1].size == 0):
            continue
        items.append((content, np.zeros((360, 640), np.float32)))
        neg += 1
    say(f"负例帧 {neg}，训练集共 {len(items)}")

    native = ort.InferenceSession(str(NATIVE), providers=["CPUExecutionProvider"])
    nkey = next(n for n in [o.name for o in native.get_outputs()] if "ll" in n)
    evals = []
    for tag, fp, box in EVAL:
        if not fp.exists():
            continue
        im = cv2.imread(str(fp))
        c = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        pn = native_prob(native, nkey, c)
        evals.append((tag, c, box, pn))
        sky_px = int((pn[REG["sky"][2]:REG["sky"][3]] > .3).sum())
        say(f"现役 {tag}: 目标={roi_stat(pn, box)} 右侧={roi_stat(pn, REG['right'])} "
            f"黄线={roi_stat(pn, REG['yellow'])} 天空={sky_px} "
            f"全图={int((pn > .3).sum())}")

    model = TwinLiteNetPlus(types.SimpleNamespace(config="small")).to(device)
    model.load_state_dict(torch.load(INIT, map_location="cpu", weights_only=True))
    for name, mod in model.named_children():
        for p in mod.parameters():
            p.requires_grad_(name in TRAIN_HEADS)
    set_modes(model, True)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=LR, weight_decay=1e-4)
    rm = torch.from_numpy(roi_mask()).to(device)

    def evaluate():
        set_modes(model, False)
        out = []
        for tag, c, box, pn in evals:
            p = model_prob(model, c)
            out.append((tag, dict(target=roi_stat(p, box),
                                  right=roi_stat(p, REG["right"]),
                                  yellow=roi_stat(p, REG["yellow"]),
                                  sky=int((p[REG["sky"][2]:REG["sky"][3]] > .3).sum()),
                                  glob=int((p > .3).sum()))))
        set_modes(model, True)
        return out

    base_glob = int((evals[0][3] > .3).sum())
    best, best_step = -1e9, -1
    t0 = time.perf_counter()
    for step in range(1, STEPS + 1):
        pick = rng.choice(len(items), size=BATCH, replace=True)
        xs = np.stack([letterbox(items[i][0]) for i in pick])
        ts = np.stack([items[i][1] for i in pick])
        x = torch.from_numpy(xs).to(device)
        t = torch.from_numpy(ts).to(device)

        set_modes(model, True)
        lo = model(x)[1][:, :, 12:-12]
        p = torch.softmax(lo, dim=1)[:, 1]
        # 只在中心 ROI 内算 BCE；ROI 外不参与（这是「专项」的核心）
        bce = -(t * torch.log(p + 1e-6) + (1 - t) * torch.log(1 - p + 1e-6))
        loss = (bce * rm).sum() / (rm.sum() * x.shape[0])
        opt.zero_grad()
        loss.backward()
        opt.step()

        if step % 10 == 0 or step == 1:
            ms = evaluate()
            f0 = ms[0][1]
            tpx = f0["target"][1] if f0["target"] else 0
            reg_ok = all(m["right"][0] >= 0.90 and m["yellow"][0] >= 0.90
                         for _, m in ms)
            sky_pen = sum(m["sky"] for _, m in ms)
            gpen = max(0.0, f0["glob"] / max(1, base_glob) - 1.5)
            score = min(tpx, 300) / 300.0 - sky_pen / 100.0 - 0.5 * gpen
            desc = " ".join(f"{tag}:T={m['target']},sky={m['sky']},g={m['glob']}"
                            for tag, m in ms)
            say(f"step {step}/{STEPS} loss={float(loss.detach()):.4f} | {desc} "
                f"reg={reg_ok} ({time.perf_counter() - t0:.0f}s)")
            if tpx >= 20 and sky_pen <= 30 and reg_ok and score > best:
                best, best_step = score, step
                torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                            "epoch": step, "val_IoU": float(f0["target"][0]),
                            "metrics": ms}, OUT / "center_best.pth")
                say(f"  best @ step {step} score={score:.4f}")

    say(f"训练结束 best={best_step} score={best:.4f} "
        f"({time.perf_counter() - t0:.0f}s)")
    final = evaluate()
    torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                "epoch": STEPS, "val_IoU": float(final[0][1]["target"][0]),
                "metrics": final}, OUT / "center_last.pth")
    log.close()


if __name__ == "__main__":
    main()
