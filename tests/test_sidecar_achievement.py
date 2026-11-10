# -*- coding: utf-8 -*-
"""sidecar 成就 RPC / 通知闸门 / 解锁事件路由（阶段 3）。

构造沿用 test_sidecar_profile_restore：__new__ 绕过 __init__（不拉控制器），注入
被测方法用到的最小状态；_load_profile/_save_profile 运行期替换。契约见
core/achievement.py 与 core/sidecar.py 成就方法。
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path

import pytest

try:
    from maaracing_master.core import achievement as a
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")

skip_no_sidecar = pytestmark  # 本文件整体依赖 sidecar 运行时

REPO = Path(__file__).resolve().parent.parent
RUN_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "run.js"


def _registry():
    norm, _ = a.validate_catalog({"schema_version": 1, "achievements": [
        {"id": "first", "title": "初次上路", "category": "旅程",
         "rule": {"fact": "demo.run", "agg": "count", "target": 1}},
        {"id": "second", "title": "再次上路", "category": "旅程",
         "rule": {"fact": "demo.run2", "agg": "count", "target": 1}},
    ]}, "demo")
    return {"demo": norm}


def _core_registry():
    """core 自带目录（与本包资源同源），供 UI 事件端到端用。"""
    raw = json.loads(a.CORE_CATALOG_PATH.read_text(encoding="utf-8"))
    norm, _ = a.validate_catalog(raw, a.CORE_MOD_ID)
    return {a.CORE_MOD_ID: norm}


def _service(tmp_path, events, registry=None):
    svc = SidecarService.__new__(SidecarService)
    svc._lock = threading.RLock()
    svc._worker = None          # 未运行：导入/重置的前置条件
    svc._achievement_notify = True
    svc._achievement_ui_last = {}
    svc._achievement_ui_lock = threading.Lock()
    svc._send_event_best_effort = events.append
    svc._achievement_engine = a.AchievementEngine(
        store_path=tmp_path / "ach.json", registry=registry or _registry(),
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


def test_unlock_payload_is_self_sufficient(tmp_path):
    """载荷自带提示条/详情卡需要的一切：描述、档位、触发条件、解锁时间。"""
    norm, _ = a.validate_catalog({"schema_version": 1, "achievements": [
        {"id": "hint", "title": "标题", "description": "描述", "category": "旅程",
         "tier": "precious", "hint": "触发条件",
         "rule": {"fact": "demo.posh", "agg": "count", "target": 1}},
        {"id": "sneaky", "title": "秘密", "description": "藏起来", "hidden": True,
         "rule": {"fact": "demo.sneaky", "agg": "count", "target": 1}},
    ]}, "demo")
    events = []
    svc = _service(tmp_path, events, registry={"demo": norm})
    svc._achievement_engine.process_fact("demo.posh", "demo::posh::1", {})
    data = events[-1]["data"]
    assert data["description"] == "描述"
    assert data["tier"] == "precious"
    assert data["hint"] == "触发条件"
    assert isinstance(data["unlocked_ts"], int)

    # 隐藏档：解锁后给真实文案，但视觉档位报 hidden（第四档）
    svc._achievement_engine.process_fact("demo.sneaky", "demo::sneaky::1", {})
    hidden = events[-1]["data"]
    assert hidden["title"] == "秘密" and hidden["tier"] == "hidden"


# ---------- UI 事件通道 ----------


def test_note_ui_event_unlocks_core_achievements(tmp_path):
    events = []
    svc = _service(tmp_path, events, registry=_core_registry())
    a.bind_engine(svc._achievement_engine)  # note_ui_event 经模块级门面上报
    try:
        ok, data, err = svc.note_ui_event({"event": "start_click"})
        assert ok and err is None
        assert [e["data"]["global_id"] for e in events] == ["core.first_start"]

        # 一分钟内的第二次开始：珍贵档（后端按两个事件的到达间隔判定）
        svc.note_ui_event({"event": "start_cancel"})
        svc.note_ui_event({"event": "start_click"})
        svc.note_ui_event({"event": "start_cancel"})
        ids = [e["data"]["global_id"] for e in events]
        assert ids == ["core.first_start", "core.cancel_countdown",
                       "core.start_twice", "core.cancel_twice"]
    finally:
        a.bind_engine(None)


def test_note_ui_event_ignores_unknown_event(tmp_path):
    events = []
    svc = _service(tmp_path, events, registry=_core_registry())
    a.bind_engine(svc._achievement_engine)
    try:
        ok, data, err = svc.note_ui_event({"event": "stop_click"})
        assert ok and err is None and data == {"ignored": True}
        ok, data, _ = svc.note_ui_event({})
        assert ok and data == {"ignored": True}
        assert events == []
    finally:
        a.bind_engine(None)


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
    assert data["state"] == "unavailable"


# ---------- 成就管理 RPC ----------


def test_management_rpcs_roundtrip(tmp_path, monkeypatch):
    events = []
    svc = _service(tmp_path, events, registry=_core_registry())
    backup = tmp_path / "backup.json"
    monkeypatch.setattr(sc, "_pick_achievement_export_path", lambda name: str(backup))
    monkeypatch.setattr(sc, "_pick_achievement_import_path", lambda: str(backup))
    svc._achievement_engine.process_fact("core.ui.start_click", "core::c::1", {})

    ok, data, err = svc.export_achievements({})
    assert ok and err is None and data["count"] == 1 and data["path"] == str(backup)

    ok, data, _ = svc.reset_achievements({"scope": "all"})
    assert ok and data["cleared"] == 1
    assert svc._achievement_engine.get_view()[0]["unlocked"] is False

    ok, data, _ = svc.import_achievements({})
    assert ok and data["count"] == 1
    view = {v["global_id"]: v for v in svc._achievement_engine.get_view()}
    assert view["core.first_start"]["unlocked"] is True  # 不重启即生效


def test_management_cancelled_when_dialog_dismissed(tmp_path, monkeypatch):
    svc = _service(tmp_path, [], registry=_core_registry())
    monkeypatch.setattr(sc, "_pick_achievement_export_path", lambda name: "")
    monkeypatch.setattr(sc, "_pick_achievement_import_path", lambda: "")
    ok, data, err = svc.export_achievements({})
    assert ok and err is None and data == {"cancelled": True}
    ok, data, err = svc.import_achievements({})
    assert ok and err is None and data == {"cancelled": True}


def test_management_refused_while_running(tmp_path, monkeypatch):
    svc = _service(tmp_path, [], registry=_core_registry())
    svc._worker = object()  # slot 已占用 = 运行中
    called = []
    monkeypatch.setattr(sc, "_pick_achievement_import_path", lambda: called.append(1) or "")
    for handler in (svc.import_achievements, svc.reset_achievements):
        ok, data, err = handler({})
        assert ok is False and "运行中" in err
    assert called == []  # 先拒再弹窗：不要让用户白选一遍文件


def test_get_achievements_reports_frozen(tmp_path):
    store = tmp_path / "ach.json"
    store.write_text(json.dumps({"store_schema_version": 99, "progress": {},
                                 "unlocked": {}, "consumed_keys": []}), encoding="utf-8")
    svc = _service(tmp_path, [], registry=_core_registry())
    ok, data, _ = svc.get_achievements({})
    assert ok and data["state"] == "frozen"
    assert data["frozen_reason"] == "version"
    assert data["store_version"] == 99
    assert data["supported_version"] == a.ACHIEVEMENT_STORE_SCHEMA_VERSION

    ok, _, err = svc.reset_achievements({"scope": "all"})
    assert ok is False and "升级" in err


@pytest.mark.parametrize("params", [
    {}, {"scope": None}, {"scope": ""}, {"scope": 0}, {"scope": False},
    {"scope": "core.first_start"},
])
def test_reset_rejects_illegal_scope(tmp_path, params):
    """非法/缺失 scope 一律拒绝：绝不能兜底成"全清"。"""
    svc = _service(tmp_path, [], registry=_core_registry())
    store = tmp_path / "ach.json"
    svc._achievement_engine.process_fact("core.ui.start_click", "core::c::1", {})
    before = store.read_text(encoding="utf-8")
    ok, data, err = svc.reset_achievements(params)
    assert ok is False and err
    assert store.read_text(encoding="utf-8") == before
    assert svc._achievement_engine.get_view()[0]["unlocked"] is True


def test_get_achievements_survives_poisoned_store(tmp_path):
    """污染存档修复后，RPC 载荷必须是标准 JSON（壳侧解析器不接受 NaN/Infinity）。"""
    store = tmp_path / "ach.json"
    store.write_text('{"store_schema_version": 1,'
                     ' "progress": {"core.first_start": NaN},'
                     ' "unlocked": {"core.first_start": Infinity},'
                     ' "consumed_keys": []}', encoding="utf-8")
    svc = _service(tmp_path, [], registry=_core_registry())
    ok, data, err = svc.get_achievements({})
    assert ok and err is None and data["state"] == "ready"
    assert data["frozen_reason"] is None
    assert data["repaired_backup"]
    json.dumps(data, ensure_ascii=False, allow_nan=False)  # 不抛即合法


# ---------- UI 事实：发射点、时间窗、前端字面量 ----------


@skip_no_sidecar
@pytest.mark.parametrize("event,prev,now,expected", [
    ("start_click", None, 100.0, ["core.ui.start_click"]),
    ("start_click", 0.0, 59.999, ["core.ui.start_click", "core.ui.start_repeat_fast"]),
    ("start_click", 0.0, 60.0, ["core.ui.start_click"]),
    ("start_cancel", 10.0, 20.0, ["core.ui.start_cancel", "core.ui.cancel_repeat_fast"]),
    ("start_cancel", 10.0, 70.0, ["core.ui.start_cancel"]),
    ("stop_click", None, 0.0, []),
])
def test_core_ui_facts_window_boundary(event, prev, now, expected):
    assert sc.core_ui_facts(event, prev, now) == expected


@skip_no_sidecar
def test_core_catalog_facts_match_emitters():
    """双向锁：目录里的每条 fact 都有发射点，每个发射点都被目录消费。"""
    facts = {e["rule"]["fact"] for e in _core_registry()[a.CORE_MOD_ID]["achievements"]}
    assert facts == set(sc.CORE_UI_FACTS)
    assert {f"core.ui.{e}" for e in sc.CORE_UI_EVENTS} <= set(sc.CORE_UI_FACTS)


@skip_no_sidecar
def test_frontend_event_literals_in_whitelist():
    """前端是 IIFE、无法 require，只能静态扫：她报的事件名必须在后端白名单里。"""
    src = RUN_JS.read_text(encoding="utf-8")
    literals = set(re.findall(r"_noteUiEvent\('([a-z_]+)'\)", src))
    assert literals, "run.js 里找不到 _noteUiEvent 调用：上报点被删或改名"
    assert literals <= sc.CORE_UI_EVENTS


@skip_no_sidecar
def test_core_achievements_unlock_through_facts(tmp_path):
    """四条核心成就的事务路径：事实 → 进度 → 解锁（含第一次 vs 一分钟内第二次）。"""
    unlocked: list[str] = []
    engine = a.AchievementEngine(store_path=tmp_path / "ach.json",
                                 registry=_core_registry(),
                                 on_unlock=lambda gid, entry, ts: unlocked.append(gid))
    a.bind_engine(engine)
    try:
        def emit(event, prev, now):
            for kind in sc.core_ui_facts(event, prev, now):
                assert a.report_fact(kind, f"{kind}::{uuid.uuid4().hex}", {}) is True

        emit("start_click", None, 0.0)          # 第一次开始
        assert unlocked == ["core.first_start"]

        emit("start_cancel", None, 3.0)         # 第一次打断倒计时
        assert unlocked == ["core.first_start", "core.cancel_countdown"]

        emit("start_click", 0.0, 5.0)           # 一分钟内再来一次
        emit("start_cancel", 3.0, 6.0)          # 一分钟内再打断一次
        assert unlocked[-2:] == ["core.start_twice", "core.cancel_twice"]

        view = {item["global_id"]: item for item in engine.get_view()}
        assert all(item["unlocked"] for item in view.values())
        assert view["core.cancel_twice"]["title"] == "对...对吗？"   # 解锁后不再遮蔽
        assert view["core.cancel_twice"]["hint"]
    finally:
        a.bind_engine(None)


@skip_no_sidecar
def test_window_slow_repeat_does_not_unlock(tmp_path):
    """窗口外重报不推第二条：一分钟以上的两次开始只解锁普通档。"""
    engine = a.AchievementEngine(store_path=tmp_path / "ach.json", registry=_core_registry())
    a.bind_engine(engine)
    try:
        for prev, now in ((None, 0.0), (0.0, 61.0)):
            for kind in sc.core_ui_facts("start_click", prev, now):
                a.report_fact(kind, f"{kind}::{uuid.uuid4().hex}", {})
        view = {item["global_id"]: item for item in engine.get_view()}
        assert view["core.first_start"]["unlocked"] is True
        assert view["core.start_twice"]["unlocked"] is False
    finally:
        a.bind_engine(None)