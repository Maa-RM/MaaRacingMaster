# -*- coding: utf-8 -*-
"""目标构型正例标签：近场车头区那条粗白虚线，只由像素亮度真值生成。

与旧的 `make_labels.py` 根本不同：**标签不来自任何模型输出**。来源是像素级
局部亮度差（白线比周围沥青亮 36~47 级，61 列大窗口背景），连通域按「细长、
落在近场中央、跨到画面底部」筛选，再按行中心重画细掩码。每帧只标这一条线，
其余标线不进标签——训练时由蒸馏项保持现役模型在其他区域的行为（见
`train_targeted.py`），避免灾难性遗忘。

黄昏下这条白线偏米黄（饱和度中位约 124），不能按「白色低饱和」筛；这也是
旧打标链把目标漏掉的原因之一。

自包含，不 import 业务码。用法：.venv/Scripts/python.exe fine_tune/make_target_labels.py
产物：fine_tune_out/target_labels/{masks,overlay}/、index.json
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
OUT = ROOT / "fine_tune_out" / "target_labels"

SESSIONS = (
    "20261109_104823_manual_p2", "20261109_104823_manual_p1",
    "20261109_095756_manual_p1", "20261109_095756_manual_p2",
    "20261107_203433_manual_p1", "20261107_203433_manual_p2",
    "20261107_215421_manual_p1", "20261108_133512_manual_p1",
)
STEP = 3                 # 抽帧步长（相邻帧高度相关）
PER_SESSION_CAP = 8      # 每会话最多取几帧，避免全挤在同一片段
HOLD_OUT = {("20261109_104823_manual_p2", "0204")}   # 验收帧，绝不进训练


_TPL_CACHE = {"tried": False, "img": None}
TPL_PATH = Path(__file__).resolve().parent / "assets" / "hud_icon_tpl.png"


def _icon_template():
    """HUD 圆形距离标记的模板（26x26）。缺文件时静默降级为纯几何检测。"""
    if not _TPL_CACHE["tried"]:
        _TPL_CACHE["tried"] = True
        if TPL_PATH.exists():
            _TPL_CACHE["img"] = cv2.imread(str(TPL_PATH))
    return _TPL_CACHE["img"]


def hud_boxes(content_bgr: np.ndarray):
    """近场中央的 HUD 圆形标记（红白「61米」/ 白底「53米」等）的外接框。

    为什么必须显式排除（2026-11-12 维护者逐张目检查出）：
    图标与那条贴脸白线常常**连成同一个连通域**，`h/w >= 1.7` 这种整体形状闸
    对它无效；连通域重心因此被图标拽偏，最终掩码落在图标上而不是线上。
    两类都要抓：红色圆环（多数）与白/亮底圆形（如 f0195 的「53米」）。
    """
    boxes = []
    hsv = cv2.cvtColor(content_bgr, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((H <= 12) | (H >= 168)) & (S > 80) & (V > 60)).astype(np.uint8)
    red[:275, :] = 0
    red[:, :200] = 0
    red[:, 340:] = 0
    n, _, st, _ = cv2.connectedComponentsWithStats(red, 8)
    for i in range(1, n):
        x, y, w, h, a = map(int, st[i])
        if a < 120:
            continue
        if not (0.55 <= w / max(1, h) <= 1.8):   # 近圆形
            continue
        boxes.append((x, y, x + w, y + h))
    # 兜底：近场中央的圆形亮斑（白底图标、圆环外圈）
    gray = cv2.cvtColor(content_bgr, cv2.COLOR_BGR2GRAY)
    roi = gray[268:352, 228:342]
    if roi.size:
        blur = cv2.GaussianBlur(roi, (5, 5), 0)
        cir = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1, minDist=24,
                               param1=90, param2=16, minRadius=7, maxRadius=26)
        if cir is not None:
            for cx, cy, r in np.round(cir[0]).astype(int):
                boxes.append((cx + 228 - r, cy + 268 - r,
                              cx + 228 + r, cy + 268 + r))
    # 主判据：模板匹配。红环连通域在上面常常碎成几个小框（实测框偏到图标右侧
    # 10~25px，标签清不干净，模型照旧把图标当线），而 HUD 图标是同一张精灵图、
    # 尺寸与外观稳定——模板匹配在含图标的帧上得分 0.88~1.00，不含图标的会话
    # 最高只有 0.57，用 0.65 作阈值即可分开。
    tpl = _icon_template()
    if tpl is not None:
        sx0, sy0, sx1, sy1 = 235, 265, 345, 355
        search = content_bgr[sy0:sy1, sx0:sx1]
        if search.shape[0] >= tpl.shape[0] and search.shape[1] >= tpl.shape[1]:
            r = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx >= 0.65:
                cx = loc[0] + sx0 + tpl.shape[1] // 2
                cy = loc[1] + sy0 + tpl.shape[0] // 2
                hw, hh = tpl.shape[1] // 2, tpl.shape[0] // 2
                # 往下多留 10px：图标下方的「59米」文字也常被当成线的一部分
                boxes.append((cx - hw, cy - hh, cx + hw, cy + hh + 10))
    return boxes


def extract(content_bgr: np.ndarray, exclude_hud: bool = True):
    """一帧 → (360x640 细掩码, 线 bbox 与面积) 或 (None, None)。"""
    V = cv2.cvtColor(content_bgr, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.float32)
    bg = cv2.boxFilter(V, -1, (61, 1), normalize=True)
    cand = ((V - bg > 18) & (V > 100)).astype(np.uint8)
    cand[:248, :] = 0
    cand[:, :20] = 0
    cand[:, 620:] = 0
    if exclude_hud:
        # 先把 HUD 图标整块挖掉，再找连通域：宁可线被截短/漏检，也不让掩码
        # 落到图标上——错的真值比没有真值更有害（会把模型的对判成错）。
        for x0, y0, x1, y1 in hud_boxes(content_bgr):
            cand[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2] = 0
    closed = cv2.morphologyEx(
        cand, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 5)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(closed, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, area = map(int, st[i])
        if h < 45 or w < 4 or w > 75 or area < 200:
            continue
        if h / max(w, 1) < 1.7:          # 细长，排除 HUD 圆形图标与金币
            continue
        if y + h < 300:                  # 必须伸到画面下部（贴脸构型）
            continue                     # 收到 340 会误杀 f0180（真线 y1=338）——
                                         # 车身（f0191, y1=328）与真线差仅 10px，
                                         # 底边分不开，靠逐张目检兜底。
        if not (170 <= x + w and x <= 420):   # 近场中央区，排除护栏
            continue
        if best is None or area > best[0]:
            best = (area, i, x, y, w, h)
    if best is None:
        return None, None
    _, i, x, y, w, h = best
    # 背景判据：贴脸车道线两侧都是暗沥青；护栏/桥梁/天空边缘一侧会明显发亮。
    # 2026-11-12 核对页发现 f242/243/245/250 的「线」其实是右侧护栏结构——
    # 它同样细长、同样延伸到画面底部，形状闸分不开，只能看两侧背景。
    side = V[y:y + h, :]
    lft = side[:, max(0, x - 26):max(0, x - 6)]
    rgt = side[:, x + w + 6:x + w + 26]
    sides = [np.median(a) for a in (lft, rgt) if a.size >= 20]
    if sides and max(sides) > 140:
        return None, None
    comp = (lab == i)
    mask = np.zeros((360, 640), np.uint8)
    for yy in range(y, y + h):
        idx = np.flatnonzero(comp[yy])
        if len(idx) == 0:
            continue
        xc = int(idx.mean())
        mask[yy, max(0, xc - 5):min(640, xc + 6)] = 255
    return mask, {"x": x, "y": y, "w": w, "h": h, "area": int(best[0])}


def main():
    (OUT / "masks").mkdir(parents=True, exist_ok=True)
    (OUT / "overlay").mkdir(parents=True, exist_ok=True)
    index = []
    for sess in SESSIONS:
        d = RAW / sess / "raw"
        if not d.exists():
            print("缺会话:", sess)
            continue
        found = []
        for fp in sorted(d.glob("*.jpg"))[::STEP]:
            fid = fp.stem.split("_")[0]
            if (sess, fid) in HOLD_OUT:
                continue
            im = cv2.imread(str(fp))
            if im is None:
                continue
            s = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
            m, info = extract(s)
            if m is None:
                continue
            found.append((fp, fid, im, m, info))
        # 均匀取样，避免全来自同一段
        if len(found) > PER_SESSION_CAP:
            take = np.linspace(0, len(found) - 1, PER_SESSION_CAP).round().astype(int)
            found = [found[int(i)] for i in take]
        for fp, fid, im, m, info in found:
            name = f"{sess}_{fid}"
            cv2.imwrite(str(OUT / "masks" / f"{name}.png"), m)
            vis = im.copy()
            big = cv2.resize(m, (1280, 720), interpolation=cv2.INTER_NEAREST) > 0
            vis[big] = (0.3 * vis[big] + 0.7 * np.array([0, 255, 0], np.float32)).astype(np.uint8)
            cv2.imwrite(str(OUT / "overlay" / f"{name}.jpg"), vis,
                        [cv2.IMWRITE_JPEG_QUALITY, 90])
            index.append({"name": name, "session": sess, "fid": fid,
                          "raw": str(fp), "mask": f"masks/{name}.png",
                          "overlay": f"overlay/{name}.jpg",
                          "bbox": info, "pixels": int((m > 0).sum())})
        print(f"{sess}: 命中 {len(found)} 帧")
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    print("总计", len(index), "帧；清单:", OUT / "index.json")


if __name__ == "__main__":
    main()
