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
    assert "warmupSurfaces" in app, "启动入口只跑注册表；弹窗预热不再维护中央清单"
    assert "registerWarmup" in app, "入口自己拥有的预热面也要走注册"


def test_each_feature_registers_its_own_warmup_surface():
    """新增弹窗时应改它自己的模块，而不是回来改启动入口。"""
    for name in ("run.js", "settings.js", "env-center.js", "achievement.js"):
        src = (SHELL / "frontend" / "js" / name).read_text(encoding="utf-8-sig")
        assert "registerWarmup" in src, f"{name} 应自己登记预热面"
    env = (SHELL / "frontend" / "js" / "env-center.js").read_text(encoding="utf-8-sig")
    assert "环境中心" in env, "环境中心必须登记预热面（漏了首次打开会卡）"


def test_marker_only_written_when_warmup_complete():
    """完成标记只代表「这次真的画完了」：失败、超时或时限兜底都留在冷态。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert "BOOT_TOTAL_MS" in app, "冷启动必须有从脚本开始算的总时限"
    assert "withDeadline(" in app, "单步等待必须受总时限约束"
    assert "armBootDeadline()" in app, "初始化本身可能挂住，必须有不依赖它的兜底放行"
    assert "finishBoot(ok && !_bootCutShort)" in app, \
        "通知壳落标记必须被「全部成功」条件包住"
    assert "_bootCutShort = true" in app
    assert "_initOk = true" in app and "if (!_initOk)" in app, (
        "初始化失败时预热没有意义，也不得写完成标记")
    assert "本次不写完成标记" in app, "超时兜底只放行界面，不得写标记"


def test_boot_release_runs_exactly_once():
    """时限兜底与正常收尾是两条路径：晚到的初始化不得再收一遍尾（会重开体检弹窗、
    并把用户已经打开的弹窗顺手关掉）。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert app.count("function finishBoot(completed)") == 1, "收尾入口应只有一个"
    start = app.index("function finishBoot(completed)")
    body = app[start:start + 500]
    assert "if (_bootFinalized) return;" in body, "收尾入口缺少一次性守卫"
    assert "_bootFinalized = true" in body, "收尾入口未置位"
    assert "if (_bootFinalized) return;" in app[app.index("async function runBootWarmup"):], (
        "预热主流程在放行后必须停下，不得继续翻页/改文案/二次收尾")


def test_env_center_placeholder_is_not_treated_as_ready():
    env = (SHELL / "frontend" / "js" / "env-center.js").read_text(encoding="utf-8-sig")
    assert "正在读取" in env, "环境中心的占位文案不能算作就绪，否则预热等于没等"
    assert "失败" in env, "回错写的是「…失败」文案，不能被当成内容已就绪"
    assert "t.indexOf('失败')" in env, "就绪判据必须显式排除失败文案"


def test_late_reply_does_not_open_dialog_after_release():
    """遮罩被总时限放行后，迟到的回包不得再开一张抢焦点的预热弹窗。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert "warmupReleased" in app, "入口必须把「遮罩已放行」暴露给预热面"
    st = (SHELL / "frontend" / "js" / "settings.js").read_text(encoding="utf-8-sig")
    assert "warmupReleased" in st, "数据占用预热必须在回包后、开窗前检查放行状态"


def test_warmup_closes_only_its_own_dialogs():
    """预热不得按 Esc 关最顶层弹窗：那会误关启动体检，也拦不住迟到的弹窗。"""
    files = {
        name: (SHELL / "frontend" / name).read_text(encoding="utf-8-sig")
        for name in ("app.js", "js/run.js", "js/settings.js", "js/env-center.js", "js/achievement.js")
    }
    for name, src in files.items():
        assert "closeTopModal" not in src, f"{name} 不得恢复「按 Esc 关最顶层」的旧做法"
    for name in ("js/run.js", "js/settings.js", "js/env-center.js"):
        assert "finally" in files[name] and "modal.close()" in files[name], (
            f"{name} 的预热面必须用句柄在 finally 里关闭自己打开的弹窗")
    assert "openManageModal" in files["js/run.js"], "模块管理弹窗必须把句柄交给调用方"
    assert "openDataUsage" in files["js/settings.js"]
    assert "openOptimizerCenter" in files["js/env-center.js"]
    assert "showVigemDialog" in files["app.js"]


def test_warmup_blocks_tab_switch_and_releases_dialogs():
    """遮罩期间键盘也能点 tab：切页会与预热抢页面状态，顶栏与内容会错位；放行时
    也不能留着预热自己开的窗——否则弹窗晚于遮罩关闭、在已经可用的界面上抢焦点。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert "if (!_bootReleased) return;" in app, "预热期间未拦住切页"
    # 两条放行路径共用同一个收尾入口：closeAllModals 只在 finishBoot 里出现一次
    assert app.count("window.MaaRM.closeAllModals()") == 1, (
        "预热弹窗收口应集中在唯一收尾入口，避免两条路径各收一遍")
    modal = (SHELL / "frontend" / "js" / "modal.js").read_text(encoding="utf-8-sig")
    assert "closeAllModals" in modal and "openDialogs" in modal, "弹窗层缺少统一收口"
    env = (SHELL / "frontend" / "js" / "env-center.js").read_text(encoding="utf-8-sig")
    assert "warmupReleased()" in env, "环境中心开窗前未检查遮罩是否已放行"


def test_overlay_blocks_keyboard_focus_to_hidden_ui():
    """遮罩挡得住鼠标但拦不住 Tab：焦点会落在被遮住的「开始」上，回车就启动任务。
    遮罩期间把顶栏/正文/底栏设为 inert，收尾再放开。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert "setShellInert(true)" in app and "setShellInert(false)" in app, (
        "遮罩期间未隔离键盘焦点，收尾也未恢复")
    body = app[app.index("function setShellInert(on)"):][:400]
    assert "inert = on" in body, "inert 未落到顶栏/正文/底栏"
    assert "document.querySelector('main')" in body, "正文未纳入键盘隔离"


def test_cold_detection_requires_shader_cache_entries():
    """字节数不能单独作判据：清掉条目后 data_* 块仍可能超过 512KB 阈值，
    会把真正的冷启动判成热启动（跳过预热，首访继续卡）。"""
    warm = WARMUP.read_text(encoding="utf-8-sig")
    assert "MinGpuCacheEntries" in warm, "冷态判据缺少条目数下限"
    assert 'StartsWith("f_"' in warm, "条目统计未按 f_* 目录条目识别"


def test_hot_start_seeds_the_release_flag():
    """热启动没有遮罩，但「已放行」标志必须从一开始为真：否则 switchTab 的遮罩拦截
    会把四个 tab 一起挡死（标记写入后的第二次启动起必现）。"""
    app = APP.read_text(encoding="utf-8-sig")
    assert ("let _bootReleased = document.documentElement.classList.contains('boot-skip');"
            in app), "放行标志未按 boot-skip 初始化：热启动 tab 会全部点不动"
