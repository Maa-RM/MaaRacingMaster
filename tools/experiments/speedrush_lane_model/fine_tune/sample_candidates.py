# -*- coding: utf-8 -*-
"""换会话重新采样正例，并生成**逐张目检**用的分批图。

背景（2026-11-12 维护者裁定）：
- `20261109_104823_manual_p2` 的 240 之后是飞坡段，全部不要；
- 该会话 180~260 是连帧验证段，也不能进训练；
- f0183 / f0191 / f0195 三张的黄色标注有问题（部分覆盖、标到车身、被 HUD 拽偏）；
- 结论：自动判据只能初筛，**必须每一张人眼看过再进训练集**。

本脚本只做初筛与出图，不做最终取舍。目检结论写回 `verified.json` 的 keep 字段，
训练脚本只读 keep=true 的帧。

用法：python fine_tune/sample_candidates.py [每会话张数]
产物：fine_tune_out/candidates/{index.json, sheets/sheet_N.jpg}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import make_target_labels as mtl  # noqa: E402

RAW = Path.home() / "AppData/Roaming/MaaRacingMaster/debug/speedrush"
OUT = ROOT / "fine_tune_out" / (sys.argv[2] if len(sys.argv) > 2 else "candidates")
(OUT / "sheets").mkdir(parents=True, exist_ok=True)

# 排除 104823_p2：240+ 飞坡、180~260 是验证段，整段不用最省心
SESSIONS = [
    "20261107_203433_manual_p1", "20261107_203433_manual_p2",
    "20261107_215421_manual_p1",
    # 20261107_221207_manual_p1 整段作废：全是车尾特写/自动巡航视角
    "20261107_222251_manual_p1", "20261109_095756_manual_p1",
    "20261109_095756_manual_p2", "20261109_104823_manual_p1",
]
PER_SESSION = int(sys.argv[1]) if len(sys.argv) > 1 else 9
STEP = 3
PER_SHEET = 12
CROP = (180, 420, 240, 360)      # 近场裁切（内容域 x0,x1,y0,y1）


def jagged(mask):
    """掩码逐行中心去趋势后的残差 std；锯齿/碎片会显著偏高。"""
    ys, cs = [], []
    for y in range(mask.shape[0]):
        q = np.flatnonzero(mask[y])
        if len(q) == 0:
            continue
        ys.append(y); cs.append(q.mean())
    if len(ys) < 10:
        return 99.0
    ys = np.array(ys, float); cs = np.array(cs, float)
    A = np.stack([ys, np.ones_like(ys)], 1)
    coef, *_ = np.linalg.lstsq(A, cs, rcond=None)
    return float(np.std(cs - A @ coef))


def main():
    used = set()
    prev = ROOT / "fine_tune_out" / "candidates" / "index.json"
    if prev.exists():
        for r in json.loads(prev.read_text(encoding="utf-8")):
            used.add((r["session"], r["fid"]))
    picked = []
    for sess in SESSIONS:
        d = RAW / sess / "raw"
        if not d.exists():
            print(f"缺会话 {sess}")
            continue
        found = []
        for fp in sorted(d.glob("*.jpg"))[::STEP]:
            im = cv2.imread(str(fp))
            if im is None:
                continue
            s = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
            m, info = mtl.extract(s)
            if m is None:
                continue
            fid0 = fp.stem.split("_")[0]
            if (sess, fid0) in used:
                continue                      # 上一轮已采过（含已淘汰的）
            if jagged(m) > 2.0:
                continue                      # 锯齿/碎片，目检也是废
            found.append((fp, fid0, info))
        if not found:
            print(f"{sess}: 无候选")
            continue
        idx = np.linspace(0, len(found) - 1,
                          min(PER_SESSION, len(found))).round().astype(int)
        take = [found[int(i)] for i in idx]
        picked += [(sess, fp, fid, info) for fp, fid, info in take]
        print(f"{sess}: 候选 {len(found)} → 取 {len(take)}")

    rows = []
    for sess, fp, fid, info in picked:
        im = cv2.imread(str(fp))
        s = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
        m, _ = mtl.extract(s)
        name = f"{sess}_{fid}"
        cv2.imwrite(str(OUT / f"{name}_mask.png"), m)
        rows.append(dict(name=name, session=sess, fid=fid, raw=str(fp),
                         mask=f"{name}_mask.png", bbox=info, keep=None))
    (OUT / "index.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    # 分批目检图：每格近场裁切放大，标名字与掩码
    x0, x1, y0, y1 = CROP
    for bi in range(0, len(rows), PER_SHEET):
        chunk = rows[bi:bi + PER_SHEET]
        tiles = []
        for r in chunk:
            im = cv2.imread(r["raw"])
            s = cv2.resize(im, (640, 360), interpolation=cv2.INTER_AREA)
            crop = s[y0:y1, x0:x1].copy()
            m = cv2.imread(str(OUT / r["mask"]), 0)
            mm = m[y0:y1, x0:x1] > 0
            crop[mm] = (0.25 * crop[mm]
                        + 0.75 * np.array([0, 255, 255], np.float32)).astype(np.uint8)
            crop = cv2.resize(crop, ((x1 - x0) * 2, (y1 - y0) * 2),
                              interpolation=cv2.INTER_NEAREST)
            tag = f"{r['session'][9:15]}{r['session'][-2:]}_{r['fid']}"
            cv2.putText(crop, tag, (6, 24), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (255, 255, 255), 2)
            tiles.append(crop)
        while len(tiles) % 4:
            tiles.append(np.zeros_like(tiles[0]))
        sheet = np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)])
        cv2.imwrite(str(OUT / "sheets" / f"sheet_{bi // PER_SHEET}.jpg"),
                    sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"候选 {len(rows)} 帧，{len(range(0, len(rows), PER_SHEET))} 张目检图")
    print("产物:", OUT)


if __name__ == "__main__":
    main()
