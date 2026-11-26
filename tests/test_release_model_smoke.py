# -*- coding: utf-8 -*-
"""发布后双源冒烟的判据回归锁：不联网，只验它怎么判。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release" / "smoke_model_sources.py"


def _load():
    spec = importlib.util.spec_from_file_location("smoke_model_sources_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _ok(size=100):
    return {"status": 206, "bytes": size, "hops": [], "error": ""}


def test_sources_prefer_cnb_then_github():
    """首选顺序与下载描述同口径：cnb 在前，github 兜底。"""
    module = _load()
    sources = module.build_sources("v1.2.3", "owner/repo", "org/repo")
    assert list(sources) == ["cnb", "github"]
    assert sources["cnb"] == (
        "https://cnb.cool/org/repo/-/releases/download/v1.2.3/"
        "MaaRacingMaster-infinite_drive-models-1.2.3.zip")


def test_both_sources_ok_passes():
    module = _load()
    assert module.judge({"cnb": _ok(), "github": _ok()}) == []


def test_hop_outside_allowlist_fails_with_reason():
    """货架把下载交给白名单外的 CDN 时必须红——这正是线上事故的形态。"""
    module = _load()
    row = {"status": None, "bytes": None, "hops": [],
           "error": "跳转不在白名单: https://asset.newcdn.example/x"}
    problems = module.judge({"cnb": row, "github": _ok()})
    assert any("跳转不在白名单" in p for p in problems)


def test_missing_asset_and_size_disagreement_fail():
    module = _load()
    missing = {"status": 404, "bytes": None, "hops": [], "error": "HTTP 404"}
    problems = module.judge({"cnb": _ok(100), "github": missing})
    assert any("资产不存在" in p for p in problems)
    problems = module.judge({"cnb": _ok(100), "github": _ok(99)})
    assert any("字节数不一致" in p for p in problems)


def test_expect_size_is_enforced():
    module = _load()
    problems = module.judge({"cnb": _ok(100), "github": _ok(100)}, expect_size=101)
    assert any("!= 本地 101" in p for p in problems)
