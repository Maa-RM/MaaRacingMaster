# -*- coding: utf-8 -*-
"""把数据集索引（``build_dataset_index.py`` 的产物）转成 Ultralytics 可消费的划分。

Ultralytics 的数据配置可以直接指向**图片路径清单**（每行一个绝对路径），不必把
文件复制/软链到 train/val 目录。清单里带绝对路径，故 yaml 里不依赖 ``path``。

本模块不 import ultralytics：训练机装没装都能生成清单，也便于单元测试。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def write_split_lists(manifest: Path, out_dir: Path,
                      splits: tuple[str, ...] = ("train", "val")) -> dict[str, Path]:
    """按 manifest 的 split 写图片路径清单；返回 {split: txt 路径}。"""
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in data.get("entries", []):
        split = entry.get("split")
        if split in splits:
            grouped[split].append(entry["path"])
    if not grouped:
        raise ValueError(f"manifest 里没有 {splits} 的条目: {manifest}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for split in splits:
        paths = sorted(grouped.get(split, []))
        if not paths:
            raise ValueError(f"split {split} 为空: {manifest}")
        target = out_dir / f"{split}.txt"
        target.write_text("\n".join(paths) + "\n", encoding="utf-8")
        written[split] = target
    return written


def write_data_yaml(names: dict[int, str] | list[str],
                    lists: dict[str, Path], out_path: Path) -> Path:
    """写 Ultralytics 数据配置（train/val 指向路径清单）。"""
    if isinstance(names, dict):
        items = sorted(names.items())
    else:
        items = list(enumerate(names))
    lines = ["# 由 build_dataset_index.py 的划分生成；路径清单为绝对路径，无需 path 字段"]
    for split in ("train", "val", "test"):
        if split in lists:
            lines.append(f"{split}: {Path(lists[split]).resolve().as_posix()}")
    lines.append("names:")
    for index, name in items:
        lines.append(f"  {index}: {name}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
