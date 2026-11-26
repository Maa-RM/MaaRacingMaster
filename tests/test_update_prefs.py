# -*- coding: utf-8 -*-
"""宿主更新偏好只读镜像：模型下载的首选源必须跟同一个参数走。"""

from __future__ import annotations

import json
from pathlib import Path

from maaracing_master.core import update_prefs


def _root(monkeypatch, tmp_path, payload: str | None) -> Path:
    root = tmp_path / "MaaRacingMaster"
    (root / "config").mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (root / "config" / update_prefs.PREFS_NAME).write_text(payload, encoding="utf-8")
    monkeypatch.setattr(update_prefs, "user_data_dir", lambda: root)
    return root


def test_missing_file_defaults_to_cnb(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path, None)
    assert update_prefs.preferred_source() == "cnb"


def test_explicit_github_is_honoured(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path, json.dumps({"channel": "stable", "source": "github"}))
    assert update_prefs.preferred_source() == "github"


def test_illegal_or_broken_values_fall_back_like_host(monkeypatch, tmp_path):
    """宿主 UpdatePreferences.Load 对非法值一律回默认，这里同口径。"""
    _root(monkeypatch, tmp_path, json.dumps({"source": "mirror"}))
    assert update_prefs.preferred_source() == "cnb"
    _root(monkeypatch, tmp_path, "{ not json")
    assert update_prefs.preferred_source() == "cnb"
    _root(monkeypatch, tmp_path, json.dumps(["cnb"]))
    assert update_prefs.preferred_source() == "cnb"
