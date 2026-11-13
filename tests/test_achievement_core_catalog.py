# -*- coding: utf-8 -*-
"""core 自带成就目录：注册、保留命名空间、展示字段（tier / hint）与视图遮蔽。

契约见 core/achievement.py 模块 docstring；这里只锁 core 目录与它引入的两个展示字段。
UI 事实侧的锁（发射点 ↔ 目录、前端字面量）住 `test_sidecar_achievement.py`——本文件
**不导入 sidecar**，否则会在收集期把 vgamepad 桩模块带进 sys.modules，打乱
`test_capabilities_gamepad.py` 的隔离前提。
"""
from __future__ import annotations

import json
import types

import pytest

from maaracing_master.core import achievement as a
from maaracing_master.core import registry as reg


@pytest.fixture()
def isolated_registry():
    """隔离全局声明索引：测试期间可自由注册，结束后原样恢复。"""
    saved = dict(a.ACHIEVEMENT_REGISTRY)
    a.ACHIEVEMENT_REGISTRY.clear()
    try:
        yield a.ACHIEVEMENT_REGISTRY
    finally:
        a.ACHIEVEMENT_REGISTRY.clear()
        a.ACHIEVEMENT_REGISTRY.update(saved)


def test_core_catalog_registers_from_package_resources(isolated_registry):
    assert a.register_core_catalog() is True
    entries = isolated_registry[a.CORE_MOD_ID]["achievements"]
    assert [e["global_id"] for e in entries] == [
        "core.first_start", "core.cancel_countdown",
        "core.start_twice", "core.cancel_twice",
    ]
    for entry in entries:
        assert entry["title"] and entry["description"] and entry["hint"]
        assert entry["rule"]["agg"] == "count"
        assert entry["rule"]["target"] == 1
    # 四条事实互不相同：共用一条 fact 会让一次点击解锁两条
    facts = [e["rule"]["fact"] for e in entries]
    assert len(set(facts)) == len(facts)
    assert all(fact.startswith("core.ui.") for fact in facts)


def test_core_catalog_hidden_entry(isolated_registry):
    a.register_core_catalog()
    entries = {e["global_id"]: e for e in isolated_registry[a.CORE_MOD_ID]["achievements"]}
    assert entries["core.cancel_twice"]["hidden"] is True
    assert all(not e["hidden"] for gid, e in entries.items() if gid != "core.cancel_twice")


def test_missing_core_catalog_is_isolated(tmp_path, isolated_registry):
    assert a.register_core_catalog(tmp_path / "nope.json") is False
    assert a.CORE_MOD_ID not in isolated_registry


def test_broken_core_catalog_is_isolated(tmp_path, isolated_registry):
    bad = tmp_path / "achievements.json"
    bad.write_text("{not json", encoding="utf-8")
    assert a.register_core_catalog(bad) is False
    assert a.CORE_MOD_ID not in isolated_registry


def test_wrong_schema_core_catalog_is_isolated(tmp_path, isolated_registry):
    bad = tmp_path / "achievements.json"
    bad.write_text(json.dumps({"schema_version": 999, "achievements": []}), encoding="utf-8")
    assert a.register_core_catalog(bad) is False
    assert a.CORE_MOD_ID not in isolated_registry


def _catalog(*entries):
    return {"schema_version": a.ACHIEVEMENT_CATALOG_SCHEMA_VERSION, "achievements": list(entries)}


def _entry(local_id="first", **over):
    base = {"id": local_id, "title": "标题", "description": "描述",
            "rule": {"fact": "demo.run", "agg": "count", "target": 1}}
    base.update(over)
    return base


def test_tier_defaults_to_normal():
    norm, warnings = a.validate_catalog(_catalog(_entry()), "demo")
    assert warnings == ()
    assert norm["achievements"][0]["tier"] == "normal"
    assert norm["achievements"][0]["hint"] == ""


@pytest.mark.parametrize("tier", ["legendary", "", None, 3])
def test_illegal_tier_degrades_to_normal(tier):
    norm, warnings = a.validate_catalog(_catalog(_entry(tier=tier)), "demo")
    assert [e["tier"] for e in norm["achievements"]] == ["normal"]
    assert any("tier" in w for w in warnings)


def test_hint_truncated_and_control_chars_rejected():
    long_ok, warnings = a.validate_catalog(
        _catalog(_entry("a", hint="条" * (a.MAX_HINT + 10))), "demo")
    assert len(long_ok["achievements"][0]["hint"]) == a.MAX_HINT
    assert any("hint" in w for w in warnings)

    bad, _ = a.validate_catalog(_catalog(_entry("b", hint="触发\x00条件")), "demo")
    assert bad["achievements"][0]["hint"] == ""


def test_view_masks_hidden_tier_and_hint(tmp_path, isolated_registry):
    a.register_core_catalog()
    engine = a.AchievementEngine(store_path=tmp_path / "achievements.json")
    try:
        view = {item["global_id"]: item for item in engine.get_view()}
        locked_hidden = view["core.cancel_twice"]
        assert locked_hidden["tier"] == "hidden"
        assert locked_hidden["title"] == ""
        assert locked_hidden["description"] == ""
        assert locked_hidden["hint"] == ""

        visible = view["core.first_start"]
        assert visible["tier"] == "normal"
        assert visible["hint"] == "第一次按下开始运行"
        assert visible["title"] == "哦，对的对的"
        assert visible["progress"] == 0
    finally:
        a.bind_engine(None)


def test_registry_rejects_core_namespace(tmp_path, isolated_registry):
    """插件占用保留 mod_id 时整份成就声明被拒，且不影响已注册的 core 目录。"""
    a.register_core_catalog()
    before = isolated_registry[a.CORE_MOD_ID]
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    rogue = plugin_dir / "achievements.json"
    rogue.write_text(json.dumps(_catalog(_entry("evil"))), encoding="utf-8")

    reg._register_achievements(a.CORE_MOD_ID, plugin_dir,
                               types.SimpleNamespace(ACHIEVEMENTS="achievements.json"))

    assert isolated_registry[a.CORE_MOD_ID] is before
    assert "evil" not in json.dumps(isolated_registry[a.CORE_MOD_ID])
