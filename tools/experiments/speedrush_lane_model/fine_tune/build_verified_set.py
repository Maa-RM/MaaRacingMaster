# -*- coding: utf-8 -*-
"""把逐张目检通过的帧落成训练集（只有 keep=true 的帧进训练）。

维护者 2026-11-12 裁定：
- `20261109_104823_manual_p2` 的 240 之后是飞坡段、180~260 是验证段，整段不用；
- `20261107_221207_manual_p1` 整段是车尾特写/自动巡航视角，作废；
- f0183 / f0191 / f0195 标注有问题；
- **每一张都必须人眼看过再喂进去**——自动判据只做初筛。

KEEP 是人工目检结论（2026-11-12 两轮共看 130 张候选，留 74 张），
不接受自动重算覆盖：要改就得重新看图。

产物：fine_tune_out/sam_labels_verified/{masks,index.json}
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC_DIRS = [ROOT / "fine_tune_out" / "candidates",
            ROOT / "fine_tune_out" / "candidates_r2"]
OUT = ROOT / "fine_tune_out" / "sam_labels_verified"
(OUT / "masks").mkdir(parents=True, exist_ok=True)

# 第一轮：63 张候选里留 29 张
KEEP_R1 = {
    ("20261107_203433_manual_p1", "0187"), ("20261107_203433_manual_p1", "0355"),
    ("20261107_203433_manual_p1", "0463"), ("20261107_203433_manual_p1", "0565"),
    ("20261107_203433_manual_p1", "0574"),
    ("20261107_203433_manual_p2", "0241"), ("20261107_203433_manual_p2", "0340"),
    ("20261107_203433_manual_p2", "0490"), ("20261107_203433_manual_p2", "0532"),
    ("20261107_203433_manual_p2", "0694"), ("20261107_203433_manual_p2", "0769"),
    ("20261107_215421_manual_p1", "0175"), ("20261107_215421_manual_p1", "0334"),
    ("20261107_215421_manual_p1", "0343"), ("20261107_215421_manual_p1", "0502"),
    ("20261107_215421_manual_p1", "0568"),
    ("20261109_095756_manual_p1", "0235"), ("20261109_095756_manual_p1", "0364"),
    ("20261109_095756_manual_p1", "0526"), ("20261109_095756_manual_p1", "0676"),
    ("20261109_095756_manual_p2", "0001"), ("20261109_095756_manual_p2", "0139"),
    ("20261109_095756_manual_p2", "0517"), ("20261109_095756_manual_p2", "0622"),
    ("20261109_104823_manual_p1", "0298"), ("20261109_104823_manual_p1", "0442"),
    ("20261109_104823_manual_p1", "0472"), ("20261109_104823_manual_p1", "0622"),
    ("20261109_104823_manual_p1", "0679"),
}

# 第二轮：67 张候选里留 45 张
KEEP_R2 = {
    ("20261107_203433_manual_p1", "0184"), ("20261107_203433_manual_p1", "0277"),
    ("20261107_203433_manual_p1", "0280"), ("20261107_203433_manual_p1", "0346"),
    ("20261107_203433_manual_p1", "0349"), ("20261107_203433_manual_p1", "0568"),
    ("20261107_203433_manual_p1", "0571"), ("20261107_203433_manual_p1", "0577"),
    ("20261107_203433_manual_p1", "0646"), ("20261107_203433_manual_p1", "0652"),
    ("20261107_203433_manual_p2", "0031"), ("20261107_203433_manual_p2", "0199"),
    ("20261107_203433_manual_p2", "0334"), ("20261107_203433_manual_p2", "0346"),
    ("20261107_203433_manual_p2", "0415"), ("20261107_203433_manual_p2", "0697"),
    ("20261107_203433_manual_p2", "0763"), ("20261107_203433_manual_p2", "0775"),
    ("20261107_203433_manual_p2", "0784"),
    ("20261107_215421_manual_p1", "0157"), ("20261107_215421_manual_p1", "0202"),
    ("20261107_215421_manual_p1", "0205"), ("20261107_215421_manual_p1", "0358"),
    ("20261107_215421_manual_p1", "0469"), ("20261107_215421_manual_p1", "0505"),
    ("20261109_095756_manual_p1", "0415"), ("20261109_095756_manual_p1", "0511"),
    ("20261109_095756_manual_p1", "0520"), ("20261109_095756_manual_p1", "0655"),
    ("20261109_095756_manual_p2", "0196"), ("20261109_095756_manual_p2", "0331"),
    ("20261109_095756_manual_p2", "0451"), ("20261109_095756_manual_p2", "0511"),
    ("20261109_095756_manual_p2", "0520"), ("20261109_095756_manual_p2", "0661"),
    ("20261109_095756_manual_p2", "0790"),
    ("20261109_104823_manual_p1", "0166"), ("20261109_104823_manual_p1", "0283"),
    ("20261109_104823_manual_p1", "0352"), ("20261109_104823_manual_p1", "0445"),
    ("20261109_104823_manual_p1", "0532"), ("20261109_104823_manual_p1", "0547"),
    ("20261109_104823_manual_p1", "0625"), ("20261109_104823_manual_p1", "0682"),
    ("20261109_104823_manual_p1", "0694"),
}

KEEP = KEEP_R1 | KEEP_R2


def main():
    rows, seen = [], set()
    for src in SRC_DIRS:
        idx_path = src / "index.json"
        if not idx_path.exists():
            print(f"缺 {idx_path}")
            continue
        for r in json.loads(idx_path.read_text(encoding="utf-8")):
            key = (r["session"], r["fid"])
            if key not in KEEP or key in seen:
                continue
            m = cv2.imread(str(src / r["mask"]), 0)
            if m is None:
                continue
            dst = OUT / "masks" / f"{r['name']}.png"
            cv2.imwrite(str(dst), m)
            ys, xs = (m > 0).nonzero()
            rows.append(dict(name=r["name"], session=r["session"], fid=r["fid"],
                             raw=r["raw"], mask=f"masks/{r['name']}.png",
                             area=int((m > 0).sum()),
                             x0=int(xs.min()), x1=int(xs.max()),
                             y0=int(ys.min()), y1=int(ys.max()),
                             verified=True))
            seen.add(key)
    (OUT / "index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    print(f"目检通过合计 {len(rows)} 帧（KEEP 名单 {len(KEEP)} 个）")
    by = {}
    for r in rows:
        by[r["session"]] = by.get(r["session"], 0) + 1
    for k, v in sorted(by.items()):
        print(f"  {k}: {v}")
    print("产物:", OUT)


if __name__ == "__main__":
    main()
