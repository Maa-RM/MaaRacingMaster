"""首次引导状态的持久化与恢复边界。

守住三条：
1. 新用户默认未同意、无已完成步骤；
2. 事件幂等，强杀后最多丢一个事件，重复写入不产生重复数据；
3. 老用户（profile 有业务数据、没有 onboarding 段）只补声明、不补引导。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")

_FRONTEND_ONBOARDING = (
    Path(__file__).resolve().parent.parent
    / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "onboarding.js"
)


def _svc(monkeypatch, profile: dict, save_ok: bool = True):
    """绕过 __init__，只注入被测方法用到的 profile 读写。"""
    svc = SidecarService.__new__(SidecarService)
    monkeypatch.setattr(sc, "_load_profile", lambda: profile)

    def _save(partial):
        if not save_ok:
            return False
        profile.update(partial)
        return True

    monkeypatch.setattr(sc, "_save_profile", _save)
    return svc


def test_new_user_defaults(monkeypatch):
    svc = _svc(monkeypatch, {})
    ok, state, err = svc.get_onboarding({})
    assert ok and err is None
    assert state["terms_accepted"] is False
    assert state["steps_done"] == []
    assert state["existing_user"] is False


def test_event_flow_is_idempotent(monkeypatch):
    profile = {}
    svc = _svc(monkeypatch, profile)
    assert svc.onboarding_event({"type": "accept_terms"})[0]
    assert svc.onboarding_event({"type": "seen_launch"})[0]
    assert svc.onboarding_event({"type": "step_done", "id": "manage"})[0]
    assert svc.onboarding_event({"type": "step_done", "id": "manage"})[0]
    assert svc.onboarding_event({"type": "finish"})[0]

    state = svc.get_onboarding({})[1]
    assert state["terms_accepted"] is True
    assert state["launch_seen"] is True
    assert state["steps_done"] == ["manage"]
    assert state["finished_at"]


def test_unknown_event_and_step_rejected(monkeypatch):
    svc = _svc(monkeypatch, {})
    assert svc.onboarding_event({"type": "nope"})[0] is False
    assert svc.onboarding_event({"type": "step_done", "id": "nope"})[0] is False


def test_terms_version_change_needs_reaccept(monkeypatch):
    svc = _svc(monkeypatch, {
        "onboarding": {"terms_version": "old", "terms_accepted_at": "2020-01-01T00:00:00"}
    })
    assert svc.get_onboarding({})[1]["terms_accepted"] is False


def test_existing_user_detected(monkeypatch):
    svc = _svc(monkeypatch, {"run_queue": {"order": ["start_game"], "enabled": {}}})
    assert svc.get_onboarding({})[1]["existing_user"] is True


def test_empty_onboarding_block_still_counts_as_existing_user(monkeypatch):
    """段存在但没有任何进度：说明还没走过声明，仍按业务数据判老用户。"""
    svc = _svc(monkeypatch, {
        "run_queue": {"order": ["start_game"], "enabled": {}},
        "onboarding": {},
    })
    assert svc.get_onboarding({})[1]["existing_user"] is True


def test_existing_user_flag_survives_second_launch(monkeypatch):
    """老用户同意声明后重启，不能因为 onboarding 段已存在而被当成新用户。"""
    profile = {"run_queue": {"order": ["launch_game"], "enabled": {"launch_game": True}}}
    svc = _svc(monkeypatch, profile)
    assert svc.get_onboarding({})[1]["existing_user"] is True
    assert svc.onboarding_event({"type": "accept_terms"})[0]
    assert profile["onboarding"]["existing_user"] is True
    # 重启 = 新服务读同一份存档
    svc2 = _svc(monkeypatch, profile)
    state = svc2.get_onboarding({})[1]
    assert state["existing_user"] is True
    assert state["terms_accepted"] is True


def test_finish_keeps_first_completion_time(monkeypatch):
    profile = {"onboarding": {"finished_at": "2020-01-01T00:00:00+08:00"}}
    svc = _svc(monkeypatch, profile)
    assert svc.onboarding_event({"type": "finish"})[0]
    assert profile["onboarding"]["finished_at"] == "2020-01-01T00:00:00+08:00"


def test_concurrent_step_done_events_both_persist(monkeypatch):
    """两笔并发事件各自读改写：都必须落盘，不能只剩最后一笔。"""
    profile = {}
    svc = _svc(monkeypatch, profile)
    errors = []

    def fire(sid):
        try:
            assert svc.onboarding_event({"type": "step_done", "id": sid})[0]
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=fire, args=(sid,))
               for sid in ("manage", "add_close_game")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(profile["onboarding"]["steps_done"]) == ["add_close_game", "manage"]


def test_save_failure_is_reported(monkeypatch):
    svc = _svc(monkeypatch, {}, save_ok=False)
    ok, _, err = svc.onboarding_event({"type": "accept_terms"})
    assert ok is False
    assert "保存失败" in err


def test_frontend_step_ids_match_sidecar():
    """前后端各维护一份步骤 id：顺序必须一致，否则 step_done 会被后端当未知事件拒绝。"""
    src = _FRONTEND_ONBOARDING.read_text(encoding="utf-8")
    match = re.search(r"var STEP_IDS = \[(.*?)\];", src, re.S)
    assert match, "前端步骤表 STEP_IDS 不见了"
    ids = re.findall(r"'([a-z_]+)'", match.group(1))
    assert tuple(ids) == sc._ONBOARDING_STEPS
