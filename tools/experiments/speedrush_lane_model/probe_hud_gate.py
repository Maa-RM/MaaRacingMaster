"""消费端剔除 HUD 距离徽章造成的假线：检测器 + 剔除规则 + 有效性评估。

背景（2026-11-12）：整段对局复核发现，微调后的权重会把画面中央的 HUD 距离徽章
（红环 + 随机头像 + 下方「74米」白字）画成一条竖直车道线，连续 30+ 帧稳定存在。
徽章内的头像是随机的，**模板匹配不可用**；但有两样东西不随头像变：

  1. 红色圆环（饱和度高的环状连通域）；
  2. 环正下方的白色数字 + 「米」字（白字黑边，横向一条亮带）。

所以检测器 = 红环连通域 + 下方亮字带 双条件，不依赖头像内容。

剔除规则不能是「把这块概率清零」——贴脸真线正好从徽章旁边穿过（f0204 真线
x258–282，徽章中心 x≈301），清零会把真线一起挖掉。改为**按线剔除**：
一条候选线如果满足「x 落在徽章横向范围内」且「纵向支撑有 >=60% 的行落在徽章
纵向范围内」，说明它只存在于徽章上，丢弃；真线纵向跨 90+ 行、远超徽章高度，
比例上不去，保留。

用法：
    python probe_hud_gate.py <权重名> [--session 会话名] [--step N]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fine_tune"))
import probe_first_look as P  # noqa: E402
import make_target_labels as mtl  # noqa: E402

OLD = (HERE.parents[2] / "maaracing_master/plugins/speedrush/resources/models/lane"
       / "plus_small_384x640.onnx")
RAW_ROOT = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"

# 徽章纵向必须覆盖的支撑比例：超过就认为这条线只长在徽章上
INSIDE_RATIO = 0.85


_MI_CACHE = {"tried": False, "tpl": None}
MI_TPL = HERE / "fine_tune" / "assets" / "hud_mi_tpl.png"


def _mi_template():
    if not _MI_CACHE["tried"]:
        _MI_CACHE["tried"] = True
        if MI_TPL.exists():
            _MI_CACHE["tpl"] = cv2.imread(str(MI_TPL))
    return _MI_CACHE["tpl"]


def detect_badge(bgr):
    """检测 HUD 距离徽章，返回 (cx, cy, r, box) 或 None（坐标均为全分辨率）。

    为什么用「米」字而不是徽章本身：徽章里的头像是**随机**的，整图模板匹配跨
    会话必然失效（实测换一局就掉到 0.4 以下）；红环检测又会被画面里其它红色物体
    （车尾灯、广告牌）干扰（实测 562 帧只检出 6%）。而「米」字是固定字体固定位置
    渲染：含徽章的帧匹配得分 0.74~1.00、纵坐标恒为 y=641；不含徽章的帧只有
    0.38~0.47。所以以「米」为锚点，反推徽章框。
    """
    tpl = _mi_template()
    if tpl is None:
        return None
    H, W = bgr.shape[:2]
    # 「米」字纵坐标固定（实测恒为 0.890H），只搜这一条窄带即可：ROI 从 144 行
    # 缩到 50 行，耗时从 2.0ms 降到 0.6ms。
    y0, y1 = int(H * 0.855), int(H * 0.930)
    x0, x1 = int(W * 0.28), int(W * 0.66)
    roi = bgr[y0:y1, x0:x1]
    if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
        return None
    r = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(r)
    if mx < 0.72:
        return None
    # 「米」字的纵坐标是固定的（HUD 固定位置，实测恒为 641/720）；路面纹理偶尔
    # 也能凑出 0.65 的假匹配，但位置对不上。这一条把误检从 98% 压回真实比例。
    if abs((loc[1] + y0 + tpl.shape[0] // 2) - H * 0.890) > 5:
        return None
    mx_c = loc[0] + x0 + tpl.shape[1] // 2
    my_c = loc[1] + y0 + tpl.shape[0] // 2
    # 「米」在徽章下方：徽章中心约在 (米x-6, 米y-24)，半径约 20
    cx, cy, rad = mx_c - 6, my_c - 24, 21
    box = (cx - rad - 6, cy - rad - 4, cx + rad + 6, my_c + 44)  # 含环、字、下方箭头
    return cx, cy, rad, box


def line_inside_badge(line, badge):
    """这条线的纵向支撑有多大比例落在徽章纵向范围内（且横向贴着徽章）。"""
    if badge is None:
        return 0.0
    cx, cy, r, box = badge
    xs, ys = [], []
    for y in range(max(int(line["y0"]), 412), min(int(line["y1"]), 719), 4):
        xs.append((line["s"] * y + line["a"]) / 2.0)
        ys.append(y)
    if len(ys) < 3:
        return 0.0
    xs = np.array(xs)          # 内容域 x（640 宽）
    # badge 坐标是全分辨率（1280 宽），换算到内容域再比
    if abs(float(np.median(xs)) - cx / 2.0) > (r / 2.0) * 1.6:
        return 0.0
    ys = np.array(ys)
    inside = ((ys >= box[1]) & (ys <= box[3])).mean()
    return float(inside)


def main():
    args = sys.argv[1:]
    weight = args[0] if args and not args[0].startswith("--") else "plus_small_verified74b_384x640.onnx"
    step, sessions = 1, []
    i = 1 if args and not args[0].startswith("--") else 0
    while i < len(args):
        if args[i] == "--step":
            step = int(args[i + 1]); i += 2
        elif args[i] == "--session":
            sessions.append(args[i + 1]); i += 2
        else:
            sessions.append(args[i]); i += 1
    sessions = sessions or ["20261109_104823_manual_p2", "20261109_095756_manual_p2"]

    sn = ort.InferenceSession(str(HERE / "weights" / weight),
                              providers=["CPUExecutionProvider"])
    t0 = time.perf_counter()
    tot = det = kept = dropped = dropped_real = 0
    ratio_icon, ratio_real = [], []
    for sess in sessions:
        d = RAW_ROOT / sess / "raw"
        if not d.exists():
            continue
        files = sorted(d.glob("*.jpg"))[::step]
        for fp in files:
            bgr = cv2.imread(str(fp))
            if bgr is None:
                continue
            tot += 1
            badge = detect_badge(bgr)
            if badge is not None:
                det += 1
            lines = P.extract_lines(bgr, P.lane_prob(sn, bgr, padded=True))
            content = cv2.resize(bgr, (640, 360), interpolation=cv2.INTER_AREA)
            gm, ginfo = mtl.extract(content)
            gt_x = (ginfo["x"] + ginfo["w"] / 2) if ginfo else None
            for c in lines:
                if not (c["y0"] <= 620 <= c["y1"]):
                    continue
                x = (c["s"] * 620 + c["a"]) / 2.0
                if not (180 <= x <= 360):
                    continue
                ratio = line_inside_badge(c, badge)
                is_real = gt_x is not None and abs(x - gt_x) < 20
                (ratio_real if is_real else ratio_icon).append(round(ratio, 2))
                if ratio >= INSIDE_RATIO:
                    dropped += 1
                    if is_real:
                        dropped_real += 1
                else:
                    kept += 1
    print(f"帧数 {tot}，检出徽章 {det} ({100*det/max(1,tot):.0f}%)  用时 {time.perf_counter()-t0:.0f}s")
    print(f"近带候选线 {kept+dropped} 条：剔除 {dropped} 条，保留 {kept} 条；"
          f"其中被误剔的真线 {dropped_real} 条")
    if ratio_icon:
        print(f"非真线（含徽章假线）支撑落在徽章内的比例 p50/p90 "
              f"{np.percentile(ratio_icon,[50,90]).round(2)}")
    if ratio_real:
        print(f"真线的同一比例 p50/p90/max "
              f"{np.percentile(ratio_real,[50,90]).round(2)} / {max(ratio_real):.2f}")


if __name__ == "__main__":
    main()
