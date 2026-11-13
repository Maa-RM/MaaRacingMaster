# -*- coding: utf-8 -*-
"""中心专项 + 外围保持：SAM 线体标签教中心，现役模型输出守外围。

为什么这样设计（2026-11-12 两次失败后的修正）：
- `train_center_specialist.py` 只在中心 ROI 算损失，结果负例帧把模型压成
  「处处不输出」（全图归零），随后为了在中心出线又把天空点着（sky 672）。
  参数是共享的，只算 ROI 损失挡不住外围被带坏。
- `train_teacher_distill.py` 整图用 SegFormer 标签迁移，线被教粗成糊带——
  因为 SegFormer 是**描边**。
- 本版：标签换成 SAM 的**线体**掩码（宽度与真实标线一致），中心 ROI 用
  SAM 正负例监督，**ROI 外一律用现役模型原生概率做蒸馏保持**。既不放弃
  外围约束，也不用描边标签。

用法：在 GPU 训练环境下 python fine_tune/train_sam_center.py [steps]
产物：fine_tune_out/sam_center_best.pth、sam_center_log.txt
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
# 上一版权重：用来挑「它确实误检」的负例帧（把错误喂回去）
NEW_W = ROOT / "weights" / "plus_small_samcenter_384x640.onnx"

RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug"
EVAL = [
    ("f0204", RAW / "speedrush/20261109_104823_manual_p2/raw/0204_raw.jpg",
     (258, 282, 265, 360)),
    ("f0202", RAW / "20261110_204716/speedrush/drive/20261110_205224_p1/raw/0202_raw.jpg",
     (310, 339, 253, 360)),
    ("f0500", RAW / "speedrush/20261109_104823_manual_p2/raw/0500_raw.jpg", None),
]
ROI = (170, 440, 240, 360)
REG = {"right": (500, 635, 270, 325), "yellow": (15, 240, 215, 359),
       "sky": (0, 640, 0, 165)}
TRAIN_HEADS = ("up_1_ll", "up_2_ll", "out_ll")
STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 150
BATCH = 8
LR = 2e-4
SEED = 0
LAM_KEEP = 1.0
N_NEG = int(sys.argv[2]) if len(sys.argv) > 2 else 60
# 标签细化：SAM 线体掩码逐行取中心重画 THIN_W 宽。
# 生产几何门的残差门槛 3.0 是按细线设计的；线体标签训出的线太粗，
# 质心抖动使 rms 达 3.8~5.1，会被全部滤掉（2026-11-12 实测）。
# 与之前 SegFormer 的差别：SAM 是单条线体（一行一个段），逐行取中心
# 不会像描边那样一行拆出两条平行线。
THIN_W = int(sys.argv[3]) if len(sys.argv) > 3 else 5


def thin(mask_bool):
    out = np.zeros_like(mask_bool, dtype=np.float32)
    for y in range(mask_bool.shape[0]):
        idx = np.flatnonzero(mask_bool[y])
        if len(idx) == 0:
            continue
        for seg in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
            c = int(round(float(seg.mean())))
            out[y, max(0, c - THIN_W // 2):min(640, c + THIN_W // 2 + 1)] = 1.0
    return out

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


def region_weight():
    """外围保持的区域权重（2026-11-12 实测依据）。

    - 天空 y<165：权重 0。实测把天空+远景涂灰，目标线与其它线的响应几乎不变
      （0.89→0.886），天空不参与判断；且生产有路面平面拟合，路面外误检会被
      滤掉，不必在此浪费约束。
    - 中远场 165~240：权重 3.0。涂掉这一段模型**全部归零**，其它车道线的透视
      结构是上下文命脉，必须重点保护。
    - 近场其它：权重 1.0。
    """
    w = np.ones((360, 640), np.float32)
    w[0:165] = 0.0
    w[165:240] = 3.0
    return w


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
    log = open(OUT / "sam_center_log.txt", "w", encoding="utf-8")

    def say(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    native = ort.InferenceSession(str(NATIVE), providers=["CPUExecutionProvider"])
    nkey = next(n for n in [o.name for o in native.get_outputs()] if "ll" in n)
    rm = roi_mask()

    index = json.loads((SAM_LABELS / "index.json").read_text(encoding="utf-8"))
    items = []
    for r in index:
        im = cv2.imread(r["raw"])
        lab = cv2.imread(str(SAM_LABELS / r["mask"]), 0)
        if im is None or lab is None:
            continue
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        tgt = thin(lab > 127)
        items.append((content, tgt, native_prob(native, nkey, content)))
    say(f"正例帧 {len(items)}（SAM 线体）")

    # 负例帧：优先选「当前权重在中心 ROI 确实误检」的帧——把错误喂回去惩罚。
    # 没有误检的普通帧对训练没用（本来就输出 0），只在不够时兜底。
    import make_target_labels as mtl
    cur = ort.InferenceSession(str(NEW_W), providers=["CPUExecutionProvider"])
    ckey = next(n for n in [o.name for o in cur.get_outputs()] if "ll" in n)
    rx0, rx1, ry0, ry1 = ROI
    neg, neg_miss = 0, 0
    for fp in sorted(BDD.glob("*.jpg"))[::5]:
        if neg >= N_NEG:
            break
        im = cv2.imread(str(fp))
        if im is None:
            continue
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        m, _ = mtl.extract(content)
        if m is not None:
            continue                      # 含目标构型，不能当负例
        pc = native_prob(cur, ckey, content)
        hit = int((pc[ry0:ry1, rx0:rx1] > 0.30).sum())
        if hit < 50 and neg_miss > N_NEG // 3:
            continue                      # 没误检，兜底只取少量
        items.append((content, np.zeros((360, 640), np.float32),
                      native_prob(native, nkey, content)))
        if hit >= 50:
            neg += 1
        else:
            neg_miss += 1
    say(f"负例帧 {neg}（中心误检）+ {neg_miss}（普通兜底），共 {len(items)}")

    evals = []
    for tag, fp, box in EVAL:
        if not fp.exists():
            continue
        im = cv2.imread(str(fp))
        c = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        pn = native_prob(native, nkey, c)
        evals.append((tag, c, box, pn))
        say(f"现役 {tag}: 目标={roi_stat(pn, box)} 右侧={roi_stat(pn, REG['right'])} "
            f"黄线={roi_stat(pn, REG['yellow'])} "
            f"天空={int((pn[0:165] > .3).sum())} 全图={int((pn > .3).sum())}")

    model = TwinLiteNetPlus(types.SimpleNamespace(config="small")).to(device)
    model.load_state_dict(torch.load(INIT, map_location="cpu", weights_only=True))
    for name, mod in model.named_children():
        for p in mod.parameters():
            p.requires_grad_(name in TRAIN_HEADS)
    set_modes(model, True)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=LR, weight_decay=1e-4)
    rmt = torch.from_numpy(rm).to(device)
    rwt = torch.from_numpy(region_weight()).to(device)

    def evaluate():
        set_modes(model, False)
        out = []
        for tag, c, box, pn in evals:
            p = model_prob(model, c)
            out.append((tag, dict(target=roi_stat(p, box),
                                  right=roi_stat(p, REG["right"]),
                                  yellow=roi_stat(p, REG["yellow"]),
                                  sky=int((p[0:165] > .3).sum()),
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
        nps = np.stack([items[i][2] for i in pick])
        x = torch.from_numpy(xs).to(device)
        t = torch.from_numpy(ts).to(device)
        g_np = torch.from_numpy(nps).to(device)

        set_modes(model, True)
        lo = model(x)[1][:, :, 12:-12]
        p = torch.softmax(lo, dim=1)[:, 1]
        # 中心：SAM 正负例 BCE（线体掩码，宽度真实）
        bce = -(t * torch.log(p + 1e-6) + (1 - t) * torch.log(1 - p + 1e-6))
        l_center = (bce * rmt).sum() / (rmt.sum() * x.shape[0])
        # 外围：保持现役原生概率，但按区域加权——天空不约束（生产有平面拟合）、
        # 中远场车道线强保护（它是上下文命脉，涂掉就全归零）。
        keep = (1.0 - rmt) * rwt
        l_keep = (((p - g_np) ** 2) * keep).sum() / (keep.sum() * x.shape[0])
        loss = l_center + LAM_KEEP * l_keep
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
            gpen = max(0.0, f0["glob"] / max(1, base_glob) - 2.0)
            score = min(tpx, 300) / 300.0 - sky_pen / 100.0 - 0.5 * gpen
            desc = " ".join(f"{tag}:T={m['target']},sky={m['sky']},g={m['glob']}"
                            for tag, m in ms)
            say(f"step {step}/{STEPS} L={float(loss.detach()):.4f} "
                f"c={float(l_center.detach()):.4f} k={float(l_keep.detach()):.4f} | "
                f"{desc} reg={reg_ok} ({time.perf_counter() - t0:.0f}s)")
            if tpx >= 20 and sky_pen <= 30 and reg_ok and score > best:
                best, best_step = score, step
                torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                            "epoch": step, "val_IoU": float(f0["target"][0]),
                            "metrics": ms}, OUT / "sam_center_best.pth")
                say(f"  best @ {step} score={score:.4f}")

    say(f"结束 best={best_step} score={best:.4f} ({time.perf_counter() - t0:.0f}s)")
    final = evaluate()
    torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                "epoch": STEPS, "val_IoU": float(final[0][1]["target"][0]),
                "metrics": final}, OUT / "sam_center_last.pth")
    log.close()


if __name__ == "__main__":
    main()
