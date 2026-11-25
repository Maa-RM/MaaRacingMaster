"""首次引导状态的持久化与恢复边界。

守住三条：
1. 新用户默认未同意、无已完成步骤；
2. 事件幂等，强杀后最多丢一个事件，重复写入不产生重复数据；
3. 老用户（profile 有业务数据、没有 onboarding 段）只补声明、不补引导。
"""
from __future__ import annotations

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.sidecar import SidecarService
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


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


def test_existing_user_not_applied_after_onboarding_exists(monkeypatch):
    svc = _svc(monkeypatch, {
        "run_queue": {"order": ["start_game"], "enabled": {}},
        "onboarding": {},
    })
    assert svc.get_onboarding({})[1]["existing_user"] is False


def test_save_failure_is_reported(monkeypatch):
    svc = _svc(monkeypatch, {}, save_ok=False)
    ok, _, err = svc.onboarding_event({"type": "accept_terms"})
    assert ok is False
    assert "保存失败" in err
