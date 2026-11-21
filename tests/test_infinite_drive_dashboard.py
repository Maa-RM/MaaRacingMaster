# -*- coding: utf-8 -*-
"""无限驾驶结算记录存储与战绩看板的回归锁。

记录门槛（v1 口径延续）：只有程序局（模块流程内）的结算入库。字段按 v2 最终
结算页定：本场得分 / 过关条件 / 关卡进度 n/5 / 本轮总得分；过关 = 得分 ≥ 过关
条件。OCR 毛刺字段显式置 None，读侧容忍坏行。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from maaracing_master.core.dashboard_contract import normalize_dashboard
from maaracing_master.plugins.infinite_drive import dashboard_store
from maaracing_master.plugins.infinite_drive.module import InfiniteDriveModule

# 真机 20261121_132826 实测读数（第 1 关结算页）
SCORE_OK = "16,110"
PROGRESS_OK = "关卡进度：1/5本轮总得分：16,110"


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_store, "store_path",
                        lambda: tmp_path / "records.jsonl")


def test_record_settlement_parses_real_reading() -> None:
    entry = dashboard_store.record_settlement(SCORE_OK, PROGRESS_OK,
                                              session_token="s1", round_no=1)
    assert entry["score"] == 16110
    assert entry["level"] == 1 and entry["level_total"] == 5
    assert entry["total_score"] == 16110
    assert entry["target"] == 6000
    assert entry["cleared"] is True
    assert entry["score_raw"] == SCORE_OK and entry["progress_raw"] == PROGRESS_OK


def test_record_settlement_garbage_keeps_raw_and_none() -> None:
    entry = dashboard_store.record_settlement("⑧毛刺", "进度", session_token=None,
                                              round_no=2)
    assert entry["score"] is None and entry["score_raw"] == "⑧毛刺"
    assert entry["level"] is None and entry["target"] is None
    assert entry["cleared"] is None


def test_parse_score_bounds() -> None:
    assert dashboard_store.parse_score("123,456") == 123456
    assert dashboard_store.parse_score("１") == 1           # 全角数字宽容解析（OCR 常见）
    assert dashboard_store.parse_score("1234567") is None   # 超合理范围 = OCR 毛刺
    assert dashboard_store.parse_score("") is None


def test_append_read_roundtrip_and_bad_lines(tmp_path) -> None:
    for round_no in (1, 2):
        dashboard_store.append(dashboard_store.record_settlement(
            SCORE_OK, PROGRESS_OK, session_token="s", round_no=round_no))
    (dashboard_store.store_path()).open("a", encoding="utf-8").write("坏行\n")
    dashboard_store.append(dashboard_store.record_settlement(
        SCORE_OK, PROGRESS_OK, session_token="s", round_no=3))

    rows = dashboard_store.read()
    assert [r["round_no"] for r in rows] == [1, 2, 3]  # 坏行被跳过
    assert [r["round_no"] for r in dashboard_store.read(limit=2)] == [2, 3]


# ---------- 轮均用时盘点（预计用时的真源） ----------

def _settle(session: str, round_no: int, ts: str) -> None:
    dashboard_store.append(dashboard_store.record_settlement(
        SCORE_OK, PROGRESS_OK, session_token=session, round_no=round_no,
        now=datetime.fromisoformat(ts)))


def test_median_round_seconds_uses_same_session_adjacent_settles() -> None:
    """同一次运行内相邻结算差 = 一轮用时（胜负都算）；取中位数。"""
    for round_no, ts in ((1, "2026-11-21T20:30:00"), (2, "2026-11-21T20:33:00"),
                         (3, "2026-11-21T20:37:00"), (4, "2026-11-21T20:40:00")):
        _settle("s", round_no, ts)
    # 间隔 180 / 240 / 180 → 中位数 180；首局含入口导航，天然不参与（无前结算）
    assert dashboard_store.median_round_seconds() == 180.0


def test_median_round_seconds_ignores_cross_session_and_bad_pairs() -> None:
    """跨运行、序号不连续（漏读一轮）、坏时间戳、超界间隔都不进样本。"""
    _settle("a", 1, "2026-11-21T20:30:00")
    _settle("b", 1, "2026-11-21T20:31:00")   # 跨运行（a→b）：不采
    _settle("b", 3, "2026-11-21T20:33:00")   # 序号 1→3 不连续：不采
    (dashboard_store.store_path()).open("a", encoding="utf-8").write(
        json.dumps({"schema": 1, "ts": "垃圾", "session": "b", "round_no": 4},
                   ensure_ascii=False) + "\n")
    _settle("b", 5, "2026-11-21T21:30:00")   # 与上一条之间隔 1h（含坏行）：不采
    _settle("b", 6, "2026-11-21T21:33:00")   # 180s：唯一有效样本
    _settle("b", 7, "2026-11-21T21:33:10")   # 10s，低于下限：不采
    assert dashboard_store.median_round_seconds() == 180.0


def test_median_round_seconds_none_without_valid_pairs() -> None:
    """空库 / 只有单局：没有依据就不出估计值。"""
    assert dashboard_store.median_round_seconds() is None
    _settle("s", 1, "2026-11-21T20:30:00")
    assert dashboard_store.median_round_seconds() is None


def test_payload_empty_and_filled() -> None:
    empty = dashboard_store.read_dashboard_payload()
    assert empty["blocks"][0]["type"] == "empty"       # 空态走契约 empty 原语
    assert empty["blocks"][0]["title"] == "还没有结算记录"
    assert normalize_dashboard(empty)[0] is not None   # 空态载荷同样过契约校验

    for round_no in (1, 2):
        dashboard_store.append(dashboard_store.record_settlement(
            SCORE_OK, PROGRESS_OK, session_token="s", round_no=round_no))
    payload = dashboard_store.read_dashboard_payload()
    assert payload["title"] == "极速狂飙战绩"
    stats = payload["blocks"][0]
    assert stats["type"] == "stats" and stats["items"][0]["value"] == "2"
    rows = payload["blocks"][1]["rows"]
    assert rows[0]["text"].startswith("第1关 · 16,110")   # 最新在前
    assert rows[0]["badge"]["text"] == "过关"
    assert normalize_dashboard(payload)[0] is not None    # 过契约校验


def test_read_dashboard_classmethod_warns_on_store_error(tmp_path, monkeypatch) -> None:
    def _boom():
        raise OSError("盘坏了")

    monkeypatch.setattr(dashboard_store, "read_dashboard_payload", _boom)
    payload = InfiniteDriveModule.read_dashboard()
    assert payload["badge"]["text"] == "读取失败"


def test_module_settlement_appends_record(tmp_path, monkeypatch) -> None:
    """结算读数 → 落一条记录；记录失败不阻断流程。"""

    class _FakeOcr:
        def __init__(self, *a, **k) -> None:
            pass

        def recognize(self, _rgb, roi):
            from maaracing_master.plugins.infinite_drive import flow_data

            text = SCORE_OK if roi == flow_data.RESULT_SCORE_ROI else PROGRESS_OK
            return type("R", (), {"text": text})()

    class _Capture:
        def frame_with_age(self):
            return (object(), 1, 2, 0.0)

    class _Ctx:
        capture = _Capture()

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "RapidOcrEngine", _FakeOcr)
    mod = InfiniteDriveModule(None)
    mod.ctx = _Ctx()

    mod._read_settlement()
    assert mod._settlement_round == 1
    assert mod.last_settlement["record"]["score"] == 16110
    rows = json.loads((dashboard_store.store_path()).read_text(
        encoding="utf-8").splitlines()[0] or "{}")
    assert rows["level"] == 1


def test_module_settlement_skips_record_in_record_mode(tmp_path, monkeypatch) -> None:
    """录制局（人工驾驶语料局）不落战绩——只读读数，不写记录。"""

    class _FakeOcr:
        def __init__(self, *a, **k) -> None:
            pass

        def recognize(self, _rgb, _roi):
            return type("R", (), {"text": SCORE_OK})()

    class _Capture:
        def frame_with_age(self):
            return (object(), 1, 2, 0.0)

    class _Ctx:
        capture = _Capture()

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "RapidOcrEngine", _FakeOcr)
    mod = InfiniteDriveModule(None)
    mod.ctx = _Ctx()
    mod._record_mode = True

    mod._read_settlement()

    assert mod._settlement_round == 0            # 不消耗局内序号
    assert mod.last_settlement["score"] == SCORE_OK  # 读数照常留存
    assert not dashboard_store.store_path().exists()  # 战绩库未落


def test_module_settlement_refreshes_config_ui_estimate(monkeypatch) -> None:
    """结算落库后重建表单声明：下一拍配置读取带最新轮均预计用时。"""

    class _FakeOcr:
        def __init__(self, *a, **k) -> None:
            pass

        def recognize(self, _rgb, roi):
            from maaracing_master.plugins.infinite_drive import flow_data

            text = SCORE_OK if roi == flow_data.RESULT_SCORE_ROI else PROGRESS_OK
            return type("R", (), {"text": text})()

    class _Capture:
        def frame_with_age(self):
            return (object(), 1, 2, 0.0)

    class _Ctx:
        capture = _Capture()

    from maaracing_master.plugins.infinite_drive import module as m

    monkeypatch.setattr(m, "RapidOcrEngine", _FakeOcr)
    monkeypatch.setattr(dashboard_store, "median_round_seconds", lambda: 150.0)
    saved = m.InfiniteDriveModule.CONFIG_UI
    try:
        mod = m.InfiniteDriveModule(None)
        mod.ctx = _Ctx()
        mod._read_settlement()
        fields = {f["key"]: f for g in m.InfiniteDriveModule.CONFIG_UI["groups"]
                  for f in g["fields"]}
        assert fields["round_total"]["unit_seconds"] == 150
    finally:
        m.InfiniteDriveModule.CONFIG_UI = saved   # 重建落在类属性上，测完还原
