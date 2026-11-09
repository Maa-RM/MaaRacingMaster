#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""感知模型统一评测：每类 AP / 近场召回 / 误检率 / 真实调用延迟。

**为什么要有这个脚本**：训练日志里的 mAP 与生产不在同一条链路上——训练尺寸、
验证几何、NMS 参数都不同，不能拿去当验收。这里走**生产同一个检测器**（预处理、
letterbox、类别映射、NMS 全部一致），只换数据集，量出来的才是部署口径。

**指标**
- ``AP50`` / ``AP50-95``：按类 COCO 式 101 点插值，匹配 IoU 0.50:0.95；
- ``近场召回``：标签框高 ≥ ``--near-h``（归一化）的子集，在生产 conf 下的召回；
- ``误检/千帧``：生产 conf 以上、IoU 0.5 匹配不到 GT 的框数；
- ``延迟``：完整 detect 调用（预处理 + run + 解码/NMS）与 run 本体，p50/p95。

**数据来源**：``--images/--labels`` 直接给目录；或 ``--manifest``（``build_dataset_index.py``
的产物）+ ``--split``。两者都不动文件，只读。

用法：
    python maaracing_master/plugins/speedrush/tools/training/eval_detector.py \\
        --images <图目录> --labels <标签目录>（或直接用缺省 --manifest）
    python ... --split test --source 1108（按来源过滤）
    python ... --manifest ... --split test --json-out build/eval/test_baseline.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from maaracing_master.core.yolo_detector import YOLODetector  # noqa: E402

DEFAULT_MODEL = (REPO / "maaracing_master" / "plugins" / "speedrush"
                 / "resources" / "models" / "perception" / "model.onnx")
IOU_THRESHOLDS = tuple(round(0.5 + 0.05 * i, 2) for i in range(10))


# ---------- 标注与匹配 ----------

def load_labels(path: Path, width: int, height: int) -> list[tuple[int, tuple]]:
    """YOLO 标注（归一化 cx cy w h）→ [(类别id, (x1,y1,x2,y2))] 像素框。"""
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        cls, cx, cy, bw, bh = int(parts[0]), *map(float, parts[1:])
        out.append((cls, ((cx - bw / 2) * width, (cy - bh / 2) * height,
                          (cx + bw / 2) * width, (cy + bh / 2) * height)))
    return out


def iou(a: tuple, b: tuple) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def match_flags(dets: list[dict], gts: list[tuple], thr: float) -> list[bool]:
    """按置信度降序贪心匹配；返回与 ``dets`` 对应的 tp 标记。"""
    order = sorted(range(len(dets)), key=lambda i: -dets[i]["confidence"])
    used = [False] * len(gts)
    flags = [False] * len(dets)
    for i in order:
        best, best_j = 0.0, -1
        for j, gt in enumerate(gts):
            if used[j]:
                continue
            value = iou(dets[i]["box"], gt)
            if value > best:
                best, best_j = value, j
        if best_j >= 0 and best >= thr:
            used[best_j] = True
            flags[i] = True
    return flags


def average_precision(records: list[tuple[float, bool]], n_gt: int) -> float:
    """COCO 式 101 点插值 AP。``records`` = [(score, is_tp)]。"""
    if n_gt == 0:
        return float("nan")
    if not records:
        return 0.0
    records = sorted(records, key=lambda item: -item[0])
    tp_cum = np.cumsum([1.0 if tp else 0.0 for _s, tp in records])
    fp_cum = np.cumsum([0.0 if tp else 1.0 for _s, tp in records])
    recall = tp_cum / n_gt
    precision = tp_cum / np.maximum(tp_cum + fp_cum, 1e-9)
    grid = np.linspace(0.0, 1.0, 101)
    interp = []
    for r in grid:
        mask = recall >= r
        interp.append(float(precision[mask].max()) if mask.any() else 0.0)
    return float(np.mean(interp))


# ---------- 主流程 ----------

def collect_pairs(images_dir: Path, labels_dir: Path,
                  manifest: Path | None, split: str | None,
                  limit: int | None,
                  source: str | None = None) -> list[tuple[Path, Path]]:
    if manifest is not None:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        pairs = []
        for entry in data.get("entries", []):
            if split and entry.get("split") != split:
                continue
            if source and entry.get("source") != source:
                continue
            image = Path(entry["path"])
            label = Path(entry["label"])
            pairs.append((image, label))
        pairs.sort()
    else:
        pairs = []
        for image in sorted(images_dir.glob("*")):
            if image.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            pairs.append((image, labels_dir / f"{image.stem}.txt"))
    return pairs[:limit] if limit else pairs


def evaluate(model: Path, pairs: list[tuple[Path, Path]], conf: float,
             nms_iou: float, near_h: float) -> dict:
    """走生产检测路径逐帧评测；det = {"box", "confidence", "class_name"}。"""
    detector = YOLODetector(str(model), conf=0.01, iou=nms_iou)
    name_to_id = {name: cid for cid, name in detector.classes.items()}
    ids = sorted(name_to_id.values())

    ap_records = {cid: {thr: [] for thr in IOU_THRESHOLDS} for cid in ids}
    gt_counts = {cid: 0 for cid in ids}
    near_gt = {cid: 0 for cid in ids}
    near_hit = {cid: 0 for cid in ids}
    det_counts = {cid: 0 for cid in ids}
    fp_at_conf = 0
    call_ms, run_ms = [], []
    skipped = 0

    for image_path, label_path in pairs:
        bgr = cv2.imread(str(image_path))
        if bgr is None:
            skipped += 1
            continue
        height, width = bgr.shape[:2]
        gts = load_labels(label_path, width, height)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        started = time.perf_counter()
        _by_class, dets, _raw = detector(rgb)
        call_ms.append((time.perf_counter() - started) * 1000.0)
        run_ms.append(detector.last_run_ms)

        by_cid: dict[int, list[dict]] = {cid: [] for cid in ids}
        for det in dets:
            by_cid[name_to_id[det["class_name"]]].append(det)

        for cid in ids:
            gt_boxes = [box for c, box in gts if c == cid]
            near_boxes = [box for box in gt_boxes
                          if (box[3] - box[1]) / height >= near_h]
            gt_counts[cid] += len(gt_boxes)
            near_gt[cid] += len(near_boxes)

            class_dets = by_cid[cid]
            for thr in IOU_THRESHOLDS:
                flags = match_flags(class_dets, gt_boxes, thr)
                ap_records[cid][thr].extend(
                    (det["confidence"], hit) for det, hit in zip(class_dets, flags))

            prod_dets = [d for d in class_dets if d["confidence"] >= conf]
            det_counts[cid] += len(prod_dets)
            prod_flags = match_flags(prod_dets, gt_boxes, 0.5)
            fp_at_conf += sum(1 for hit in prod_flags if not hit)
            near_flags = match_flags(prod_dets, near_boxes, 0.5)
            near_hit[cid] += sum(1 for hit in near_flags if hit)

    ap50 = {cid: average_precision(ap_records[cid][0.5], gt_counts[cid]) for cid in ids}
    ap5095 = {}
    for cid in ids:
        values = [average_precision(ap_records[cid][thr], gt_counts[cid])
                  for thr in IOU_THRESHOLDS]
        values = [v for v in values if not np.isnan(v)]
        ap5095[cid] = float(np.mean(values)) if values else float("nan")

    call_ms.sort()
    run_ms.sort()
    frames = max(1, len(pairs) - skipped)
    return {
        "frames": len(pairs) - skipped,
        "skipped": skipped,
        "conf": conf,
        "near_h": near_h,
        "classes": detector.classes,
        "gt_counts": {detector.classes[c]: n for c, n in gt_counts.items()},
        "det_counts_at_conf": {detector.classes[c]: n for c, n in det_counts.items()},
        "ap50": {detector.classes[c]: round(v, 4) for c, v in ap50.items()},
        "ap50_95": {detector.classes[c]: round(v, 4) for c, v in ap5095.items()},
        "near_recall": {
            detector.classes[c]: round(near_hit[c] / near_gt[c], 4) if near_gt[c] else None
            for c in ids},
        "near_gt_counts": {detector.classes[c]: n for c, n in near_gt.items()},
        "fp_per_1k_frames": round(fp_at_conf / frames * 1000.0, 2),
        "latency_ms": {
            "provider": detector.provider,
            "call_p50": round(statistics.median(call_ms), 2),
            "call_p95": round(call_ms[max(0, int(len(call_ms) * 0.95) - 1)], 2),
            "run_p50": round(statistics.median(run_ms), 2),
            "run_p95": round(run_ms[max(0, int(len(run_ms) * 0.95) - 1)], 2),
        },
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="感知模型统一评测")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--images", default=None)
    parser.add_argument("--labels", default=None)
    parser.add_argument("--manifest",
                        default=str(REPO / "maaracing_master" / "plugins" / "speedrush"
                                     / "data" / "index" / "dataset_index.json"),
                        help="数据集索引 JSON（优先于 --images/--labels）")
    parser.add_argument("--split", default=None, help="配合 --manifest：train/val/test")
    parser.add_argument("--conf", type=float, default=0.35, help="生产置信度阈值")
    parser.add_argument("--nms-iou", type=float, default=0.45)
    parser.add_argument("--near-h", type=float, default=0.18, help="近场判定：标签框高占比")
    parser.add_argument("--source", default=None,
                        help="配合 --manifest：只看某个来源（如 1108）")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    model = Path(args.model)
    if not model.is_file():
        print(f"模型不存在: {model}", file=sys.stderr)
        return 2
    # 显式给 --images 时优先用目录；否则用索引（缺省指向插件 data/index）
    manifest = Path(args.manifest) if args.manifest and args.images is None else None
    if args.images is None and (manifest is None or not manifest.is_file()):
        print(f"索引不存在且未给 --images: {args.manifest}", file=sys.stderr)
        return 2
    pairs = collect_pairs(Path(args.images) if args.images else Path("."),
                          Path(args.labels) if args.labels else Path("."),
                          manifest, args.split, args.limit, args.source)
    if not pairs:
        print("没有可评测的图片/标注对", file=sys.stderr)
        return 2

    report = evaluate(model, pairs, args.conf, args.nms_iou, args.near_h)
    report["model"] = str(model)
    report["source"] = args.manifest or args.images

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"帧数 {report['frames']}（跳过 {report['skipped']}）  模型 {model.name}")
    print(f"{'类别':<10}{'GT':>6}{'检出':>6}{'AP50':>8}{'AP50-95':>9}"
          f"{'近场GT':>7}{'近场召回':>9}")
    for name in report["gt_counts"]:
        near = report["near_recall"][name]
        print(f"{name:<10}{report['gt_counts'][name]:>6}"
              f"{report['det_counts_at_conf'][name]:>6}"
              f"{report['ap50'][name]:>8.3f}{report['ap50_95'][name]:>9.3f}"
              f"{report['near_gt_counts'][name]:>7}"
              f"{('%.3f' % near) if near is not None else '-':>9}")
    print(f"误检/千帧 @conf={report['conf']}: {report['fp_per_1k_frames']}")
    lat = report["latency_ms"]
    if lat:
        print(f"延迟 provider={lat['provider']} call p50/p95="
              f"{lat['call_p50']}/{lat['call_p95']} ms  "
              f"run p50/p95={lat['run_p50']}/{lat['run_p95']} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
