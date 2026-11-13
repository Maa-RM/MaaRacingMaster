# -*- coding: utf-8 -*-
"""用大号教师（SegFormer-B2）的全图软标签蒸馏微调现役 TwinLiteNet+ Small。

与上一轮 `train_targeted.py` 的区别（也是维护者 2026-11-12 的新路线）：

- 旧标签只给目标线一个正例，其余区域用**现役模型自己的输出**做「蒸馏保持」，
  等于没有明确的负例 -> 模型学成「看见亮纹理就吐线」-> 原始分辨率目检野线否决。
- 新标签由独立大号教师给出**整幅 640x360 概率软标签**：所有真实车道线为正例，
  天空 / 沥青 / 护栏 / HUD 被明确判为负。教师只在离线跑一次，学生仍是 0.5MB
  小模型，部署为**单模型单次推理、零额外算力**。

教师标尺独立于学生：SegFormer-B2 与 TwinLiteNet+ 无共同权重与训练流程。

用法：在独立训练环境下 python fine_tune/train_teacher_distill.py [steps] [标签口径]
产物：fine_tune_out/distill_best.pth、distill_log.txt
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
DATA = ROOT / "fine_tune_out" / "teacher_labels"
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
REG = {"right": (500, 635, 270, 325), "yellow": (15, 240, 215, 359)}
SKY = (0, 640, 0, 165)
TRAIN_HEADS = ("up_1_ll", "up_2_ll", "out_ll")
STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 120
BATCH = 8
LR = 2e-4
SEED = 0
POS_W = 3.0            # 正例稀疏，加权但不过度，避免把线训粗
HARD_T = 0.30          # 教师软概率在校准到游戏域前不可直接当回归目标
LINE_HW = 2            # 逐行中心重画半宽：线宽 5px，与真实标线同量级
# 标签口径：hard（阈值+逐行中心细化）| calib（保留教师几何，只做概率标度校准）
LABEL_MODE = (sys.argv[2] if len(sys.argv) > 2 else "calib")
CALIB_T = 0.55         # 教师概率除以该常数后截断到 1；跨域未校准的补偿
MERGE_GAP = 25         # fill 口径：同行相邻段间距 <= 此值视为同一条线的两条边
MAX_W = 60             # fill 口径：合并后超过此宽度视为糊带/眩光，丢弃

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


def roi(p, box):
    if box is None:
        return None
    x0, x1, y0, y1 = box
    s = p[y0:y1, x0:x1]
    return round(float(s.max()), 4), int((s > 0.30).sum())


def set_modes(model, training):
    for name, mod in model.named_children():
        if name in TRAIN_HEADS:
            mod.train(training)
        else:
            mod.eval()


def harden(soft: np.ndarray) -> np.ndarray:
    """教师软概率 -> 硬标签 + 逐行中心重画细线。

    教师是跨域输出，概率未校准（全图 max 0.84、仅 3.4% 像素 >0.30），
    直接做软目标交叉熵会把学生教成「处处 0.4」，过不了生产 0.30 阈值
    （2026-11-12 首轮实测：目标线 0.47 / 过阈 16 像素，回归锁破）。
    按标准伪标签做法取阈值后重画：只保留每行连通段中心，宽度固定 5px，
    既符合真实标线宽度，也避免教师输出的 28px 粗带把线训糊。
    关键不变：阈值外整幅都是明确负例，这正是旧标签缺的东西。
    """
    if LABEL_MODE == "calib":
        # 只校准概率标度，完全保留教师输出的几何形态（粗细 / 断续 / 位置）。
        # 教师跨域概率最高约 0.84，除常数后正例趋近 1、背景仍为 0，
        # 不引入任何阈值化或逐行重画造成的多条平行假线。
        return np.clip(soft / CALIB_T, 0.0, 1.0)
    if LABEL_MODE == "binary":
        # 阈值化成硬标签，但**不逐行中心重画**：完整保留教师输出的粗细与
        # 断续形态。判别用：区分「阈值化」与「hard 的逐行细化」哪个引入假线。
        return (soft > HARD_T).astype(np.float32)
    if LABEL_MODE == "fill":
        # 教师实测在「描边」：同一条线的左右边缘各出一条细带，中间是线体
        # （放大 6 倍目检 + 全量剖面：42.7% 的相邻段间距 2~25px、中位 8px）。
        # 直接学边缘带会让小模型一行学出两条平行线（hard 口径的平行假线
        # 就是这么做出来的）。此口径把同行间距 <= MERGE_GAP 的相邻段合并、
        # 整段填实成线体，宽度仍受 MAX_W 约束防止把糊带当线。
        hit = soft > HARD_T
        out = np.zeros_like(soft, np.float32)
        for y in range(soft.shape[0]):
            ix = np.flatnonzero(hit[y])
            if len(ix) == 0:
                continue
            segs = [s for s in np.split(ix, np.flatnonzero(np.diff(ix) > 1) + 1)
                    if len(s) >= 2]
            if not segs:
                continue
            merged = [[int(segs[0][0]), int(segs[0][-1])]]
            for s in segs[1:]:
                if int(s[0]) - merged[-1][1] <= MERGE_GAP:
                    merged[-1][1] = int(s[-1])
                else:
                    merged.append([int(s[0]), int(s[-1])])
            for a, b in merged:
                if b - a + 1 <= MAX_W:
                    out[y, a:b + 1] = 1.0
        return out
    hit = soft > HARD_T
    out = np.zeros_like(soft, np.float32)
    for y in range(soft.shape[0]):
        idx = np.flatnonzero(hit[y])
        if len(idx) == 0:
            continue
        for seg in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
            if len(seg) == 0 or len(seg) > 70:   # 超宽连片 = 眩光/护栏，丢弃
                continue
            c = int(round(float(seg.mean())))
            out[y, max(0, c - LINE_HW):min(soft.shape[1], c + LINE_HW + 1)] = 1.0
    return out


def main():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rng = np.random.RandomState(SEED)
    log = open(OUT / "distill_log.txt", "w", encoding="utf-8")

    def say(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    index = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    items = []
    for r in index:
        ip = (DATA / r["img"]).resolve()
        lp = DATA / r["split"] / f"{r['name']}.png"
        im, lab = cv2.imread(str(ip)), cv2.imread(str(lp), 0)
        if im is None or lab is None:
            continue
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        items.append((content, harden(lab.astype(np.float32) / 255.0)))
    pos_px = np.mean([b.sum() for _, b in items])
    say(f"数据：{len(items)} 帧教师标签（口径={LABEL_MODE}，平均标签质量 "
        f"{pos_px:.0f}；train/val 合并，留出帧单独评估）")

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
        say(f"现役基线 {tag}: 目标={roi(pn, box)} 右侧={roi(pn, REG['right'])} "
            f"黄线={roi(pn, REG['yellow'])} 天空>0.3={int((pn[SKY[2]:SKY[3]] > .3).sum())} "
            f"全图={int((pn > .3).sum())}")

    model = TwinLiteNetPlus(types.SimpleNamespace(config="small")).to(device)
    model.load_state_dict(torch.load(INIT, map_location="cpu", weights_only=True))
    for name, mod in model.named_children():
        for p in mod.parameters():
            p.requires_grad_(name in TRAIN_HEADS)
    set_modes(model, True)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=LR, weight_decay=1e-4)

    def evaluate():
        set_modes(model, False)
        out = []
        for tag, c, box, pn in evals:
            p = model_prob(model, c)
            out.append((tag, dict(target=roi(p, box), right=roi(p, REG["right"]),
                                  yellow=roi(p, REG["yellow"]),
                                  sky=int((p[SKY[2]:SKY[3]] > .3).sum()),
                                  glob=int((p > .3).sum()))))
        set_modes(model, True)
        return out

    base_glob = int((evals[0][3] > .3).sum())
    best_score, best_step = -1e9, -1
    t0 = time.perf_counter()
    for step in range(1, STEPS + 1):
        pick = rng.choice(len(items), size=BATCH, replace=True)
        xs = np.stack([letterbox(items[i][0]) for i in pick])
        ts = np.stack([items[i][1] for i in pick])
        x = torch.from_numpy(xs).to(device)
        t = torch.from_numpy(ts).to(device)

        set_modes(model, True)
        lo = model(x)[1][:, :, 12:-12]
        logp = torch.log_softmax(lo, dim=1)
        lp = logp[:, 1]
        ln = logp[:, 0]
        # 软标签交叉熵：教师概率同时提供正例与明确负例，全图参与
        ce = -(t * lp + (1 - t) * ln)
        w = 1.0 + POS_W * t
        loss = (ce * w).sum() / w.sum().clamp(min=1)
        opt.zero_grad()
        loss.backward()
        opt.step()

        if step % 10 == 0 or step == 1:
            ms = evaluate()
            f0 = ms[0][1]
            target_px = f0["target"][1] if f0["target"] else 0
            # 选步判据：目标出线 + 无天空野线 + 全图不过量 + 回归线仍在
            reg_ok = all(m["right"][0] >= 0.90 and m["yellow"][0] >= 0.90
                         for _, m in ms)
            sky_pen = sum(m["sky"] for _, m in ms)
            glob_pen = max(0.0, f0["glob"] / max(1, base_glob) - 2.0)
            score = min(target_px, 400) / 400.0 - sky_pen / 200.0 - 0.5 * glob_pen
            desc = " ".join(
                f"{tag}:T={m['target']},R={m['right']},Y={m['yellow']},"
                f"sky={m['sky']},g={m['glob']}" for tag, m in ms)
            say(f"step {step}/{STEPS} loss={float(loss.detach()):.4f} | {desc} "
                f"reg_ok={reg_ok} ({time.perf_counter() - t0:.0f}s)")
            if target_px >= 20 and sky_pen <= 50 and reg_ok and score > best_score:
                best_score, best_step = score, step
                torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                            "epoch": step, "val_IoU": float(f0["target"][0]),
                            "metrics": ms}, OUT / "distill_best.pth")
                say(f"  best 更新 @ step {step} score={score:.4f}")
    say(f"训练结束：best step={best_step} score={best_score:.4f}，"
        f"总耗时 {time.perf_counter() - t0:.0f}s")
    # 无论 best 是否达标都存末态供目检：判据是机检代理，位置正确性由
    # 原始分辨率目检裁决（上一轮野线就是机检漏过的）。
    final_metrics = evaluate()
    torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                "epoch": STEPS, "val_IoU": float(final_metrics[0][1]["target"][0]),
                "metrics": final_metrics}, OUT / "distill_last.pth")
    log.close()


if __name__ == "__main__":
    main()
