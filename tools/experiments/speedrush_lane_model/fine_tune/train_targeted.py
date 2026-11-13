# -*- coding: utf-8 -*-
"""只针对「贴脸 / 车头区粗白虚线」构型微调车道头，验收只看这条线出不出得来。

与旧微调（`train.py` + `make_labels.py`）的三点根本不同：

1. 标签来自像素亮度真值（`make_target_labels.py`），不来自任何模型输出；
2. 只标这一目标构型；其余区域用**现役模型自身的原生概率**做蒸馏保持，
   避免把原本能检出的线训没（旧微调的灾难性遗忘就栽在这）；
3. 不追 IoU / F1。验收只看留出帧 f0204 上目标线 ROI 的响应与位置，
   并以同帧右侧斜白线、黄线走廊作回归锁。

留出帧 f0204 由 `make_target_labels.py` 的 HOLD_OUT 排除，绝不进训练。

自包含（模型定义取实验区 upstream/），不 import 业务码。
用法：.venv/Scripts/python.exe fine_tune/train_targeted.py [steps]
产物：fine_tune_out/targeted_best.pth、targeted_log.txt
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
DATA = ROOT / "fine_tune_out" / "target_labels"
OUT = ROOT / "fine_tune_out"
INIT = ROOT / "weights" / "plus_small_ft_init.pth"
NATIVE = (ROOT.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
          / "plus_small_384x640.onnx")
HOLD = (Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
        / "20261109_104823_manual_p2/raw/0204_raw.jpg")

TARGET = (258, 282, 265, 360)          # 内容域 x0,x1,y0,y1
REG = {"right": (500, 635, 270, 325), "yellow": (15, 240, 215, 359)}
TRAIN_HEADS = ("up_1_ll", "up_2_ll", "out_ll")
STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 80
BATCH = 4
LAM_KEEP = 1.0
LR = 2e-4
SEED = 0

_mpl = types.ModuleType("matplotlib")
_mpl.pyplot = types.ModuleType("matplotlib.pyplot")
sys.modules.setdefault("matplotlib", _mpl)
sys.modules.setdefault("matplotlib.pyplot", _mpl.pyplot)
sys.path.insert(0, str(UP))

import onnxruntime as ort  # noqa: E402
import torch  # noqa: E402
from model.model import TwinLiteNetPlus  # noqa: E402


def letterbox(content: np.ndarray) -> np.ndarray:
    im = cv2.copyMakeBorder(content, 12, 12, 0, 0,
                            cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray((im.astype(np.float32) / 255.0).transpose(2, 0, 1))


def native_key(sess):
    return next(n for n in [o.name for o in sess.get_outputs()] if "ll" in n)


def native_prob(sess, key, content) -> np.ndarray:
    x = letterbox(content)[None]
    lo = sess.run([key], {sess.get_inputs()[0].name: x})[0][0]
    e = np.exp(lo - lo.max(0, keepdims=True))
    return (e / e.sum(0))[1][12:-12].astype(np.float32)


def model_prob(model, content) -> np.ndarray:
    with torch.no_grad():
        x = torch.from_numpy(letterbox(content)[None])
        logits = model(x)[1][:, :, 12:-12]
        return torch.softmax(logits, dim=1)[0, 1].numpy()


def roi(p, box):
    x0, x1, y0, y1 = box
    s = p[y0:y1, x0:x1]
    return float(s.max()), int((s > 0.30).sum())


def set_modes(model, training: bool):
    for name, mod in model.named_children():
        if name in TRAIN_HEADS:
            mod.train(training)
        else:
            mod.eval()


def quality(content, gt):
    """剔除抠错 / 特效 / 转场帧：标签处亮度对比、蓝色特效占比、线的横向范围。"""
    V = cv2.cvtColor(content, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.float32)
    bg = cv2.boxFilter(V, -1, (61, 1), normalize=True)
    contrast = float(np.median((V - bg)[gt > 0])) if gt.any() else 0.0
    hsv = cv2.cvtColor(content, cv2.COLOR_BGR2HSV)
    H, S = hsv[:, :, 0], hsv[:, :, 1]
    # 只看路面下半：白天天空本身是蓝的，全图统计会把整场白天帧误杀
    blue = float(((H[200:] > 90) & (H[200:] < 130) & (S[200:] > 140)).mean())
    cols = np.flatnonzero(gt.any(0))
    x0, x1 = (int(cols[0]), int(cols[-1])) if len(cols) else (0, 0)
    ok = contrast > 14 and blue < 0.06 and 190 <= x0 and x1 <= 400
    return ok, round(contrast, 1), round(blue, 3), (x0, x1)


def main():
    random = np.random.RandomState(SEED)
    log = open(OUT / "targeted_log.txt", "w", encoding="utf-8")

    def say(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    native = ort.InferenceSession(str(NATIVE), providers=["CPUExecutionProvider"])
    nkey = native_key(native)
    index = json.loads((DATA / "index.json").read_text(encoding="utf-8"))

    items, skipped = [], []
    for r in index:
        im = cv2.imread(r["raw"])
        if im is None:
            continue
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        gt = cv2.imread(str(DATA / r["mask"]), 0) > 0
        ok, contrast, blue, xr = quality(content, gt)
        if not ok:
            skipped.append((r["name"], contrast, blue, xr))
            continue
        pos = cv2.dilate(gt.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        keep = ~(cv2.dilate(gt.astype(np.uint8), np.ones((17, 17), np.uint8)) > 0)
        items.append((content, pos.astype(np.float32), keep.astype(np.float32),
                      native_prob(native, nkey, content)))
    say(f"数据：{len(items)} 帧正例入选，{len(skipped)} 帧被质量闸剔除")
    for s in skipped:
        say(f"  剔除 {s[0]} contrast={s[1]} blue={s[2]} x={s[3]}")
    if not items:
        raise SystemExit("无可用训练帧")

    # 纯保持帧：没有目标构型的常规帧，正例区为空，只把输出拉回现役模型。
    # 只训正例帧时，模型会泛化成「看见亮纹理就吐线」——原始分辨率目检在
    # 未训练帧上看到天空与空沥青的大片野线（2026-11-12 首轮否决点）。
    import make_target_labels as mtl
    n_keep = 0
    for sess in mtl.SESSIONS:
        d = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush" / sess / "raw"
        if not d.exists():
            continue
        got = 0
        for fp in sorted(d.glob("*.jpg"))[::25]:
            if got >= 6:
                break
            im = cv2.imread(str(fp))
            if im is None:
                continue
            content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
            m, _ = mtl.extract(content)
            if m is not None:          # 含目标构型，不能当纯背景推零
                continue
            items.append((content, np.zeros((360, 640), np.float32),
                          np.ones((360, 640), np.float32),
                          native_prob(native, nkey, content)))
            got += 1
            n_keep += 1
        say(f"  保持帧 {sess}: {got}")
    say(f"纯保持帧合计 {n_keep}，训练帧总数 {len(items)}")

    model = TwinLiteNetPlus(types.SimpleNamespace(config="small"))
    model.load_state_dict(torch.load(INIT, map_location="cpu", weights_only=True))
    for name, mod in model.named_children():
        for p in mod.parameters():
            p.requires_grad_(name in TRAIN_HEADS)
    set_modes(model, True)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=LR, weight_decay=1e-4)

    hold = cv2.imread(str(HOLD))
    hold = cv2.resize(hold, (640, 360), interpolation=cv2.INTER_AREA)
    p_native = native_prob(native, nkey, hold)
    base = {"target": roi(p_native, TARGET),
            "right": roi(p_native, REG["right"]),
            "yellow": roi(p_native, REG["yellow"]),
            "global": int((p_native > 0.30).sum())}
    say(f"现役基线：目标线 max/像素={base['target']}；右侧线={base['right']}；"
        f"黄线={base['yellow']}；全图>0.3={base['global']}")

    def evaluate():
        set_modes(model, False)
        p = model_prob(model, hold)
        set_modes(model, True)
        return {"target": roi(p, TARGET), "right": roi(p, REG["right"]),
                "yellow": roi(p, REG["yellow"]), "global": int((p > 0.30).sum())}, p

    def keep_ok(m):
        # 回归锁看「线还在不在」（max），不锁像素数：细线标签会把响应训窄，
        # 像素数下降不等于线消失；位置正确性由导出后的原始分辨率目检裁决。
        return (m["right"][0] >= 0.90 and m["yellow"][0] >= 0.90
                and m["global"] <= base["global"] * 3.0)

    best_score, best_step = -1.0, -1
    t0 = time.perf_counter()
    for step in range(1, STEPS + 1):
        pick = random.choice(len(items), size=BATCH, replace=True)
        batch = [items[int(i)] for i in pick]
        xs = np.stack([letterbox(b[0]) for b in batch])
        pos = np.stack([b[1] for b in batch])
        keep = np.stack([b[2] for b in batch])
        nps = np.stack([b[3] for b in batch])
        x = torch.from_numpy(xs)
        g_pos = torch.from_numpy(pos)
        g_keep = torch.from_numpy(keep)
        g_np = torch.from_numpy(nps)

        set_modes(model, True)
        logits = model(x)[1][:, :, 12:-12]
        p = torch.softmax(logits, dim=1)[:, 1]
        # 两项都按各自像素数归一、都用 MSE：量级相当，保持项才压得住正例项。
        # 旧版用 BCE 时正例项约 10、保持项约 0.01，十步就把其他线全训没了。
        l_pos = (((p - 1.0) ** 2) * g_pos).sum() / g_pos.sum().clamp(min=1)
        # 原本有线的地方加权保持：背景像素占了绝大多数，直接平均会把
        # 「保住既有车道线」的梯度稀释掉，表现为全图假阳性暴涨。
        w_keep = 1.0 + 9.0 * g_np
        l_keep = ((((p - g_np) ** 2) * g_keep * w_keep).sum()
                  / (g_keep * w_keep).sum().clamp(min=1))
        bg = (g_np < 0.02).float() * g_keep
        l_bg = ((p ** 2) * bg).sum() / bg.sum().clamp(min=1)
        loss = l_pos + LAM_KEEP * l_keep + 0.3 * l_bg
        opt.zero_grad()
        loss.backward()
        opt.step()

        if step % 10 == 0 or step == 1:
            m, _ = evaluate()
            # 目标出线 + 回归线仍在 + 假阳性不过分，三者合起来选步，
            # 避免一味挑「目标响应最大」的过拟合末态。
            score = (min(m["target"][1], 400) / 400.0
                     + m["right"][1] / max(1, base["right"][1])
                     - 0.3 * max(0.0, m["global"] / max(1, base["global"]) - 1.5))
            say(f"step {step}/{STEPS} loss={float(loss.detach()):.4f} "
                f"pos={float(l_pos.detach()):.4f} keep={float(l_keep.detach()):.5f} | "
                f"目标 max/px={m['target']} 右侧={m['right']} "
                f"黄线={m['yellow']} 全图={m['global']} "
                f"({time.perf_counter() - t0:.0f}s)")
            if m["target"][1] >= 20 and score > best_score and keep_ok(m):
                best_score, best_step = score, step
                torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                            "epoch": step, "val_IoU": float(m["target"][0]),
                            "metrics": m}, OUT / "targeted_best.pth")
                say(f"  best 更新 @ step {step}")
    say(f"训练结束：best step={best_step} score={best_score:.4f}，"
        f"总耗时 {time.perf_counter() - t0:.0f}s")
    log.close()


if __name__ == "__main__":
    main()
