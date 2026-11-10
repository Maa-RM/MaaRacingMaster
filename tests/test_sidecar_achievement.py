# -*- coding: utf-8 -*-
"""sidecar 成就 RPC / 通知闸门 / 解锁事件路由（阶段 3）。

构造沿用 test_sidecar_profile_restore：__new__ 绕过 __init__（不拉控制器），注入
被测方法用到的最小状态；_load_profile/_save_profile 运行期替换。契约见
core/achievement.py 与 core/sidecar.py 成就方法。
"""
from __future__ import annotations

import threading

import pytest

try:
    from maaracing_master.core import achievement as a
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


def _registry():
    norm, _ = a.validate_catalog({"schema_version": 1, "achievements": [
        {"id": "first", "title": "初次上路", "category": "旅程",
         "rule": {"fact": "demo.run", "agg": "count", "target": 1}},
        {"id": "second", "title": "再次上路", "category": "旅程",
         "rule": {"fact": "demo.run2", "agg": "count", "target": 1}},
    ]}, "demo")
    return {"demo": norm}


def _service(tmp_path, events):
    svc = SidecarService.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._achievement_notify = True
    svc._send_event_best_effort = events.append
    svc._achievement_engine = a.AchievementEngine(
        store_path=tmp_path / "ach.json", registry=_registry(),
        on_unlock=svc._on_achievement_unlock)
    return svc


def test_get_achievements_returns_view_and_notify(tmp_path):
    svc = _service(tmp_path, [])
    ok, data, err = svc.get_achievements({})
    assert ok and err is None
    assert data["notify"] is True
    assert {x["global_id"] for x in data["achievements"]} == {"demo.first", "demo.second"}


def test_set_achievement_notify_persists(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(sc, "_load_profile", lambda: {})
    monkeypatch.setattr(sc, "_save_profile", lambda partial: saved.update(partial))
    svc = _service(tmp_path, [])
    ok, data, _ = svc.set_achievement_notify({"enabled": False})
    assert ok and data["achievement_notify"] is False
    assert svc._achievement_notify is False
    assert saved["debug"]["achievement_notify"] is False


def test_unlock_emits_event_when_notify_on(tmp_path):
    events = []
    svc = _service(tmp_path, events)
    assert svc._achievement_engine.process_fact("demo.run", "demo::run::1", {}) is True
    assert len(events) == 1
    assert events[0]["event"] == "achievement_unlocked"
    assert events[0]["data"]["global_id"] == "demo.first"
    assert events[0]["data"]["title"] == "初次上路"


def test_notify_off_suppresses_event_but_commits(tmp_path):
    events = []
    svc = _service(tmp_path, events)
    svc._achievement_notify = False
    assert svc._achievement_engine.process_fact("demo.run", "demo::run::1", {}) is True
    assert events == []  # 不弹通知
    assert svc._achievement_engine.get_view()[0]["unlocked"] is True  # 进度仍提交


def test_restore_profile_recovers_notify_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_load_profile",
                        lambda: {"debug": {"achievement_notify": False}})
    svc = _service(tmp_path, [])
    svc._achievement_notify = True
    svc._restore_profile()
    assert svc._achievement_notify is False


def test_notify_off_init_meta_suppresses_event(tmp_path):
    norm, _ = a.validate_catalog({"schema_version": 1, "achievements": [
        {"id": "dep", "title": "依赖", "rule": {"fact": "k", "agg": "count", "target": 1}},
        {"id": "meta", "title": "集齐", "rule": {"agg": "all_unlocked", "scope": "plugin"}},
    ]}, "demo")
    reg = {"demo": norm}
    a.AchievementEngine(store_path=tmp_path / "ach.json", registry=reg).process_fact("k", "1", {})
    events = []
    svc = SidecarService.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._achievement_notify = False  # 用户已关通知（先于引擎构造回填）
    svc._send_event_best_effort = events.append
    svc._achievement_engine = a.AchievementEngine(
        store_path=tmp_path / "ach.json", registry=reg, on_unlock=svc._on_achievement_unlock)
    assert events == []  # 初始化触发元成就解锁但不发通知
    assert {v["global_id"] for v in svc._achievement_engine.get_view()
            if v["unlocked"]} == {"demo.dep", "demo.meta"}


def test_get_achievements_null_engine_degrades(tmp_path):
    svc = _service(tmp_path, [])
    svc._achievement_engine = None  # 引擎初始化失败降级
    ok, data, err = svc.get_achievements({})
    assert ok and err is None
    assert data["achievements"] == []
    assert data["notify"] is True