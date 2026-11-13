# -*- coding: utf-8 -*-
"""按 TwinLiteNet+ 论文口径重训中心专项（Focal + Tversky）。

为什么换损失（2026-11-12 诊断）：
- 之前中心用逐像素 BCE。逐像素损失**对散点是合法的**：每行点亮几个点，
  BCE 就能降下去，于是模型学出「这片区域大概有线」的散点，而不是一条线。
  实测 f0204 目标区逐行概率 0.005 / 0.070 / 0.003 / 0.069 / 0.463，正是散点。
- 论文（arXiv:2403.16958 v6, 3.4 节）车道分支用的是
  **Focal(γ=2, α=0.25) + Tversky(α=0.9, β=0.1)**，Tversky 的 α>β 刻意偏重
  召回。Tversky 是**区域级重叠**（TP/FN/FP 的集合量），散点会推高 FN，
  因此它天然逼模型覆盖整条连续线——这正是生产行扫描需要的前提。
- 论文还把 PCAA（本仓库的 CAAM + conv_caam）作为提升分割精度的核心模块，
  而此前只训了 up_1_ll / up_2_ll / out_ll 三层，注意力模块是冻着的。
  本脚本可选解冻 PCAA（BN 保持 eval，即 frozen-BN 微调，避免改统计量）。

判据也换了：不再看「ROI 最大概率 + 过阈像素数」——那个指标会被散点骗。
改成生产口径的**逐行连续段覆盖率**：每一行是否有一段 >=3px 的连续响应，
以及这些段中心的抖动。覆盖率低 / 抖动大 = 生产提取不到。

用法（GPU 训练环境）：
  python fine_tune/train_paper_loss.py [steps] [n_neg] [label:body|thin] [unfreeze_pcaa:0|1]
产物：fine_tune_out/paper_loss_best.pth、paper_loss_last.pth、paper_loss_log.txt
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
LABELS_DIR = sys.argv[5] if len(sys.argv) > 5 else "sam_labels"
SAM_LABELS = ROOT / "fine_tune_out" / LABELS_DIR
BDD = ROOT / "bdd100k" / "images" / "train"
OUT = ROOT / "fine_tune_out"
INIT = ROOT / "weights" / "plus_small_ft_init.pth"
NATIVE = (ROOT.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
          / "plus_small_384x640.onnx")
# 上一版权重：用来挑「它确实误检」的负例帧（把错误喂回去）
NEW_W = ROOT / "weights" / "plus_small_verified74_384x640.onnx"

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

STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 250
N_NEG = int(sys.argv[2]) if len(sys.argv) > 2 else 80
LABEL_MODE = sys.argv[3] if len(sys.argv) > 3 else "body"
UNFREEZE_PCAA = bool(int(sys.argv[4])) if len(sys.argv) > 4 else True

BATCH = 8
LR = 2e-4
SEED = 0
THIN_W = 5
HUD_MARGIN = 8         # HUD 圆形距离标记外扩像素：整块强制为负例
ICON_PEN = 3.0        # 图标框内的预测惩罚权重（3.0 时实测无效：图标帧在
                       # 234 帧里只占 40，一个 batch 抽不到，梯度淹没在中心项里）
ICON_REPEAT = 1       # 图标帧过采样倍数

# 整段复核发现（2026-11-12）——注意：20x 惩罚 + 10x 过采样会让训练崩溃（目标线归零），
# 5x + 4x 会把真线一起压（逐行覆盖 0.75→0.59）。默认 3x/1x 是「不伤真线」的保守值，
# 图标假线仍未完全消除，见 README「整段真实对局复核」一节。：新版把画面中央的 HUD 圆形距离标记（「59米」）学成了
# 一条车道线，且连续 30+ 帧稳定存在——gt=0 帧的新增线里 66% 落在这个图标框内。
# 根因是训练标签：贴脸线常常紧贴图标，SAM 掩码/逐行中心把图标像素也算进了正例。
# 修法两条：① 标签里把图标框整块清零（硬负例）；② 负例优先取「有图标但没有目标线」
# 的帧，直接教「图标不是线」。
FOCAL_GAMMA = 2.0
FOCAL_ALPHA = 0.25
TVERSKY_ALPHA = 0.9     # FN 权重（论文车道分支）
TVERSKY_BETA = 0.1      # FP 权重
KEEP_RATIO = 0.5        # 外围保持项相对中心项的目标比例（自适应，不手调）

# 只训这些模块；PCAA 是否加入由 UNFREEZE_PCAA 决定
HEADS = ("up_1_ll", "up_2_ll", "out_ll")
PCAA = ("caam", "conv_caam")

_mpl = types.ModuleType("matplotlib")
_mpl.pyplot = types.ModuleType("matplotlib.pyplot")
sys.modules.setdefault("matplotlib", _mpl)
sys.modules.setdefault("matplotlib.pyplot", _mpl.pyplot)
sys.path.insert(0, str(UP))

import onnxruntime as ort  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402

from model.model import TwinLiteNetPlus  # noqa: E402
import make_target_labels as mtl  # noqa: E402


def icon_mask(content):
    """HUD 圆形标记区域的二值掩码：这块里出现车道线概率要付代价。

    只把标签清零不够——74 张正例里含图标的帧只占一部分，模型仍会在图标上
    画出稳定假线（实测 35 帧里 8 帧）。所以对**每一帧**都加一项显式惩罚：
    只要在图标框内输出高概率，损失就增加。
    """
    m = np.zeros((360, 640), np.float32)
    for x0, y0, x1, y1 in mtl.hud_boxes(content):
        m[max(0, y0 - HUD_MARGIN):y1 + HUD_MARGIN,
          max(0, x0 - HUD_MARGIN):x1 + HUD_MARGIN] = 1.0
    return m


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

    - 天空 y<165：权重 0。实测涂灰天空后目标线与其它线几乎不变，且生产有
      路面平面拟合，路面外误检会被滤掉。
    - 中远场 165~240：权重 3.0。涂掉这一段两个模型**全部归零**，其它车道线
      的透视结构是上下文命脉。
    - 近场其它：权重 1.0。
    """
    w = np.ones((360, 640), np.float32)
    w[0:165] = 0.0
    w[165:240] = 3.0
    return w


def is_holdout_frame(stem):
    """连帧验证段必须从负例源里排除，否则验证不再独立。

    bdd100k/images/train 是旧训练集，含 20261109_104823_manual_p2 的
    0180~0260 共 14 帧（实测），这些帧不能进任何一侧的训练数据。
    """
    p = stem.split("_")
    if len(p) >= 4 and p[0] == "104823" and p[2] == "p2" and p[3].isdigit():
        fid = int(p[3])
        return 180 <= fid <= 260 or fid == 204
    return False


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


def row_profile(p, box, half=40, thr=0.30, min_w=3):
    """生产口径的连续性判据。

    以目标框中心为轴、左右各 half 列，从 y0 到 359 逐行找最长连续段；
    段宽 >= min_w 才算「这一行有明确位置」。返回 (覆盖率, 段中心标准差)。
    覆盖率低 = 生产行扫描连不成轨迹；抖动大 = 拟合残差会超门。
    """
    if box is None:
        return None
    cx = (box[0] + box[1]) // 2
    y0 = box[2]
    x0, x1 = max(0, cx - half), min(640, cx + half)
    hit, centers = 0, []
    total = 0
    for y in range(y0, 360):
        total += 1
        idx = np.flatnonzero(p[y, x0:x1] > thr)
        if len(idx) == 0:
            continue
        segs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        g = max(segs, key=len)
        if len(g) >= min_w:
            hit += 1
            centers.append(float(g.mean()) + x0)
    cov = hit / max(1, total)
    jit = float(np.std(centers)) if len(centers) > 1 else 99.0
    return round(cov, 3), round(jit, 2)


def set_modes(model, training):
    """只把可训的解码器置 train；PCAA 用 frozen-BN（eval）以护外围统计量。"""
    for name, mod in model.named_children():
        mod.train(training and name in HEADS)


def focal_loss(p, t, w):
    pt = t * p + (1 - t) * (1 - p)
    at = t * FOCAL_ALPHA + (1 - t) * (1 - FOCAL_ALPHA)
    loss = -at * (1 - pt) ** FOCAL_GAMMA * torch.log(pt + 1e-6)
    return (loss * w).sum() / (w.sum() * p.shape[0])


def tversky_loss(p, t, w):
    tp = (p * t * w).sum()
    fp = (p * (1 - t) * w).sum()
    fn = ((1 - p) * t * w).sum()
    return 1.0 - (tp + 1.0) / (tp + 1.0 + TVERSKY_ALPHA * fn + TVERSKY_BETA * fp)


def main():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rng = np.random.RandomState(SEED)
    log = open(OUT / f"paper_loss_{LABELS_DIR}_{LABEL_MODE}.txt", "w", encoding="utf-8")

    def say(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    say(f"配置：steps={STEPS} n_neg={N_NEG} label={LABEL_MODE} "
        f"unfreeze_pcaa={UNFREEZE_PCAA} loss=Focal({FOCAL_GAMMA},{FOCAL_ALPHA})+"
        f"Tversky({TVERSKY_ALPHA},{TVERSKY_BETA})")

    native = ort.InferenceSession(str(NATIVE), providers=["CPUExecutionProvider"])
    nkey = next(n for n in [o.name for o in native.get_outputs()] if "ll" in n)
    rm = torch.from_numpy(roi_mask())
    rwt = torch.from_numpy(region_weight())

    index = json.loads((SAM_LABELS / "index.json").read_text(encoding="utf-8"))
    items = []
    for r in index:
        im = cv2.imread(r["raw"])
        lab = cv2.imread(str(SAM_LABELS / r["mask"]), 0)
        if im is None or lab is None:
            continue
        content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        tgt = thin(lab > 127) if LABEL_MODE == "thin" else (lab > 127).astype(np.float32)
        # 图标框整块清零：HUD 距离标记不是车道线，且它常和贴脸线相邻，
        # 不清掉就会被当成线的一部分学进去（整段复核实测 66% 新增线落在此框内）。
        for hx0, hy0, hx1, hy1 in mtl.hud_boxes(content):
            tgt[max(0, hy0 - HUD_MARGIN):hy1 + HUD_MARGIN,
                max(0, hx0 - HUD_MARGIN):hx1 + HUD_MARGIN] = 0.0
        items.append((content, tgt, native_prob(native, nkey, content),
                      icon_mask(content)))
    pos_px = float(np.mean([it[1].sum() for it in items])) if items else 0
    say(f"正例帧 {len(items)}（SAM {LABEL_MODE}，平均正例 {pos_px:.0f} 像素/帧）")

    # 负例帧：优先选「当前权重在中心 ROI 确实误检」的帧——把错误喂回去惩罚。
    cur = ort.InferenceSession(str(NEW_W), providers=["CPUExecutionProvider"])
    ckey = next(n for n in [o.name for o in cur.get_outputs()] if "ll" in n)
    rx0, rx1, ry0, ry1 = ROI
    neg, neg_miss = 0, 0
    for fp in sorted(BDD.glob("*.jpg"))[::5]:
        if neg >= N_NEG:
            break
        if is_holdout_frame(fp.stem):
            continue                      # 验证段绝不进训练
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
                      native_prob(native, nkey, content), icon_mask(content)))
        if hit >= 50:
            neg += 1
        else:
            neg_miss += 1
    # 专项负例：有 HUD 图标、但没有目标线的真实驾驶帧 —— 直接教「图标不是线」。
    icon_neg = 0
    icon_items = []
    for sess in ("20261109_104823_manual_p2", "20261109_095756_manual_p2",
                 "20261107_203433_manual_p2"):
        d = RAW / "speedrush" / sess / "raw"
        if not d.exists():
            continue
        for fp in sorted(d.glob("*.jpg"))[::11]:
            if icon_neg >= max(20, N_NEG // 3):
                break
            if is_holdout_frame(fp.stem):
                continue
            im = cv2.imread(str(fp))
            if im is None:
                continue
            content = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
            if not mtl.hud_boxes(content):
                continue                  # 没图标，教不了这件事
            if mtl.extract(content)[0] is not None:
                continue                  # 有目标线，不是负例
            it = (content, np.zeros((360, 640), np.float32),
                  native_prob(native, nkey, content), icon_mask(content))
            items.append(it)
            icon_items.append(it)
            icon_neg += 1
    # 图标帧过采样：只有让「图标≠线」在 batch 里占到足够比例，梯度才压得住
    if icon_items:
        items.extend(icon_items * ICON_REPEAT)
    say(f"负例帧 {neg}（中心误检）+ {neg_miss}（普通兜底）+ {icon_neg}（图标专项，"
        f"过采样 x{ICON_REPEAT}），共 {len(items)}")

    evals = []
    for tag, fp, box in EVAL:
        if not fp.exists():
            continue
        im = cv2.imread(str(fp))
        c = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        pn = native_prob(native, nkey, c)
        evals.append((tag, c, box, pn))
        say(f"现役 {tag}: 目标={roi_stat(pn, box)} 行覆盖={row_profile(pn, box)} "
            f"右侧={roi_stat(pn, REG['right'])} 黄线={roi_stat(pn, REG['yellow'])} "
            f"全图={int((pn > .3).sum())}")

    model = TwinLiteNetPlus(types.SimpleNamespace(config="small")).to(device)
    model.load_state_dict(torch.load(INIT, map_location="cpu", weights_only=True))
    trainable = set(HEADS) | (set(PCAA) if UNFREEZE_PCAA else set())
    for name, mod in model.named_children():
        for p in mod.parameters():
            p.requires_grad_(name in trainable)
    set_modes(model, True)
    n_tr = sum(p.numel() for p in model.parameters() if p.requires_grad)
    say(f"可训模块 {sorted(trainable)}，参数 {n_tr/1e3:.1f}K")
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                            lr=LR, weight_decay=1e-4)

    def evaluate():
        set_modes(model, False)
        out = []
        for tag, c, box, pn in evals:
            p = model_prob(model, c)
            out.append((tag, dict(target=roi_stat(p, box),
                                  row=row_profile(p, box),
                                  right=roi_stat(p, REG["right"]),
                                  yellow=roi_stat(p, REG["yellow"]),
                                  sky=int((p[0:165] > .3).sum()),
                                  glob=int((p > .3).sum()))))
        set_modes(model, True)
        return out

    base_glob = int((evals[0][3] > .3).sum()) if evals else 1
    best, best_step = -1e9, -1
    t0 = time.perf_counter()
    for step in range(1, STEPS + 1):
        pick = rng.choice(len(items), size=BATCH, replace=True)
        xs = np.stack([letterbox(items[i][0]) for i in pick])
        ts = np.stack([items[i][1] for i in pick])
        nps = np.stack([items[i][2] for i in pick])
        ims = np.stack([items[i][3] for i in pick])
        x = torch.from_numpy(xs).to(device)
        t = torch.from_numpy(ts).to(device)
        g_np = torch.from_numpy(nps).to(device)
        im_t = torch.from_numpy(ims).to(device)

        set_modes(model, True)
        lo = model(x)[1][:, :, 12:-12]
        p = torch.softmax(lo, dim=1)[:, 1]
        rmt = rm.to(device)[None]
        # 中心：论文口径 Focal + Tversky（ROI 内）
        l_center = focal_loss(p, t, rmt) + tversky_loss(p, t, rmt)
        # 外围：保持现役原生概率。权重自适应，使保持项 ≈ KEEP_RATIO × 中心项，
        # 避免重演「中心 10 / 保持 0.01」那种量级失衡导致的灾难性遗忘。
        keep = (1.0 - rmt) * rwt.to(device)[None]
        l_keep_raw = (((p - g_np) ** 2) * keep).sum() / (keep.sum() * x.shape[0])
        lam = float((l_center.detach() / (l_keep_raw.detach() + 1e-8)).clamp(1.0, 200.0)) * KEEP_RATIO
        # 图标框惩罚：这块不是车道线，出现高概率直接罚
        l_icon = ((p ** 2) * im_t).sum() / (im_t.sum().clamp(min=1) * p.shape[0])
        loss = l_center + lam * l_keep_raw + ICON_PEN * l_icon
        opt.zero_grad()
        loss.backward()
        opt.step()

        if step % 10 == 0 or step == 1:
            ms = evaluate()
            f0 = ms[0][1]
            cov = f0["row"][0] if f0["row"] else 0.0
            jit = f0["row"][1] if f0["row"] else 99.0
            reg_ok = all(m["right"][0] >= 0.90 and m["yellow"][0] >= 0.90
                         for _, m in ms)
            sky_pen = sum(m["sky"] for _, m in ms)
            gpen = max(0.0, f0["glob"] / max(1, base_glob) - 2.0)
            # 分数以「逐行覆盖率」为主：这才是生产能提取的前提
            score = cov - max(0.0, jit - 4.0) / 40.0 - sky_pen / 200.0 - 0.3 * gpen
            desc = " ".join(f"{tag}:T={m['target']},row={m['row']},g={m['glob']}"
                            for tag, m in ms)
            say(f"step {step}/{STEPS} L={float(loss.detach()):.4f} "
                f"c={float(l_center.detach()):.4f} k={float(l_keep_raw.detach()):.5f} "
                f"icon={float(l_icon.detach()):.5f} "
                f"lam={lam:.0f} | {desc} reg={reg_ok} ({time.perf_counter() - t0:.0f}s)")
            if cov >= 0.5 and jit <= 10.0 and sky_pen <= 60 and reg_ok and score > best:
                best, best_step = score, step
                torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                            "epoch": step, "val_IoU": float(f0["target"][0]),
                            "metrics": ms}, OUT / f"paper_loss_{LABELS_DIR}_{LABEL_MODE}_best.pth")
                say(f"  best @ {step} score={score:.4f} cov={cov} jit={jit}")

    say(f"结束 best={best_step} score={best:.4f} ({time.perf_counter() - t0:.0f}s)")
    final = evaluate()
    torch.save({"state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                "epoch": STEPS, "val_IoU": float(final[0][1]["target"][0]),
                "metrics": final}, OUT / f"paper_loss_{LABELS_DIR}_{LABEL_MODE}_last.pth")
    log.close()


if __name__ == "__main__":
    main()
