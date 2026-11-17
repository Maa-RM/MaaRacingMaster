# -*- coding: utf-8 -*-
"""邻道锚点门 A/B 复算（只读）：旧横向门 vs 新框完整性判据。

**要回答**（P3 杠杆 1 的进生产依据）：
1. 去掉「横向米制位 >1.2m 不给锚」后，锚点覆盖涨多少、本道锚点掉多少；
2. 新增覆盖里有多少是侧视/并框合并（宽高比超上限）——这些必须继续弃权；
3. 新增覆盖的距离与横向分布，确认增量落在决策关心的近距邻道。

**输入**：既有会话的逐帧检测缓存（`speedrush_depthart_prod/adjacent_seq_out/
scan_cache.json`，由 `probe_adjacent_seq.py` 跑 ONNX 生成）。trace 本身没有框
边，无法反推完整性，故用检测缓存复算。

**口径**：A = 旧生产门（横向 |x_m|≤1.2 且左右不贴边）；B = 现生产门
（`depth_geo._box_anchor_z`：左右/下缘不贴边、宽高比 ≤ `ANCHOR_BOX_AR_MAX`，
横向不设限）。z 域 2~90m、框最小 3px 与生产一致。

用法：``python tools/experiments/speedrush_agility/probe_anchor_gate_ab.py``
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.speedrush import depth_geo as dg  # noqa: E402

CACHE = (ROOT / "tools" / "experiments" / "speedrush_depthart_prod"
         / "adjacent_seq_out" / "scan_cache.json")
ADJ_X_M = 1.2


def legacy_anchored(box, y_v: float) -> bool:
    """旧生产门：横向 1.2m 一刀切 + 左右贴边弃权。"""
    x0, y0, x1, y1 = box
    if x1 - x0 < 3 or y1 - y0 < 3 or y1 - y_v <= 2.0:
        return False
    z = dg.FOCAL_PX * dg.CAM_H_M / (y1 - y_v)
    if not dg.ANCHOR_Z_LO <= z <= dg.ANCHOR_Z_HI:
        return False
    x_m = (0.5 * (x0 + x1) - dg.PT_CX) * z / dg.FOCAL_PX
    return abs(x_m) <= ADJ_X_M and x0 > 0 and x1 < dg.FRAME_W


def main() -> None:
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    stat = {"frames": 0, "boxes": 0, "a": 0, "b": 0, "add": 0, "add_ar_bad": 0,
            "a_lost": 0}
    add_rows = []
    for drive, v in data.items():
        yv_by = dict((i, y) for i, y in v["yvs"])
        boxes_by = dict((i, bs) for i, bs in v["boxes"])
        for idx in range(len(v["yvs"])):
            stat["frames"] += 1
            yv = yv_by.get(idx) or dg.FALLBACK_Y_V
            for box in boxes_by.get(idx, []):
                stat["boxes"] += 1
                a_ok = legacy_anchored(box, yv)
                b_ok = dg._box_anchor_z(tuple(box), yv) is not None
                stat["a"] += a_ok
                stat["b"] += b_ok
                if a_ok and not b_ok:
                    stat["a_lost"] += 1
                if b_ok and not a_ok:
                    stat["add"] += 1
                    x0, y0, x1, y1 = box
                    z = dg.FOCAL_PX * dg.CAM_H_M / (y1 - yv)
                    x_m = (0.5 * (x0 + x1) - dg.PT_CX) * z / dg.FOCAL_PX
                    add_rows.append((z, abs(x_m), (x1 - x0) / float(y1 - y0),
                                     drive))
    print("帧", stat["frames"], "框", stat["boxes"],
          "A", stat["a"], "B", stat["b"], "新增", stat["add"],
          "本道掉锚", stat["a_lost"])
    if add_rows:
        zs = sorted(r[0] for r in add_rows)
        xs = sorted(r[1] for r in add_rows)
        ars = sorted(r[2] for r in add_rows)
        q = lambda seq, p: seq[min(len(seq) - 1, int(p * len(seq)))]
        print("新增 z p10/50/90 %.1f/%.1f/%.1f" %
              (q(zs, 0.1), q(zs, 0.5), q(zs, 0.9)))
        print("新增 |x| p10/50/90 %.2f/%.2f/%.2f" %
              (q(xs, 0.1), q(xs, 0.5), q(xs, 0.9)))
        print("新增宽高比 p10/50/90 %.2f/%.2f/%.2f" %
              (q(ars, 0.1), q(ars, 0.5), q(ars, 0.9)))
        near = [r for r in add_rows if 4.0 <= r[0] <= 20.0]
        print("新增 4~20m", len(near),
              "其中宽高比 >%.1f" % dg.ANCHOR_BOX_AR_MAX,
              sum(r[2] > dg.ANCHOR_BOX_AR_MAX for r in near))


if __name__ == "__main__":
    main()
