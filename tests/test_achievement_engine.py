# -*- coding: utf-8 -*-
"""成就引擎/门面/存档/注册发现：夹具验证 A1–A4 + 存档版本保护（A6 边界）。

契约见 core/achievement.py 模块 docstring。夹具非业务成就；不接 GUI/业务。
"""
from __future__ import annotations

import json
import types
from pathlib import Path

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
        on_unlock=(lambda gid, en, ts: events.append(gid)) if events is not None else None,
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
    reg = _registry(_entry("h", {"fact": "k", "agg": "count", "target": 3}, hidden=True))
    eng = _engine(tmp_path, reg)
    v0 = eng.get_view()[0]
    assert v0["title"] == "" and v0["description"] == "" and v0["hint"] == ""
    # 规则字段同样遮蔽：只藏文案，进度条会把条件写在脸上
    assert v0["agg"] is None and v0["target"] is None and v0["progress"] is None
    eng.process_fact("k", "1", {})
    v1 = eng.get_view()[0]
    # 有进度但未解锁：仍然整条遮蔽（进度值本身会暴露条件）
    assert v1["agg"] is None and v1["target"] is None and v1["progress"] is None
    assert v1["title"] == ""
    eng.process_fact("k", "2", {})
    eng.process_fact("k", "3", {})
    v2 = eng.get_view()[0]
    assert v2["title"] == "标题h"
    assert v2["unlocked"] is True and v2["progress"] == 3


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

    def _boom(*a, **k):
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

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(eng, "_save_state_locked", _boom)
    assert a.report_fact("k", "1", {}) is False  # 不抛
    assert eng.get_view()[0]["progress"] == 0


def test_a4_notify_error_does_not_block_commit(tmp_path):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 1}))

    def _bad_unlock(gid, en, ts):
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


# ---------- 事务失败路径：解锁态必须随进度一起去重一起回滚 ----------

def test_unlock_rollback_on_write_failure_then_resubmit(tmp_path, monkeypatch):
    events = []
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 1}))
    eng = _engine(tmp_path, reg, events)
    original = eng._save_state_locked

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(eng, "_save_state_locked", _boom)
    with pytest.raises(OSError):
        eng.process_fact("k", "1", {})
    v = eng.get_view()[0]
    assert v["progress"] == 0 and v["unlocked"] is False  # 解锁态真回滚
    assert events == []
    assert not (tmp_path / "ach.json").exists()
    monkeypatch.setattr(eng, "_save_state_locked", original)
    assert eng.process_fact("k", "1", {}) is True  # 回滚后未记去重，同键可重提
    assert eng.get_view()[0]["unlocked"] is True
    assert events == ["demo.c"]  # 仅首次成功提交通知一次


def test_meta_unlock_rollback_on_write_failure(tmp_path, monkeypatch):
    events = []
    reg = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}),
                    _entry("meta", {"agg": "all_unlocked", "scope": "plugin"}))
    eng = _engine(tmp_path, reg, events)
    original = eng._save_state_locked

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(eng, "_save_state_locked", _boom)
    with pytest.raises(OSError):
        eng.process_fact("k", "1", {})  # 一条事实同时解锁普通+元成就
    assert {v["global_id"] for v in eng.get_view() if v["unlocked"]} == set()
    assert events == []
    monkeypatch.setattr(eng, "_save_state_locked", original)
    assert eng.process_fact("k", "1", {}) is True
    assert {v["global_id"] for v in eng.get_view() if v["unlocked"]} == {"demo.dep", "demo.meta"}
    assert events == ["demo.dep", "demo.meta"]


def test_non_oserror_rollback_and_facade_isolation(tmp_path, monkeypatch):
    reg = _registry(_entry("c", {"fact": "k", "agg": "count", "target": 1}))
    eng = _engine(tmp_path, reg)

    def _boom(*a, **k):
        raise TypeError("serialize fail")

    monkeypatch.setattr(eng, "_save_state_locked", _boom)
    a.bind_engine(eng)
    assert a.report_fact("k", "1", {}) is False  # 门面隔离非 OSError
    v = eng.get_view()[0]
    assert v["progress"] == 0 and v["unlocked"] is False


def test_init_meta_write_failure_no_fake_unlock(tmp_path, monkeypatch):
    reg_dep = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}))
    _engine(tmp_path, reg_dep).process_fact("k", "1", {})  # dep 解锁落盘
    reg_full = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}),
                         _entry("meta", {"agg": "all_unlocked", "scope": "plugin"}))

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(a.AchievementEngine, "_save_state_locked", _boom)
    eng2 = a.AchievementEngine(store_path=tmp_path / "ach.json", registry=reg_full)  # 不抛
    assert {v["global_id"] for v in eng2.get_view() if v["unlocked"]} == {"demo.dep"}


def test_init_meta_non_oserror_isolated(tmp_path, monkeypatch):
    reg_dep = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}))
    _engine(tmp_path, reg_dep).process_fact("k", "1", {})
    reg_full = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}),
                         _entry("meta", {"agg": "all_unlocked", "scope": "plugin"}))

    def _boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(a.AchievementEngine, "_save_state_locked", _boom)
    eng2 = a.AchievementEngine(store_path=tmp_path / "ach.json", registry=reg_full)  # 不抛
    assert {v["global_id"] for v in eng2.get_view() if v["unlocked"]} == {"demo.dep"}

# ---------- 导入 / 导出 / 重置 ----------


def _one(tmp_path, **kw):
    """单条夹具成就（fact=k，target=1）。"""
    reg = _registry(_entry("first", {"fact": "k", "agg": "count", "target": 1}))
    return a.AchievementEngine(store_path=tmp_path / "ach.json", registry=reg, **kw)


def test_export_roundtrip_keeps_progress(tmp_path):
    eng = _one(tmp_path)
    eng.process_fact("k", "k::1", {})
    backup = tmp_path / "backup.json"
    assert eng.export_store(backup) == 1
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["exported_at"] and payload["app_version"]
    assert payload["store_schema_version"] == a.ACHIEVEMENT_STORE_SCHEMA_VERSION

    eng.reset("all")
    assert eng.get_view()[0]["unlocked"] is False
    assert eng.import_store(backup) == 1
    view = eng.get_view()[0]
    # 导入必须同步内存态：不重启就能看到新进度（只改磁盘 = 假成功）
    assert view["unlocked"] is True and view["progress"] == 1


def test_export_without_store_writes_empty_backup(tmp_path):
    eng = _one(tmp_path)  # 从没产生过进度：存档文件尚未生成
    assert not (tmp_path / "ach.json").exists()
    backup = tmp_path / "backup.json"
    assert eng.export_store(backup) == 0
    assert json.loads(backup.read_text(encoding="utf-8"))["progress"] == {}


def test_import_rejects_higher_store_version(tmp_path):
    eng = _one(tmp_path)
    eng.process_fact("k", "k::1", {})
    before = (tmp_path / "ach.json").read_text(encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"store_schema_version": 99, "progress": {},
                               "unlocked": {}, "consumed_keys": []}), encoding="utf-8")
    with pytest.raises(a.AchievementStoreError):
        eng.import_store(bad)
    assert (tmp_path / "ach.json").read_text(encoding="utf-8") == before
    assert eng.get_view()[0]["unlocked"] is True


def test_import_bad_shape_keeps_original(tmp_path):
    eng = _one(tmp_path)
    eng.process_fact("k", "k::1", {})
    before = (tmp_path / "ach.json").read_text(encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"store_schema_version": 1, "progress": {}}), encoding="utf-8")
    with pytest.raises(a.AchievementStoreError):
        eng.import_store(bad)
    assert (tmp_path / "ach.json").read_text(encoding="utf-8") == before


def test_import_backs_up_current_store(tmp_path):
    eng = _one(tmp_path)
    eng.process_fact("k", "k::1", {})
    backup = tmp_path / "backup.json"
    eng.export_store(backup)
    eng.import_store(backup)
    assert list(tmp_path.glob("ach.pre-import-*.json"))


def test_reset_all_clears_consumed_keys(tmp_path):
    eng = _one(tmp_path)
    eng.process_fact("k", "k::1", {})
    assert eng.reset("all") == 1
    state = json.loads((tmp_path / "ach.json").read_text(encoding="utf-8"))
    assert state["consumed_keys"] == [] and state["progress"] == {} and state["unlocked"] == {}


def test_reset_single_rejected(tmp_path):
    """单项重置暂不开放：级联语义定案前不保留这条不一致路径。"""
    eng = _one(tmp_path)
    with pytest.raises(a.AchievementStoreError):
        eng.reset("demo.first")


@pytest.mark.parametrize("scope", ["", "ALL", "all ", "demo.nope"])
def test_reset_illegal_scope_rejected(tmp_path, scope):
    eng = _one(tmp_path)
    with pytest.raises(a.AchievementStoreError):
        eng.reset(scope)


def test_frozen_store_rejects_writes_and_keeps_file(tmp_path):
    store = tmp_path / "ach.json"
    original = json.dumps({"store_schema_version": 99, "progress": {"demo.first": 1},
                           "unlocked": {"demo.first": 1}, "consumed_keys": ["k::1"]})
    store.write_text(original, encoding="utf-8")
    eng = _one(tmp_path)
    assert eng.frozen is True and eng.store_version == 99

    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"store_schema_version": 1, "progress": {},
                                  "unlocked": {}, "consumed_keys": []}), encoding="utf-8")
    with pytest.raises(a.AchievementFrozenError):
        eng.reset("all")
    with pytest.raises(a.AchievementFrozenError):
        eng.import_store(backup)
    assert store.read_text(encoding="utf-8") == original  # 只读保护：原文件一个字节没动


def test_reload_recomputes_frozen_state(tmp_path):
    store = tmp_path / "ach.json"
    store.write_text(json.dumps({"store_schema_version": 99, "progress": {},
                                 "unlocked": {}, "consumed_keys": []}), encoding="utf-8")
    eng = _one(tmp_path)
    assert eng.frozen is True

    store.write_text(json.dumps({"store_schema_version": 1,
                                 "progress": {"demo.first": 1},
                                 "unlocked": {"demo.first": 1700000000000000000},
                                 "consumed_keys": []}), encoding="utf-8")
    assert eng.reload_from_disk() is True
    assert eng.frozen is False and eng.store_version == 1
    assert eng.get_view()[0]["unlocked"] is True


# ---------- 存档规范化 / 损坏修复 ----------

def test_poisoned_store_repaired_with_backup(tmp_path):
    """NaN / Infinity / 错类型：原文件留证后修复，视图可安全序列化，补偿不弹通知。"""
    store = tmp_path / "ach.json"
    original = ('{"store_schema_version": 1,'
                ' "progress": {"demo.ok": 2, "demo.nan": NaN, "demo.str": "oops"},'
                ' "unlocked": {"demo.ok": Infinity},'
                ' "consumed_keys": [1, "k::1"]}')
    store.write_text(original, encoding="utf-8")
    reg = _registry(_entry("ok", {"fact": "k", "agg": "count", "target": 2}))
    events = []
    eng = a.AchievementEngine(store_path=store, registry=reg,
                              on_unlock=lambda gid, en, ts: events.append(gid))

    assert eng.frozen is False
    backup = tmp_path / eng.repaired_backup
    assert backup.is_file() and backup.read_text(encoding="utf-8") == original
    text = store.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    json.dumps(eng.get_view(), ensure_ascii=False, allow_nan=False)  # RPC 输出必须合法
    view = {v["global_id"]: v for v in eng.get_view()}
    assert view["demo.ok"]["unlocked"] is True    # 进度达标且缺解锁记录 → 补齐
    assert events == []                           # 修复与补偿都静默


def test_poisoned_store_without_backup_turns_readonly(tmp_path, monkeypatch):
    """留证失败时宁可不写盘：原文件一个字节都不能动。"""
    store = tmp_path / "ach.json"
    original = ('{"store_schema_version":1,"progress":{"demo.first":"oops"},'
                '"unlocked":{},"consumed_keys":[]}')
    store.write_text(original, encoding="utf-8")
    real_open = Path.open

    def fail_exclusive(self, mode="r", *args, **kwargs):
        if mode == "xb":
            raise OSError("备份目录不可写")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_exclusive)
    eng = _one(tmp_path)
    assert eng.frozen is True and eng.frozen_reason == "corrupt"
    assert eng.process_fact("k", "1", {}) is False
    assert store.read_text(encoding="utf-8") == original

    # 只读也要留一条恢复路径：导出内存态（规范后的空档），不照抄非法原文件
    dest = tmp_path / "export.json"
    assert eng.export_store(dest) == 0
    assert json.loads(dest.read_text(encoding="utf-8"))["progress"] == {}


def test_load_compensates_achieved_without_unlock(tmp_path):
    """启动加载与导入同一条补偿路径：补解锁但不发通知。"""
    store = tmp_path / "ach.json"
    store.write_text(json.dumps({"store_schema_version": 1,
                                 "progress": {"demo.first": 5},
                                 "unlocked": {}, "consumed_keys": []}), encoding="utf-8")
    events = []
    eng = _engine(tmp_path, _registry(_entry("first", {"fact": "k", "agg": "count",
                                                      "target": 1})), events)
    assert eng.get_view()[0]["unlocked"] is True
    assert events == []
    assert "demo.first" in json.loads(store.read_text(encoding="utf-8"))["unlocked"]


# ---------- 聚合溢出 ----------

def test_sum_overflow_rejected_without_pollution(tmp_path):
    store = tmp_path / "ach.json"
    reg = _registry(_entry("s", {"fact": "k", "agg": "sum", "field": "v", "target": 1}))
    eng = _engine(tmp_path, reg)
    assert eng.process_fact("k", "1", {"v": 1e308}) is True
    assert eng.process_fact("k", "2", {"v": 1e308}) is True   # 提交去重键，但不累计
    assert eng.get_view()[0]["progress"] == 1e308
    text = store.read_text(encoding="utf-8")
    assert "Infinity" not in text
    assert eng.process_fact("k", "2", {"v": 1.0}) is False     # 去重键已消费


# ---------- 导入：拒绝非法值 / 补偿 / 原子性 ----------

@pytest.mark.parametrize("bad", [
    {"progress": {"demo.first": "invalid"}},
    {"progress": {"demo.first": float("nan")}},
    {"progress": {"demo.first": float("inf")}},
    {"unlocked": {"demo.first": "yesterday"}},
    {"consumed_keys": [1]},
])
def test_import_rejects_invalid_values(tmp_path, bad):
    eng = _one(tmp_path)
    eng.process_fact("k", "k::1", {})
    store = tmp_path / "ach.json"
    before = store.read_text(encoding="utf-8")
    payload = {"store_schema_version": 1, "progress": {}, "unlocked": {},
               "consumed_keys": []}
    payload.update(bad)
    src = tmp_path / "bad.json"
    src.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(a.AchievementStoreError):
        eng.import_store(src)
    assert store.read_text(encoding="utf-8") == before
    view = eng.get_view()[0]
    assert view["unlocked"] is True and view["progress"] == 1


def test_import_compensates_achieved_and_meta_without_notify(tmp_path):
    events = []
    reg = _registry(_entry("dep", {"fact": "k", "agg": "count", "target": 1}),
                    _entry("meta", {"agg": "all_unlocked", "scope": "plugin"}))
    eng = a.AchievementEngine(store_path=tmp_path / "ach.json", registry=reg,
                              on_unlock=lambda gid, en, ts: events.append(gid))
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"store_schema_version": 1,
                                  "progress": {"demo.dep": 5}, "unlocked": {},
                                  "consumed_keys": []}), encoding="utf-8")
    assert eng.import_store(backup) == 1
    view = {v["global_id"]: v for v in eng.get_view()}
    assert view["demo.dep"]["unlocked"] is True
    assert view["demo.meta"]["unlocked"] is True
    assert events == []                       # 恢复备份不弹提示
    state = json.loads((tmp_path / "ach.json").read_text(encoding="utf-8"))
    assert set(state["unlocked"]) == {"demo.dep", "demo.meta"}   # 补偿一次落盘


def test_import_keeps_historical_unlock_below_target(tmp_path):
    """规则目标提高不得撤销历史解锁：只补缺，不改已有。"""
    reg = _registry(_entry("x", {"fact": "k", "agg": "count", "target": 10}))
    eng = a.AchievementEngine(store_path=tmp_path / "ach.json", registry=reg)
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"store_schema_version": 1,
                                  "progress": {"demo.x": 3},
                                  "unlocked": {"demo.x": 1700000000000000000},
                                  "consumed_keys": []}), encoding="utf-8")
    eng.import_store(backup)
    view = eng.get_view()[0]
    assert view["unlocked"] is True and view["progress"] == 3


def test_import_coerces_numeric_unlock_ts(tmp_path):
    eng = _one(tmp_path)
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"store_schema_version": 1,
                                  "progress": {"demo.first": 1},
                                  "unlocked": {"demo.first": 1.7e18},
                                  "consumed_keys": []}), encoding="utf-8")
    eng.import_store(backup)
    assert isinstance(eng.get_view()[0]["unlocked_ts"], int)


def test_import_write_failure_keeps_disk_and_memory(tmp_path, monkeypatch):
    eng = _one(tmp_path)
    eng.process_fact("k", "k::1", {})
    store = tmp_path / "ach.json"
    before = store.read_text(encoding="utf-8")
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"store_schema_version": 1, "progress": {},
                                  "unlocked": {}, "consumed_keys": []}),
                      encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(eng, "_save_state_locked", _boom)
    with pytest.raises(OSError):
        eng.import_store(backup)
    assert store.read_text(encoding="utf-8") == before
    view = eng.get_view()[0]
    assert view["unlocked"] is True and view["progress"] == 1
