# -*- coding: utf-8 -*-
"""成就引擎/门面/存档/注册发现：夹具验证 A1–A4 + 存档版本保护（A6 边界）。

契约见 core/achievement.py 模块 docstring。夹具非业务成就；不接 GUI/业务。
"""
from __future__ import annotations

import json
import types

import pytest

from maaracing_master.core import achievement as a
from maaracing_master.core import registry


@pytest.fixture(autouse=True)
def _clean():
    saved = dict(a.ACHIEVEMENT_REGISTRY)
    a.ACHIEVEMENT_REGISTRY.clear()
    a.bind_engine(None)
    yield
    a.bind_engine(None)
    a.ACHIEVEMENT_REGISTRY.clear()
    a.ACHIEVEMENT_REGISTRY.update(saved)


def _catalog(*entries):
    return {"schema_version": 1, "achievements": list(entries)}


def _entry(local_id, rule, **over):
    base = {"id": local_id, "title": f"标题{local_id}", "description": "描述",
            "category": "旅程", "rule": rule}
    base.update(over)
    return base


def _registry(*entries, mod_id="demo"):
    norm, _ = a.validate_catalog(_catalog(*entries), mod_id)
    return {mod_id: norm}


def _engine(tmp_path, registry_dict, events=None, **kw):
    return a.AchievementEngine(
        store_path=tmp_path / "ach.json",
        on_unlock=(lambda gid, en: events.append(gid)) if events is not None else None,
        registry=registry_dict, **kw)


# ---------- A1 注册发现 ----------

def test_a1_engine_lists_registered_without_module_instance(tmp_path):
    reg = _registry(_entry("first", {"fact": "demo.run", "agg": "count", "target": 1}))
    eng = _engine(tmp_path, reg)
    view = eng.get_view()
    assert [v["global_id"] for v in view] == ["demo.first"]


def test_a1_registry_loads_catalog_from_plugin_dir(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "achievements.json").write_text(json.dumps(_catalog(
        _entry("first", {"fact": "demo.run", "agg": "count", "target": 1}))), encoding="utf-8")
    manifest = types.SimpleNamespace(ACHIEVEMENTS="achievements.json")
    registry._register_achievements("demo", plugin, manifest)
    assert "demo" in a.ACHIEVEMENT_REGISTRY
    assert a.ACHIEVEMENT_REGISTRY["demo"]["achievements"][0]["global_id"] == "demo.first"


@pytest.mark.parametrize("rel", ["../evil.json", "sub/../../evil.json"])
def test_a1_path_traversal_rejected(tmp_path, rel):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (tmp_path / "evil.json").write_text(json.dumps(_catalog(
        _entry("x", {"fact": "x", "agg": "count", "target": 1}))), encoding="utf-8")
    manifest = types.SimpleNamespace(ACHIEVEMENTS=rel)
    registry._register_achievements("demo", plugin, manifest)
    assert "demo" not in a.ACHIEVEMENT_REGISTRY


def test_a1_missing_file_isolated(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    manifest = types.SimpleNamespace(ACHIEVEMENTS="nope.json")
    registry._register_achievements("demo", plugin, manifest)
    assert "demo" not in a.ACHIEVEMENT_REGISTRY


def test_a1_corrupt_catalog_does_not_raise(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "achievements.json").write_text("{bad json", encoding="utf-8")
    manifest = types.SimpleNamespace(ACHIEVEMENTS="achievements.json")
    registry._register_achievements("demo", plugin, manifest)  # 不抛
    assert "demo" not in a.ACHIEVEMENT_REGISTRY


def test_a1_no_declaration_zero_cost(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    registry._register_achievements("demo", plugin, types.SimpleNamespace())
    assert "demo" not in a.ACHIEVEMENT_REGISTRY


# ---------- A2 进度解锁 ----------

def test_a2_count_unlock_once(tmp_path):
    events = []
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 2}))
    eng = _engine(tmp_path, reg, events)
    assert eng.process_fact("k", "1", {}) is True
    assert events == []
    assert eng.process_fact("k", "2", {}) is True
    assert events == ["demo.c"]
    assert eng.process_fact("k", "3", {}) is True
    assert events == ["demo.c"]  # 不重复通知


def test_a2_count_if_predicate(tmp_path):
    reg = _registry(_entry("w", {"fact": "k", "agg": "count_if", "target": 1,
                                  "where": {"win": {"eq": True}}}))
    eng = _engine(tmp_path, reg)
    eng.process_fact("k", "1", {"win": False})
    eng.process_fact("k", "2", {"win": None})
    assert eng.get_view()[0]["unlocked"] is False
    eng.process_fact("k", "3", {"win": True})
    assert eng.get_view()[0]["unlocked"] is True


def test_a2_sum_skips_invalid(tmp_path):
    reg = _registry(_entry("s", {"fact": "k", "agg": "sum", "field": "v", "target": 100}))
    eng = _engine(tmp_path, reg)
    eng.process_fact("k", "1", {"v": 50})
    eng.process_fact("k", "2", {"v": None})
    eng.process_fact("k", "3", {"v": float("nan")})
    assert eng.get_view()[0]["progress"] == 50
    eng.process_fact("k", "4", {"v": 60})
    assert eng.get_view()[0]["unlocked"] is True


def test_a2_max_null_init_then_unlock(tmp_path):
    reg = _registry(_entry("m", {"fact": "k", "agg": "max", "field": "v", "target": 100}))
    eng = _engine(tmp_path, reg)
    assert eng.get_view()[0]["progress"] is None  # 初值 null 非 0
    eng.process_fact("k", "1", {"v": 50})
    assert eng.get_view()[0]["unlocked"] is False
    eng.process_fact("k", "2", {"v": 150})
    assert eng.get_view()[0]["progress"] == 150
    assert eng.get_view()[0]["unlocked"] is True


def test_a2_meta_evaluated_at_load(tmp_path):
    # 先只装依赖成就并解锁，落盘。
    reg_dep = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}))
    eng1 = _engine(tmp_path, reg_dep)
    eng1.process_fact("k", "1", {})
    # 再装含元成就的目录读同一存档：加载时即应触发元成就。
    events = []
    reg_full = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}),
                         _entry("meta", {"agg": "all_unlocked", "scope": "plugin"}))
    eng2 = _engine(tmp_path, reg_full, events)
    assert "demo.meta" in events
    assert {v["global_id"] for v in eng2.get_view() if v["unlocked"]} == {"demo.dep", "demo.meta"}


def test_a2_hidden_masked_until_unlocked(tmp_path):
    reg = _registry(_entry("h", {"fact": "k", "agg": "count", "target": 1}, hidden=True))
    eng = _engine(tmp_path, reg)
    v0 = eng.get_view()[0]
    assert v0["title"] == "" and v0["description"] == ""
    eng.process_fact("k", "1", {})
    v1 = eng.get_view()[0]
    assert v1["title"] == "标题h"


def test_a2_unmatched_kind_noop(tmp_path):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 1}))
    eng = _engine(tmp_path, reg)
    assert eng.process_fact("other", "1", {}) is False
    assert eng.get_view()[0]["progress"] == 0


# ---------- A3 去重 / 提交 ----------

def test_a3_duplicate_key_not_counted(tmp_path):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 5}))
    eng = _engine(tmp_path, reg)
    assert eng.process_fact("k", "same", {}) is True
    assert eng.process_fact("k", "same", {}) is False
    assert eng.get_view()[0]["progress"] == 1


def test_a3_write_failure_rolls_back_uncommitted(tmp_path, monkeypatch):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 5}))
    eng = _engine(tmp_path, reg)

    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(eng, "_save_state_locked", _boom)
    with pytest.raises(OSError):
        eng.process_fact("k", "1", {})
    assert eng.get_view()[0]["progress"] == 0  # 回滚，未提交
    assert not (tmp_path / "ach.json").exists()


def test_a3_restart_consistent_and_dedup(tmp_path):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 5}))
    eng1 = _engine(tmp_path, reg)
    eng1.process_fact("k", "1", {})
    eng2 = _engine(tmp_path, reg)
    assert eng2.get_view()[0]["progress"] == 1
    assert eng2.process_fact("k", "1", {}) is False  # 窗口内重启后仍去重
    assert eng2.get_view()[0]["progress"] == 1


def test_a3_fifo_prune_window_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "DEDUP_WINDOW", 3)
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 100}))
    eng = _engine(tmp_path, reg)
    for key in ["k1", "k2", "k3", "k4"]:  # k1 被修剪出窗口
        eng.process_fact("k", key, {})
    assert eng.get_view()[0]["progress"] == 4
    assert eng.process_fact("k", "k4", {}) is False  # 窗口内仍去重
    assert eng.process_fact("k", "k1", {}) is True   # 窗口外重报不保证去重
    assert eng.get_view()[0]["progress"] == 5


# ---------- A4 故障隔离 / 性能 ----------

def test_a4_facade_unbound_noop():
    a.bind_engine(None)
    assert a.report_fact("k", "1", {}) is False


def test_a4_facade_swallows_write_error(tmp_path, monkeypatch):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 5}))
    eng = _engine(tmp_path, reg)
    a.bind_engine(eng)

    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(eng, "_save_state_locked", _boom)
    assert a.report_fact("k", "1", {}) is False  # 不抛
    assert eng.get_view()[0]["progress"] == 0


def test_a4_notify_error_does_not_block_commit(tmp_path):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 1}))

    def _bad_unlock(gid, en):
        raise RuntimeError("notify down")

    eng = a.AchievementEngine(store_path=tmp_path / "ach.json",
                              on_unlock=_bad_unlock, registry=reg)
    assert eng.process_fact("k", "1", {}) is True  # 通知异常不影响提交
    assert eng.get_view()[0]["unlocked"] is True


def test_a4_high_frequency_burst_correct_no_deadlock(tmp_path):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 100000}))
    eng = _engine(tmp_path, reg)
    for i in range(1000):
        assert eng.process_fact("k", f"key-{i}", {}) is True
    assert eng.get_view()[0]["progress"] == 1000


# ---------- A6 存档版本保护 ----------

def test_a6_higher_version_preserved_and_frozen(tmp_path):
    path = tmp_path / "ach.json"
    original = {"store_schema_version": 999, "progress": {"demo.x": 5},
                "unlocked": {}, "consumed_keys": []}
    path.write_text(json.dumps(original), encoding="utf-8")
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 5}))
    eng = a.AchievementEngine(store_path=path, registry=reg)
    assert eng.process_fact("k", "1", {}) is False  # 停写
    assert json.loads(path.read_text(encoding="utf-8")) == original  # 原文件保留


def test_a6_corrupt_store_rebuilt_empty(tmp_path):
    path = tmp_path / "ach.json"
    path.write_text("{not json", encoding="utf-8")
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 5}))
    eng = a.AchievementEngine(store_path=path, registry=reg)
    assert eng.get_view()[0]["progress"] == 0
    assert eng.process_fact("k", "1", {}) is True