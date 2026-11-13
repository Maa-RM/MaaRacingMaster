#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把审计的候选标签合并成一份**待复核副本**（不动原标签、不动原图）。

审计（`audit_labels.py --suggest`）给出的是模型对"疑似漏标"的候选；本脚本把它们
并进原始标签，得到一个新的目录供人在标注工具里过一遍：

- 图片用硬链接（同盘不占空间），标签是**合并后的副本**；
- 候选先过两道过滤：与已有任何 GT 的 IoU > 0.2 的丢掉（防重复），
  "画面底部两角 + 大面积"的丢掉（第一人称下常常是自车车头，不是要标的目标）；
- 每个文件的增删都写进 `_merge_report.csv`，复核时按需回查。

复核完成后，训练用它作为新来源；原始目录保持原样，随时可回退。

用法：
    python .../apply_suggestions.py --images D:/yolo/1108 --suggestions build/audit/suggested_1108 \\
        --out D:/yolo/1108_review
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO))


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def read_rows(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    return [line.split() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse(rows: list[list[str]]) -> list[tuple[int, tuple]]:
    boxes = []
    for parts in rows:
        if len(parts) == 5:
            cls, cx, cy, bw, bh = int(parts[0]), *map(float, parts[1:])
            boxes.append((cls, (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)))
    return boxes


def iou(a: tuple, b: tuple) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union if union > 0 else 0.0


def is_own_car(box: tuple) -> bool:
    """画面底部两角的大框：第一人称下车头/自己车身会露出来，不该标成社会车。"""
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    return cy > 0.86 and (cx < 0.28 or cx > 0.72)


def link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main() -> int:
    try:
        # sys.stdout 类型声明是 TextIO，reconfigure 是 TextIOWrapper 的方法——经 getattr 取用
        reconf = getattr(sys.stdout, "reconfigure", None)
        if callable(reconf):
            reconf(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="合并审计候选标签为待复核副本")
    parser.add_argument("--images", required=True, help="原始图片目录（也是原标签目录）")
    parser.add_argument("--suggestions", required=True, help="audit_labels.py --suggest 的输出目录")
    parser.add_argument("--out", required=True, help="复核副本目录（新建）")
    parser.add_argument("--min-iou-drop", type=float, default=0.2,
                        help="与已有任意 GT 的 IoU 超过此值即视为重复，不合并")
    parser.add_argument("--keep-own", action="store_true", help="保留底部两角的大框")
    parser.add_argument("--force", action="store_true", help="输出目录非空时清空重建")
    args = parser.parse_args()

    images_dir = Path(args.images)
    sugg_dir = Path(args.suggestions)
    out_dir = Path(args.out)
    if out_dir.exists() and any(out_dir.iterdir()):
        if not args.force:
            print(f"输出目录非空：{out_dir}（加 --force 清空重建）", file=sys.stderr)
            return 2
        for item in out_dir.iterdir():
            if item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item)
    out_dir.mkdir(parents=True, exist_ok=True)

    report_rows = []
    totals = {"added": 0, "drop_duplicate": 0, "drop_own": 0, "files": 0}
    for image in sorted(images_dir.iterdir()):
        if image.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        label = image.with_suffix(".txt")
        existing_rows = read_rows(label)
        existing = parse(existing_rows)
        candidates = parse(read_rows(sugg_dir / f"{image.stem}.txt"))

        accepted, drop_dup, drop_own = [], 0, 0
        for cls, box in candidates:
            if any(iou(box, other) > args.min_iou_drop for _c, other in existing + accepted):
                drop_dup += 1
                continue
            if not args.keep_own and is_own_car(box):
                drop_own += 1
                continue
            accepted.append((cls, box))

        link_or_copy(image, out_dir / image.name)
        lines = list(existing_rows)
        for cls, box in accepted:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            bw, bh = box[2] - box[0], box[3] - box[1]
            lines.append([str(cls), f"{cx:.6f}", f"{cy:.6f}", f"{bw:.6f}", f"{bh:.6f}"])
        (out_dir / f"{image.stem}.txt").write_text(
            "\n".join(" ".join(parts) for parts in lines) + "\n", encoding="utf-8")

        totals["files"] += 1
        totals["added"] += len(accepted)
        totals["drop_duplicate"] += drop_dup
        totals["drop_own"] += drop_own
        if accepted or drop_dup or drop_own:
            report_rows.append({
                "image": image.name, "existing": len(existing), "added": len(accepted),
                "drop_duplicate": drop_dup, "drop_own": drop_own,
                "classes_added": " ".join(str(c) for c, _b in sorted(accepted)),
            })

    with (out_dir / "_merge_report.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "existing", "added",
                                                    "drop_duplicate", "drop_own",
                                                    "classes_added"])
        writer.writeheader()
        writer.writerows(report_rows)

    print(f"复核副本: {out_dir}")
    print(f"  文件 {totals['files']}，新增 {totals['added']} 框，"
          f"丢重复 {totals['drop_duplicate']}，丢自车 {totals['drop_own']}")
    print("  明细见 _merge_report.csv；在标注工具里过完后用它作为训练来源。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
