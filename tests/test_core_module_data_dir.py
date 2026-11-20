# -*- coding: utf-8 -*-
"""模块专属数据目录 helper 的 containment 回归锁。"""

from __future__ import annotations

import pytest

from maaracing_master.core import paths


def test_module_data_dir_builds_module_scoped_path(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    path = paths.module_data_dir("infinite_drive", "models", "abc123")
    assert path == tmp_path / "infinite_drive" / "models" / "abc123"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "C:\\tmp", ".", "", ".."])
def test_module_data_dir_rejects_bad_module_id(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        paths.module_data_dir(bad, "models")


@pytest.mark.parametrize("bad", ["..", "a/b", "C:\\tmp", ".", ""])
def test_module_data_dir_rejects_bad_subpath(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    with pytest.raises(ValueError):
        paths.module_data_dir("infinite_drive", bad)
