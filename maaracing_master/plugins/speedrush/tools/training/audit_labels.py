#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标注审计：格式、重复、疑似漏标、疑似类别冲突，一次列全。

**为什么用当前模型做审计**：人眼过一千张不现实，而"模型高置信报了、标签里却没有"
是漏标最强的线索——注意只看**中心点落不落在 GT 框内**，不看 IoU：近场大车常被模型
框得偏小（实测出现过 IoU 0.47），按 IoU 会把真检出错判成漏标。

**四类问题**
- ``bad_format``：字段数、类别越界、坐标越界、宽高非正；
- ``duplicate``：同类两框 IoU > 0.9（同一辆车标了两遍）；
- ``possible_missing``：置信度 ≥ ``--conf`` 的检出，中心不落在同类任何 GT 框内；
- ``class_conflict``：检出中心落在**另一类** GT 框内（类别归属可能标反）。

报告只列问题，不改标签。``--render`` 把有问题的帧画出来（GT 绿、检出红、问题黄字），
供人工抽查。

用法：
    python maaracing_master/plugins/speedrush/tools/training/audit_labels.py \\
        --render build/audit
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO))

import cv2  # noqa: E402

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402

DEFAULT_MODEL = (REPO / "maaracing_master" / "plugins" / "speedrush"
                 / "resources" / "models" / "perception" / "model.onnx")


def read_raw_labels(label: Path) -> list[list[str]]:
    if not label.is_file():
        return []
    return [line.split() for line in label.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_labels(rows: list[list[str]], n_classes: int) -> tuple[list[tuple[int, tuple]], list[str]]:
    """返回 (合法框, 格式问题描述)。合法框为归一化 (cls, (x1,y1,x2,y2))。"""
    boxes, issues = [], []
    for index, parts in enumerate(rows, 1):
        if len(parts) != 5:
            issues.append(f"第{index}行字段数 {len(parts)}")
            continue
        try:
            cls = int(parts[0])
            cx, cy, bw, bh = map(float, parts[1:])
        except ValueError:
            issues.append(f"第{index}行非数字")
            continue
        if not 0 <= cls < n_classes:
            issues.append(f"第{index}行类别越界 {cls}")
            continue
        if bw <= 0 or bh <= 0:
            issues.append(f"第{index}行宽高非正 {bw}x{bh}")
            continue
        if not all(-1e-6 <= v <= 1 + 1e-6 for v in (cx, cy, bw, bh)):
            issues.append(f"第{index}行坐标越界")
            continue
        boxes.append((cls, (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)))
    return boxes, issues


def iou(a: tuple, b: tuple) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union if union > 0 else 0.0


def point_inside(point: tuple[float, float], box: tuple) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def audit(model: Path, entries: list[dict], conf: float) -> dict:
    detector = YOLODetector(str(model), conf=conf, iou=0.45)
    name_to_id = {name: cid for cid, name in detector.classes.items()}
    id_to_name = {cid: name for name, cid in name_to_id.items()}
    counts: Counter = Counter()
    samples: dict[str, list] = {key: [] for key in
                                ("bad_format", "duplicate", "possible_missing", "class_conflict")}
    rendered: list[dict] = []
    suggestions: dict[str, list[str]] = {}

    for entry in entries:
        image_path, label_path = Path(entry["path"]), Path(entry["label"])
        bgr = cv2.imread(str(image_path))
        if bgr is None:
            counts["unreadable_image"] += 1
            continue
        height, width = bgr.shape[:2]
        raw = read_raw_labels(label_path)
        boxes, issues = parse_labels(raw, len(detector.classes))
        if issues:
            counts["bad_format"] += len(issues)
            samples["bad_format"].append({"image": image_path.name, "issues": issues[:5]})

        seen: list[tuple[int, tuple]] = []
        for index, (cls, box) in enumerate(boxes):
            if any(cls == other_cls and iou(box, other) > 0.9
                   for other_cls, other in seen):
                counts["duplicate"] += 1
                samples["duplicate"].append({"image": image_path.name, "class": id_to_name[cls]})
            seen.append((cls, box))

        _by_class, dets, _raw = detector(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        local_issues = []
        for det in dets:
            cid = name_to_id[det["class_name"]]
            x1, y1, x2, y2 = det["box"]
            center = ((x1 + x2) / 2 / width, (y1 + y2) / 2 / height)
            same = [box for cls, box in boxes if cls == cid]
            if not any(point_inside(center, box) for box in same):
                counts["possible_missing"] += 1
                samples["possible_missing"].append({
                    "image": image_path.name, "class": det["class_name"],
                    "conf": round(det["confidence"], 3),
                    "center": [round(center[0], 3), round(center[1], 3)],
                })
                local_issues.append(("missing", det, center))
                norm = (cid, (x1 / width, y1 / height, x2 / width, y2 / height))
                suggestions.setdefault(label_path.as_posix(), []).append(
                    f"{norm[0]} {((norm[1][0] + norm[1][2]) / 2):.6f} "
                    f"{((norm[1][1] + norm[1][3]) / 2):.6f} "
                    f"{(norm[1][2] - norm[1][0]):.6f} {(norm[1][3] - norm[1][1]):.6f}")
            other = [(cls, box) for cls, box in boxes
                     if cls != cid and point_inside(center, box)]
            if other:
                counts["class_conflict"] += 1
                samples["class_conflict"].append({
                    "image": image_path.name, "det": det["class_name"],
                    "label": id_to_name[other[0][0]],
                    "conf": round(det["confidence"], 3),
                })
                local_issues.append(("conflict", det, center))
        if local_issues:
            rendered.append({"image": bgr, "path": image_path, "boxes": boxes,
                             "issues": local_issues, "size": (width, height)})

    return {"counts": dict(counts),
            "samples": {k: v[:20] for k, v in samples.items()},
            "rendered": rendered,
            "suggestions": suggestions}


def render(report: dict, out_dir: Path, limit: int) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for item in report["rendered"]:
        if saved >= limit:
            break
        vis = item["image"].copy()
        width, height = item["size"]
        for cls, box in item["boxes"]:
            cv2.rectangle(vis, (int(box[0] * width), int(box[1] * height)),
                          (int(box[2] * width), int(box[3] * height)), (0, 255, 0), 2)
        for kind, det, _center in item["issues"]:
            x1, y1, x2, y2 = det["box"]
            cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 1)
            cv2.putText(vis, f"{kind} {det['class_name']} {det['confidence']:.2f}",
                        (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 255, 255), 1)
        cv2.imwrite(str(out_dir / f"{item['path'].stem}.jpg"), vis)
        saved += 1
    return saved


def main() -> int:
    try:
        # sys.stdout 类型声明是 TextIO，reconfigure 是 TextIOWrapper 的方法——经 getattr 取用
        reconf = getattr(sys.stdout, "reconfigure", None)
        if callable(reconf):
            reconf(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="标注审计（只报告，不改标签）")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--manifest",
                        default=str(REPO / "maaracing_master" / "plugins" / "speedrush"
                                     / "data" / "index" / "dataset_index.json"))
    parser.add_argument("--split", default=None, help="只看某个集合（train/val/test）")
    parser.add_argument("--source", default=None,
                        help="只看某个来源（如 1108）：第三人称旧帧含未标注自车，会污染漏标统计")
    parser.add_argument("--conf", type=float, default=0.5, help="漏标线索的检出阈值")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--render", default=None, help="把问题帧画到该目录")
    parser.add_argument("--max-render", type=int, default=60)
    parser.add_argument("--suggest", default=None,
                        help="把疑似漏标的框写成候选标签目录（只写新文件，不改原标签）")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    manifest = Path(args.manifest)
    if not manifest.is_file():
        print(f"manifest 不存在: {manifest}", file=sys.stderr)
        return 2
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entries = [e for e in data.get("entries", [])
               if e.get("split") != "gap"
               and (args.split is None or e.get("split") == args.split)
               and (args.source is None or e.get("source") == args.source)]
    if args.limit:
        entries = entries[:args.limit]

    report = audit(Path(args.model), entries, args.conf)
    rendered = report.pop("rendered")
    suggestions = report.pop("suggestions", {})
    report["images"] = len(entries)
    if args.render:
        report["rendered_count"] = render(
            {"rendered": rendered}, Path(args.render), args.max_render)
    if args.suggest:
        out_dir = Path(args.suggest)
        out_dir.mkdir(parents=True, exist_ok=True)
        for label_posix, lines in suggestions.items():
            (out_dir / f"{Path(label_posix).stem}.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
        report["suggested_labels"] = len(suggestions)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"审计 {len(entries)} 张：{report['counts'] or '无问题'}")
    for key, items in report["samples"].items():
        if items:
            print(f"-- {key} 示例 --")
            for item in items[:5]:
                print("  ", item)
    if args.render:
        print(f"问题帧已画到 {args.render}（{report.get('rendered_count', 0)} 张）")
    if args.suggest:
        print(f"候选标签已写到 {args.suggest}（{report.get('suggested_labels', 0)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
