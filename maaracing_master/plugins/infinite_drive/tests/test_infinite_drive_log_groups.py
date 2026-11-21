# -*- coding: utf-8 -*-
"""无限驾驶组归属行为验收（speedrush 同款线性组链）。

锁四件事：
1. _tlog 开组期间归当前组，组终态按组内 ERROR/WARNING 自动推导；
2. logger 无 group()（测试桩）时 _open_grp 回退 legacy 单行；
3. 停止路径显式 incomplete 与组内错误并存；
4. 线性组链：每个 group_start 都被下一个开组或收尾收口，不留 running；
   组号同步给导航图，图动作日志不落回散行。

结构锁本身在 tests/test_log_attribution.py + tests/log_sites.py，此处只验行为。
"""
from types import SimpleNamespace

from maaracing_master.core.logger import Logger
from maaracing_master.plugins.infinite_drive import module as imod


def _recs(lg):
    return [r for _s, r in lg._lines]


def _module():
    return imod.InfiniteDriveModule(None)


def test_tlog_routes_to_open_group_and_derives_warning(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(imod, "logger", lg)
    m = _module()
    m._open_grp("[极速狂飙] 模块启动", "session")
    m._tlog("已进入驾驶页")
    m._tlog("录制器启动失败", "WARNING")
    m._end_grp()
    recs = _recs(lg)
    assert [r["event_type"] for r in recs] == ["group_start", "log", "log", "group_end"]
    gid = recs[0]["group_id"]
    assert recs[1]["group_id"] == gid and recs[2]["group_id"] == gid
    assert recs[0]["kind"] == "session" and recs[0]["outcome"] == "running"
    end = recs[-1]
    assert end["outcome"] == "warning" and end["has_warning"] is True


def test_open_grp_falls_back_without_group_api(monkeypatch):
    class _Recorder:
        def __init__(self):
            self.lines = []

        def log(self, msg, level="INFO", channel=None):
            self.lines.append((level, str(msg)))

    rec = _Recorder()
    monkeypatch.setattr(imod, "logger", rec)
    m = _module()
    m._open_grp("[极速狂飙] 模块启动（录制开）", "session")
    assert rec.lines == [("INFO", "[极速狂飙] 模块启动（录制开）")]
    assert m._log_grp is None                                 # 不持有句柄
    m._tlog("普通行", "WARNING")
    assert ("WARNING", "普通行") in rec.lines


def test_stop_path_incomplete_keeps_error_flag(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(imod, "logger", lg)
    m = _module()
    m._open_grp("驾驶阶段 p1", "phase")
    m._tlog("深度模型初始化失败", "ERROR")
    m._end_grp("incomplete")
    end = [r for r in _recs(lg) if r["event_type"] == "group_end"][-1]
    assert end["outcome"] == "incomplete" and end["has_error"] is True


def test_linear_group_chain_no_dangling(tmp_path, monkeypatch):
    """模块启动 → 每轮 → 选卡 → 驾驶：开新组自动收旧组，start finally 收最后一组。"""
    lg = Logger(tmp_path)
    monkeypatch.setattr(imod, "logger", lg)
    m = _module()
    m._open_grp("模块启动", "session")
    m._open_grp("第 1 轮", "session")
    m._open_grp("选卡阶段 card1", "phase")
    m._open_grp("驾驶阶段 p1", "phase")
    m._tlog("已进入驾驶页")
    m._end_grp()                       # start finally：收最后一组
    m._end_grp("incomplete")           # 槽已空，必须 no-op，不产生迟到 end
    gend = [r for r in _recs(lg) if r["event_type"] == "group_end"]
    assert [r["title"] for r in gend] == ["模块启动", "第 1 轮", "选卡阶段 card1", "驾驶阶段 p1"]
    assert [r["outcome"] for r in gend] == ["success"] * 4
    assert lg._groups == {}


def test_group_id_synced_to_nav_graph(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(imod, "logger", lg)
    m = _module()
    seen = []
    m._graph = SimpleNamespace(set_log_group=seen.append)
    m._open_grp("模块启动", "session")
    assert seen[-1] == m._log_grp.id
    m._end_grp()
    assert seen[-1] is None


def test_start_opens_group_and_records_failure_terminal(tmp_path, monkeypatch):
    """启动段本身就是一张卡：图加载失败时日志归组、终态 failure，不留散行。"""
    lg = Logger(tmp_path)
    monkeypatch.setattr(imod, "logger", lg)

    class _Graph:
        def __init__(self, _ctx):
            pass

        def add_plugin(self, *_args):
            pass

        def load(self):
            return False

        def set_log_group(self, _gid):
            pass

    monkeypatch.setattr(imod, "NavGraph", _Graph)
    ctx = SimpleNamespace(connect=lambda: True, check_window_aspect=lambda: True)
    m = imod.InfiniteDriveModule(ctx)
    assert m.start() is False
    recs = _recs(lg)
    assert [r["event_type"] for r in recs] == ["group_start", "log", "group_end"]
    assert recs[0]["kind"] == "session" and "模块启动" in recs[0]["title"]
    assert recs[1]["group_id"] == recs[0]["group_id"]
    assert "导航图加载失败" in recs[1]["message"]
    assert recs[-1]["outcome"] == "failure"
    assert m._log_grp is None
