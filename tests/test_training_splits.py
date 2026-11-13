# -*- coding: utf-8 -*-
"""训练划分清单生成的回归锁：gap 不得进任何集合、清单按路径排序、yaml 可被消费。"""

from __future__ import annotations

import json

from maaracing_master.plugins.speedrush.tools.training.splits import (
    write_data_yaml,
    write_split_lists,
)


def _manifest(tmp_path):
    entries = [
        {"path": "b.jpg", "label": "b.txt", "split": "train"},
        {"path": "a.jpg", "label": "a.txt", "split": "train"},
        {"path": "v.jpg", "label": "v.txt", "split": "val"},
        {"path": "t.jpg", "label": "t.txt", "split": "test"},
        {"path": "g.jpg", "label": "g.txt", "split": "gap"},
    ]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_split_lists_exclude_gap_and_sort(tmp_path):
    lists = write_split_lists(_manifest(tmp_path), tmp_path / "splits")
    assert lists["train"].read_text(encoding="utf-8").split("\n")[:2] == ["a.jpg", "b.jpg"]
    assert lists["val"].read_text(encoding="utf-8").strip() == "v.jpg"
    assert not (tmp_path / "splits" / "test.txt").exists()


def test_data_yaml_points_to_lists(tmp_path):
    lists = write_split_lists(_manifest(tmp_path), tmp_path / "splits")
    out = write_data_yaml({0: "coin", 1: "car"}, lists, tmp_path / "data.yaml")
    text = out.read_text(encoding="utf-8")
    assert f"train: {lists['train'].resolve().as_posix()}" in text
    assert f"val: {lists['val'].resolve().as_posix()}" in text
    assert "  1: car" in text
