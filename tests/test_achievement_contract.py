# -*- coding: utf-8 -*-
"""成就 catalog 校验器：纯函数测试（照 test_dashboard_contract 口径，不依赖运行时）。

契约见 core/achievement.py 模块 docstring。机制预埋，夹具非业务成就。
"""
from __future__ import annotations

import pytest

from maaracing_master.core import achievement as a


def _catalog(*entries, version=a.ACHIEVEMENT_CATALOG_SCHEMA_VERSION):
    return {"schema_version": version, "achievements": list(entries)}


def _entry(local_id="first", **over):
    base = {"id": local_id, "title": "标题", "description": "描述",
            "category": "旅程", "rule": {"fact": "demo.run", "agg": "count", "target": 1}}
    base.update(over)
    return base


def test_valid_catalog_normalized_with_global_id():
    norm, warnings = a.validate_catalog(_catalog(_entry()), "demo")
    assert warnings == ()
    assert norm["mod_id"] == "demo"
    assert norm["achievements"][0]["global_id"] == "demo.first"


def test_unsupported_schema_version_raises():
    with pytest.raises(a.AchievementContractError):
        a.validate_catalog(_catalog(_entry(), version=999), "demo")


def test_mod_id_with_dot_raises():
    with pytest.raises(a.AchievementContractError):
        a.validate_catalog(_catalog(_entry()), "bad.id")


def test_top_level_not_object_raises():
    with pytest.raises(a.AchievementContractError):
        a.validate_catalog([], "demo")


def test_illegal_agg_dropped_with_warning():
    norm, warnings = a.validate_catalog(
        _catalog(_entry(rule={"fact": "x", "agg": "script", "target": 1}),
                 _entry("ok")), "demo")
    assert [e["id"] for e in norm["achievements"]] == ["ok"]
    assert any("agg" in w for w in warnings)


@pytest.mark.parametrize("target", [0, -1, float("nan"), float("inf"), "1", None])
def test_invalid_target_dropped(target):
    norm, warnings = a.validate_catalog(
        _catalog(_entry(rule={"fact": "x", "agg": "count", "target": target})), "demo")
    assert norm["achievements"] == []
    assert any("target" in w for w in warnings)


@pytest.mark.parametrize("local_id", ["Bad", "with-dash", "has.dot", "", "空格"])
def test_invalid_local_id_dropped(local_id):
    norm, _ = a.validate_catalog(_catalog(_entry(local_id)), "demo")
    assert norm["achievements"] == []


def test_duplicate_id_dropped():
    norm, warnings = a.validate_catalog(_catalog(_entry("dup"), _entry("dup")), "demo")
    assert len(norm["achievements"]) == 1
    assert any("重复" in w for w in warnings)


def test_sum_requires_field():
    norm, _ = a.validate_catalog(
        _catalog(_entry(rule={"fact": "x", "agg": "sum", "target": 10})), "demo")
    assert norm["achievements"] == []


def test_max_valid():
    norm, warnings = a.validate_catalog(
        _catalog(_entry(rule={"fact": "x", "agg": "max", "field": "score", "target": 100})),
        "demo")
    assert warnings == ()
    assert norm["achievements"][0]["rule"]["field"] == "score"


def test_count_if_where_and_semantics():
    rule = {"fact": "x", "agg": "count_if", "target": 2,
            "where": {"win": {"eq": True}}}
    norm, warnings = a.validate_catalog(_catalog(_entry(rule=rule)), "demo")
    assert warnings == ()
    assert norm["achievements"][0]["rule"]["where"] == {"win": {"eq": True}}


def test_count_if_empty_where_dropped():
    rule = {"fact": "x", "agg": "count_if", "target": 2, "where": {}}
    norm, _ = a.validate_catalog(_catalog(_entry(rule=rule)), "demo")
    assert norm["achievements"] == []


def test_interval_condition_nan_rejected():
    rule = {"fact": "x", "agg": "count_if", "target": 1,
            "where": {"score": {"gte": float("nan")}}}
    norm, warnings = a.validate_catalog(_catalog(_entry(rule=rule)), "demo")
    assert norm["achievements"] == []
    assert any("有限数值" in w for w in warnings)


def test_all_unlocked_empty_set_rejected():
    norm, warnings = a.validate_catalog(
        _catalog(_entry("meta", rule={"agg": "all_unlocked", "scope": "plugin"})), "demo")
    assert norm["achievements"] == []
    assert any("空集合" in w for w in warnings)


def test_all_unlocked_with_dependency_kept():
    norm, warnings = a.validate_catalog(
        _catalog(_entry("dep"),
                 _entry("meta", rule={"agg": "all_unlocked", "scope": "plugin"})), "demo")
    assert {e["id"] for e in norm["achievements"]} == {"dep", "meta"}
    assert warnings == ()


def test_all_unlocked_bad_scope_dropped():
    norm, _ = a.validate_catalog(
        _catalog(_entry("dep"),
                 _entry("meta", rule={"agg": "all_unlocked", "scope": "global"})), "demo")
    assert [e["id"] for e in norm["achievements"]] == ["dep"]


def test_hidden_defaults_false():
    norm, _ = a.validate_catalog(_catalog(_entry()), "demo")
    assert norm["achievements"][0]["hidden"] is False


def test_register_catalog_populates_index_and_isolates_fatal():
    saved = dict(a.ACHIEVEMENT_REGISTRY)
    try:
        a.ACHIEVEMENT_REGISTRY.clear()
        norm, _ = a.register_catalog("demo", _catalog(_entry()))
        assert norm is not None
        assert "demo" in a.ACHIEVEMENT_REGISTRY
        bad, reasons = a.register_catalog("demo2", _catalog(_entry(), version=999))
        assert bad is None and reasons
        assert "demo2" not in a.ACHIEVEMENT_REGISTRY
    finally:
        a.ACHIEVEMENT_REGISTRY.clear()
        a.ACHIEVEMENT_REGISTRY.update(saved)