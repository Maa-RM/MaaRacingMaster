# -*- coding: utf-8 -*-
"""显卡探针的回归锁：停顿抓拍的时序与产物。

抓拍要能回答「我方线程卡在哪」，就必须赶在 run 还在飞的时候把栈落盘——
外部 nvidia-smi 在驱动停摆期自己会被卡住（生产实测 844ms），所以栈优先、
且抓拍不占盯梢线程。正常短 run 与已结束的 run 都不得触发抓拍。
"""
import contextlib
import threading
import time

from maaracing_master.core import dml_lock
from maaracing_master.plugins.speedrush import gpu_probe
from maaracing_master.plugins.speedrush.gpu_probe import GpuProbe


def setup_function():
    dml_lock.RUN_WATCH.exit()          # 防御清场：防其他用例泄漏在飞标记


def teardown_function():
    dml_lock.RUN_WATCH.exit()


def _join_dump(p: GpuProbe) -> None:
    if p._dump_th is not None:
        p._dump_th.join(5.0)
        assert not p._dump_th.is_alive()


def test_stall_watch_dumps_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_probe, "_cmd_output",
                        lambda cmd: f"(外部取证 {cmd[-1]})\n")
    p = GpuProbe()
    p._trace_dir = tmp_path
    p._sink.bind_session(tmp_path, "20260101_000000_1")
    try:
        dml_lock.RUN_WATCH.enter("yolo")
        p._watch(time.perf_counter())               # 刚进 run：未超阈，不拍
        assert p._dumped is None
        time.sleep(0.25)
        p._watch(time.perf_counter())               # 超阈：拍现场
        _join_dump(p)
        p._watch(time.perf_counter())               # 同一次在飞：不重复拍
        files = list(tmp_path.glob("stall_*_yolo.txt"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        # 三问齐全，且我方线程栈排在外部取证之前
        assert text.index("faulthandler") < text.index("PERFORMANCE")
        assert text.index("PERFORMANCE") < text.index("pmon")
        assert "most recent call first" in text     # 栈真的落进去了
        assert "栈抓拍时在飞: yolo 已" in text
        assert "外部取证结束时在飞: yolo 已" in text
    finally:
        dml_lock.RUN_WATCH.exit()
        _join_dump(p)
        p._sink.flush(2.0)
        res = p._sink.close_session(2.0)
    assert res.ok


def test_stall_dump_stacks_first_without_blocking_watch(tmp_path, monkeypatch):
    """外部取证卡住时：盯梢立刻返回，且栈已落盘（旧实现两条都不成立）。"""
    gate = threading.Event()
    external_started = threading.Event()

    def stuck_cmd(cmd):
        external_started.set()
        gate.wait(3.0)
        return "(外部取证卡住)\n"

    monkeypatch.setattr(gpu_probe, "_cmd_output", stuck_cmd)
    p = GpuProbe()
    p._trace_dir = tmp_path
    dml_lock.RUN_WATCH.enter("moge")
    try:
        time.sleep(0.25)
        t0 = time.perf_counter()
        p._watch(time.perf_counter())
        assert time.perf_counter() - t0 < 0.1       # 盯梢不等外部调用
        assert external_started.wait(2.0)
        files = list(tmp_path.glob("stall_*_moge.txt"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "most recent call first" in text     # 外部还卡着，栈已在盘上
        assert "外部取证卡住" not in text
    finally:
        gate.set()
        dml_lock.RUN_WATCH.exit()
        _join_dump(p)


def test_stall_watch_fires_again_for_new_run(tmp_path, monkeypatch):
    """一场对局停顿 1~2 次：去重只压同一次在飞，不得把哨兵永久关掉。"""
    monkeypatch.setattr(gpu_probe, "_cmd_output", lambda cmd: "(外部取证)\n")
    p = GpuProbe()
    p._trace_dir = tmp_path
    try:
        for _ in range(2):
            dml_lock.RUN_WATCH.enter("yolo")
            time.sleep(0.25)
            p._watch(time.perf_counter())
            _join_dump(p)
            dml_lock.RUN_WATCH.exit()
        assert len(list(tmp_path.glob("stall_*_yolo.txt"))) == 2
    finally:
        dml_lock.RUN_WATCH.exit()
        _join_dump(p)


def test_stall_watch_resets_after_run_ends(tmp_path):
    p = GpuProbe()
    p._trace_dir = tmp_path
    dml_lock.RUN_WATCH.enter("moge")
    dml_lock.RUN_WATCH.exit()
    p._watch(time.perf_counter())                   # 已不在飞：不拍
    assert p._dumped is None
    assert p._dump_th is None
    assert list(tmp_path.glob("stall_*")) == []


# --------- 停顿窗口内的线程 CPU 时间双采样 ---------

@contextlib.contextmanager
def _inflight_thread(body, who="moge"):
    """body(stop) 在目标线程里跑；登记与摘除都由该线程自己做（记账按线程）。"""
    entered = threading.Event()
    stop = threading.Event()

    def run():
        dml_lock.RUN_WATCH.enter(who)
        entered.set()
        try:
            body(stop)
        finally:
            dml_lock.RUN_WATCH.exit()

    th = threading.Thread(target=run)
    th.start()
    assert entered.wait(2.0)
    try:
        yield stop
    finally:
        stop.set()
        th.join(3.0)


def _probe(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_probe, "_cmd_output", lambda cmd: "(外部取证)\n")
    p = GpuProbe()
    p._trace_dir = tmp_path
    return p


def _dump_now(p):
    time.sleep(0.25)                                # 越过在飞阈
    p._watch(time.perf_counter())
    _join_dump(p)
    files = list(p._trace_dir.glob("stall_*.txt"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


def test_cpu_probe_busy_thread(tmp_path, monkeypatch):
    """持续忙循环：测量窗口里抓拍线程在睡，目标线程独占核 → 判为占用 CPU。"""
    p = _probe(tmp_path, monkeypatch)

    def busy(stop):
        x = 0
        while not stop.is_set():
            x += 1

    with _inflight_thread(busy):
        text = _dump_now(p)
    assert "不具判读条件" not in text
    assert "持续占用 CPU 执行" in text
    assert "阶段=原生推理在飞" in text


def test_cpu_probe_blocked_thread(tmp_path, monkeypatch):
    """阻塞等待：CPU 时间几乎不增长，且必须明说分不出阻塞与就绪排队。"""
    p = _probe(tmp_path, monkeypatch)
    with _inflight_thread(lambda stop: stop.wait(2.0)):
        text = _dump_now(p)
    assert "不具判读条件" not in text
    assert "几乎没获得 CPU 执行时间" in text
    assert "ETW" in text


def test_cpu_probe_call_ends_midwindow(tmp_path, monkeypatch):
    """调用结束但线程还活着（生产形态：worker 跨观测复用）→ 样本跨越恢复点。"""
    p = _probe(tmp_path, monkeypatch)

    def end_call(stop):
        stop.wait(0.32)
        dml_lock.RUN_WATCH.exit()      # 本次调用结束，线程继续活着
        stop.wait(2.0)

    with _inflight_thread(end_call):
        text = _dump_now(p)
    assert "不具判读条件" in text
    assert "跨越恢复点" in text


def test_cpu_probe_unreadable_times(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_probe, "thread_times", lambda tid: None)
    p = _probe(tmp_path, monkeypatch)
    with _inflight_thread(lambda stop: stop.wait(2.0)):
        text = _dump_now(p)
    assert "不具判读条件" in text and "读不到" in text


def test_cpu_probe_tid_reuse_guard(tmp_path, monkeypatch):
    """线程创建时间不一致 = TID 被复用，两次读数不是同一条线程。"""
    calls = []

    def fake(tid):
        calls.append(tid)
        return (1000 + len(calls), 0.5)

    monkeypatch.setattr(gpu_probe, "thread_times", fake)
    p = _probe(tmp_path, monkeypatch)
    with _inflight_thread(lambda stop: stop.wait(2.0)):
        text = _dump_now(p)
    assert "TID 已被复用" in text


def test_cpu_probe_report_bands_and_phase():
    """判读分档与阶段标注（纯函数，不吃机器负载）。"""
    report = gpu_probe._cpu_probe_report
    entry = dml_lock.InFlight(since=1.0, who="yolo", native_id=123, run_since=None)
    first = (10.0, 777, 5.0)
    assert "已无在飞" in report(None, None, None, None)
    idle = report(entry, first, (10.15, 777, 5.0), entry)
    assert "阶段=等锁" in idle and "几乎没获得 CPU 执行时间" in idle
    assert "持续占用 CPU 执行" in report(entry, first, (10.15, 777, 5.14), entry)
    assert "不足以分流" in report(entry, first, (10.15, 777, 5.045), entry)
    running = entry._replace(run_since=1.5)
    assert "阶段=原生推理在飞" in report(running, first, (10.15, 777, 5.0), running)