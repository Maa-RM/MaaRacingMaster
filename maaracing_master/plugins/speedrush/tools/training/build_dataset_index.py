#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据集索引与会话级划分：读**标注目录**，产出可复现的 train/val/test 清单。

**数据布局**（都在插件内、不入库、不进发布包，见 `.gitignore` 与
`scripts/release/assemble.ps1`）::

    plugins/speedrush/data/
      annotations/   <主干>.jpg + <主干>.txt 平铺（标注工具工作区，唯一图片真源）
      index/         dataset_index.json / exclude.txt / 划分统计（本脚本产物，不复制图片）

**分组规则**
- 纯数字主干（旧集 1000…1784）：按采集先后切三段，块间留 ``--gap`` 帧空隙；
- 主干含 ``__``（``<会话>__<序号>.jpg``）：以会话为组，整场只进一个集合；
- ``exclude.txt`` 列出的主干：保留在标注目录，但不进任何集合。

**泄漏自检**：对所有图片算 64 位 dHash，统计跨集合、汉明距离 ≤ ``--leak-hamming``
的图片对。只报告，不改划分。

用法：
    python maaracing_master/plugins/speedrush/tools/training/build_dataset_index.py
    python ... --annotations <目录> --exclude <文件> --out <json>
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO))

import cv2  # noqa: E402

PLUGIN_DIR = REPO / "maaracing_master" / "plugins" / "speedrush"
DEFAULT_ANNOTATIONS = PLUGIN_DIR / "data" / "annotations"
DEFAULT_EXCLUDE = PLUGIN_DIR / "data" / "index" / "exclude.txt"
DEFAULT_OUT = PLUGIN_DIR / "data" / "index" / "dataset_index.json"

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


# ---------- 采集 ----------

def read_exclude(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    stems = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            stems.add(Path(text).stem)
    return stems


def _read_boxes(label: Path) -> list[tuple[int, float]]:
    """返回 [(类别id, 框高归一化)]；读不到就是空表（空标注是合法样本）。"""
    out: list[tuple[int, float]] = []
    if not label.is_file():
        return out
    for line in label.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 5:
            out.append((int(parts[0]), float(parts[4])))
    return out


def group_of(stem: str) -> str:
    if stem.isdigit():
        return "legacy"
    if "__" in stem:
        return stem.split("__", 1)[0]
    return "misc"


def collect(annotations: Path, excluded: set[str]) -> list[dict]:
    entries: list[dict] = []
    for image in sorted(annotations.glob("*")):
        if image.suffix.lower() not in IMAGE_SUFFIXES or image.stem in excluded:
            continue
        bgr = cv2.imread(str(image))
        if bgr is None:
            continue
        height, width = bgr.shape[:2]
        label = image.with_suffix(".txt")
        entries.append({
            "path": str(image.resolve()),
            "label": str(label.resolve()),
            "group": group_of(image.stem),
            "image_size": [width, height],
            "boxes": _read_boxes(label),
        })
    return entries


# ---------- 划分 ----------

def split_legacy(entries: list[dict], ratios: tuple[float, float, float],
                 gap: int) -> int:
    """旧集按采集先后切三段，段间留 gap 帧（这些帧不进任何集合）。返回 gap 帧数。"""
    ordered = sorted(entries, key=lambda e: int(Path(e["path"]).stem))
    total = len(ordered)
    n_train = int(total * ratios[0])
    n_val = int(total * ratios[1])
    val_start = n_train + gap
    test_start = val_start + n_val + gap
    for index, entry in enumerate(ordered):
        if index < n_train:
            entry["split"] = "train"
        elif val_start <= index < val_start + n_val:
            entry["split"] = "val"
        elif index >= test_start:
            entry["split"] = "test"
        else:
            entry["split"] = "gap"
    return sum(1 for e in ordered if e["split"] == "gap")


def split_sessions(entries: list[dict], ratios: tuple[float, float, float],
                   seed: int) -> None:
    """新批次：整场会话归一个集合，按图片数配平 val/test 配额。"""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        buckets[entry["group"]].append(entry)
    sessions = sorted(buckets)
    random.Random(seed).shuffle(sessions)
    total = len(entries)
    target_val = total * ratios[1]
    target_test = total * ratios[2]
    assigned = Counter()
    for session in sessions:
        if assigned["val"] < target_val:
            split = "val"
        elif assigned["test"] < target_test:
            split = "test"
        else:
            split = "train"
        for entry in buckets[session]:
            entry["split"] = split
        assigned[split] += len(buckets[session])


# ---------- 泄漏自检 ----------

def dhash(path: str, size: int = 8) -> int | None:
    gray = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).reshape(-1)
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def leak_report(entries: list[dict], hamming: int, max_examples: int = 5) -> dict:
    hashes = []
    for entry in entries:
        value = dhash(entry["path"])
        if value is not None:
            hashes.append((value, entry))
    pairs = 0
    examples = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            a, entry_a = hashes[i]
            b, entry_b = hashes[j]
            if entry_a["split"] == entry_b["split"]:
                continue
            distance = (a ^ b).bit_count()
            if distance <= hamming:
                pairs += 1
                if len(examples) < max_examples:
                    examples.append({
                        "a": entry_a["path"], "a_split": entry_a["split"],
                        "b": entry_b["path"], "b_split": entry_b["split"],
                        "hamming": distance,
                    })
    return {"cross_split_pairs": pairs, "hamming_threshold": hamming,
            "examples": examples}


# ---------- 统计 ----------

def summarize(entries: list[dict], near_h: float) -> dict:
    stats: dict[str, dict] = {}
    for split in ("train", "val", "test", "gap"):
        subset = [e for e in entries if e["split"] == split]
        if not subset:
            continue
        class_counts: Counter = Counter()
        near_counts: Counter = Counter()
        height_ratios = []
        sizes: Counter = Counter()
        groups: Counter = Counter()
        for entry in subset:
            groups[entry["group"]] += 1
            sizes[tuple(entry["image_size"])] += 1
            for cid, h_ratio in entry["boxes"]:
                class_counts[cid] += 1
                height_ratios.append(h_ratio)
                if h_ratio >= near_h:
                    near_counts[cid] += 1
        height_ratios.sort()
        stats[split] = {
            "images": len(subset),
            "boxes_by_class": dict(sorted(class_counts.items())),
            "near_boxes_by_class": dict(sorted(near_counts.items())),
            "near_share": (round(len([h for h in height_ratios if h >= near_h])
                                 / len(height_ratios), 4) if height_ratios else None),
            "box_h_p50": (round(height_ratios[len(height_ratios) // 2], 4)
                          if height_ratios else None),
            "resolutions": {f"{w}x{h}": n for (w, h), n in sizes.most_common()},
            "groups": dict(groups),
        }
    return stats


# ---------- 主流程 ----------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="标注目录 → 会话级训练索引")
    parser.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))
    parser.add_argument("--exclude", default=str(DEFAULT_EXCLUDE),
                        help="排除清单（每行一个主干；文件不存在时不过滤）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--test-ratio", type=float, default=0.15)
    parser.add_argument("--gap", type=int, default=30, help="旧集时间块之间的空隙帧数")
    parser.add_argument("--near-h", type=float, default=0.18)
    parser.add_argument("--leak-hamming", type=int, default=6)
    args = parser.parse_args()

    annotations = Path(args.annotations)
    if not annotations.is_dir():
        print(f"标注目录不存在: {annotations}", file=sys.stderr)
        return 2
    excluded = read_exclude(Path(args.exclude))
    entries = collect(annotations, excluded)
    if not entries:
        print("没有可用图片", file=sys.stderr)
        return 2

    ratios = (1.0 - args.val_ratio - args.test_ratio, args.val_ratio, args.test_ratio)
    legacy = [e for e in entries if e["group"] == "legacy"]
    sessions = [e for e in entries if e["group"] not in ("legacy", "misc")]
    misc = [e for e in entries if e["group"] == "misc"]
    gaps = split_legacy(legacy, ratios, args.gap) if legacy else 0
    if sessions:
        split_sessions(sessions, ratios, args.seed)
    for entry in misc:   # 无会话标识的散图：只进训练，不参与 val/test
        entry["split"] = "train"

    leaks = leak_report(entries, args.leak_hamming)
    stats = summarize(entries, args.near_h)
    payload = {
        "seed": args.seed,
        "ratios": {"train": ratios[0], "val": ratios[1], "test": ratios[2]},
        "near_h": args.near_h,
        "annotations": str(annotations.resolve()),
        "excluded": sorted(excluded, key=lambda s: (not s.isdigit(), s)),
        "gap_frames": gaps,
        "misc_images": len(misc),
        "leaks": leaks,
        "stats": stats,
        "entries": entries,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"索引已写入 {out}（{len(entries)} 张，排除 {len(excluded)}，散图 {len(misc)}）")
    for split, item in stats.items():
        print(f"  {split:<6} 图片={item['images']:<5} 框={item['boxes_by_class']} "
              f"近场占比={item['near_share']}")
    print(f"  跨集合近重复对（dHash≤{args.leak_hamming}）：{leaks['cross_split_pairs']}")
    for example in leaks["examples"]:
        print("   ", example["a_split"], "|", example["b_split"],
              "hamming", example["hamming"], Path(example["a"]).name,
              "~", Path(example["b"]).name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
