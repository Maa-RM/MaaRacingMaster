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
    assert "BOOT_SURFACES" in app, "弹窗预热必须是集中维护的清单，便于新增入口时补登记"
    assert "环境中心" in app, "环境中心曾因漏登记在首次打开时卡顿，必须留在预热清单里"


def test_marker_only_written_when_warmup_complete():
    """完成标记只代表「这次真的画完了」：失败、超时或时限兜底都留在冷态。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert "BOOT_TOTAL_MS" in app, "冷启动必须有从脚本开始算的总时限"
    assert "withDeadline(" in app, "单步等待必须受总时限约束"
    assert "armBootDeadline()" in app, "初始化本身可能挂住，必须有不依赖它的兜底放行"
    assert "if (ok && !_bootCutShort" in app, \
        "通知壳落标记必须被「全部成功」条件包住"
    assert "_bootCutShort = true" in app
    assert "本次不写完成标记" in app, "超时兜底只放行界面，不得写标记"


def test_env_center_placeholder_is_not_treated_as_ready():
    app = APP.read_text(encoding="utf-8-sig")
    assert "正在读取" in app, "环境中心的占位文案不能算作就绪，否则预热等于没等"


def test_warmup_closes_only_its_own_dialogs():
    """预热不得按 Esc 关最顶层弹窗：那会误关启动体检，也拦不住迟到的弹窗。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert "useBootModal" in app and "_bootModals" in app
    assert "closeTopModal" not in app, "禁止恢复「按 Esc 关最顶层」的旧做法"
    for api in ("openManageModal", "openDataUsage", "openOptimizerCenter", "showVigemDialog"):
        assert api in app, f"预热应通过 {api} 拿到自己打开的弹窗句柄"
    run = (SHELL / "frontend" / "js" / "run.js").read_text(encoding="utf-8-sig")
    settings = (SHELL / "frontend" / "js" / "settings.js").read_text(encoding="utf-8-sig")
    assert "openManageModal" in run, "模块管理弹窗必须把句柄交给调用方"
    assert "openDataUsage" in settings and "openOptimizerCenter" in settings
