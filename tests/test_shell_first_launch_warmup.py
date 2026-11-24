# -*- coding: utf-8 -*-
"""首次启动预热遮罩的源码锁：冷启动判定、遮罩按查询参数显隐、预热步骤与标记回写。

动态行为（遮罩停留时长、步骤推进、标记落盘）由真机冷/热两次启动实测；静态锁只防
接线回退：去掉查询参数会让热启动也闪遮罩，去掉标记回写会让每次启动都重播预热。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHELL = REPO / "apps" / "MaaRacingMaster.Shell"
MAIN = SHELL / "MainWindow.xaml.cs"
WARMUP = SHELL / "RenderWarmup.cs"
INDEX = SHELL / "frontend" / "index.html"
APP = SHELL / "frontend" / "app.js"
CSS = SHELL / "frontend" / "style.css"


def test_cold_detection_and_marker_roundtrip():
    warm = WARMUP.read_text(encoding="utf-8-sig")
    assert "IsCold" in warm and "MarkDone" in warm, "冷启动判定与标记回写必须在同一入口"
    assert ".render-warmup.json" in warm, "预热标记文件名是跨启动复用的唯一凭据"
    main = MAIN.read_text(encoding="utf-8-sig")
    assert "RenderWarmup.IsCold" in main, "初始化时必须先判定冷启动"
    assert "warmup=1" in main, "冷启动必须把 warmup=1 带进页面地址"
    assert "warmup-done" in main, "壳必须处理前端预热完成消息"


def test_overlay_hidden_before_first_paint_on_warm_start():
    html = INDEX.read_text(encoding="utf-8-sig")
    assert 'id="boot-overlay"' in html
    assert "boot-skip" in html, "热启动必须在首帧前隐藏遮罩（不能靠脚本来回切换）"
    css = CSS.read_text(encoding="utf-8-sig")
    assert ".boot-skip .boot-overlay" in css


def test_warmup_steps_have_floor_and_failure_isolation():
    app = APP.read_text(encoding="utf-8-sig")
    assert "runBootWarmup" in app
    assert "BOOT_MIN_MS" in app, "遮罩必须有保底显示时长，防止一闪而过"
    assert "warmup-done" in app, "预热完成必须通知壳落标记"
    assert "console.error('[warmup]'" in app, "单步失败只记日志，不得阻断收尾"
    assert "await pollDashboard()" in app, (
        "数据页预热必须等看板数据真正渲染：只切页面不拉数据，首次点击仍会卡")
