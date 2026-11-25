# -*- coding: utf-8 -*-
"""关于页更新卡交互的静态锁。

**为什么是静态锁**：页面装载验收只覆盖「检查更新」这一条按钮通路，其余契约仍可从源码
文本判定——更新主流程实际调用哪些宿主动作、相位到视图的映射、进度响应是否带修订守卫、
提交临界区是否不给取消、非受管是否有手动入口、动作的主次分级。真正链路验证由提权整链
（真实 WebView 按钮驱动）完成，本文件的价值是让回退在 CI 即红。

**结构**：渲染被拆成两张表——`describe()`（相位 → 图标/标题/说明/动作/附加区）与
`renderSnapshot()`（把表搬上屏）。断言尽量落在 `describe()` 上，它才是文案与按钮语义的
真源；`renderSnapshot` 只锁「表确实被搬上屏」。

**空过防护**：每处抽取都断言抽到锚点串，抽取规则失效时测试报错而不是静默通过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "about-update.js"
SHELL_APP_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "app.js"
RPC_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "rpc.js"
MAINWINDOW = REPO / "apps" / "MaaRacingMaster.Shell" / "MainWindow.xaml.cs"
COORDINATOR = REPO / "apps" / "MaaRacingMaster.Shell" / "UpdateCoordinator.cs"

HOST_ACTIONS = (
    "update_check", "update_start_download", "update_cancel_download",
    "update_status", "update_apply", "update_set_channel", "update_set_source",
    "update_set_dismissed", "update_set_auto_check",
)

INDEX_HTML = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "index.html"
STYLE_CSS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "style.css"


@pytest.fixture(scope="module")
def app_src() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    assert "function describe(snapshot)" in src, "about-update.js 相位表缺失，抽取规则已失效"
    return src


def _case(view_src: str, phase: str) -> str:
    """取出 describe() 里某个 case 分支（到下一个 case / default 为止）。"""
    m = re.search(rf"case '{phase}':(.*?)(?=\n      case |\n      default:)", view_src, re.S)
    assert m, f"describe() 缺 {phase} 分支"
    return m.group(1)


def test_up_to_date_copy_is_honest_about_channel_gap(app_src: str):
    """从测试轨切回且稳定版尚未追上时如实说「等待下一稳定版」，不假装「已是最新版本」；
    区分依据是后端快照的 awaiting_stable。"""
    view = _js_function_body(app_src, "function describe(snapshot)")
    branch = _case(view, "up_to_date")
    assert "snapshot.awaiting_stable" in branch, "up_to_date 未区分等待与真正已是最新"
    assert "当前版本高于稳定渠道" in branch and "等待下一稳定版" in branch, "等待文案缺失"
    assert "已是最新版本" in branch, "常规已是最新文案缺失"


def test_settings_note_tells_the_two_dimensions_apart():
    """浮窗说明要区分两维语义：渠道切换立即重查；源切换只是换首选、下次检查生效。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "切换版本渠道立即重新检查" in html and "自下次检查起作为首选" in html, (
        "浮窗说明未区分渠道与下载源的不同生效方式"
    )


def test_update_flow_calls_host_actions(app_src: str):
    """更新主流程实际调用全部宿主动作（不只搜按钮文字，核调用字面量）。
    含渠道/源切换两个设置动作——它们带枚举值，但仍是固定意图。"""
    for action in HOST_ACTIONS:
        assert re.search(rf"maarm\.call\(\s*'{action}'", app_src), f"缺少宿主动作调用: {action}"


def test_update_flow_no_legacy_call(app_src: str):
    """关于页检查动作只有协调器一套：主流程不再调用业务侧远程版本检查。"""
    assert "maarm.call('check_update'" not in app_src, (
        "关于页检查已切宿主协调器，不得残留业务侧 check_update 调用（第二套版本来源）")


def test_update_status_polling_uses_revision_guard(app_src: str):
    """进度轮询带修订守卫：旧响应不得回退进度（revision 只前进才渲染）。"""
    render = _js_function_body(app_src, "function renderSnapshot(snapshot)")
    assert "snapshot.revision < lastUpdateRevision" in render, "渲染入口缺修订守卫"


def test_current_version_comes_from_shared_state(app_src: str):
    """「当前版本」取 get_initial_state 落到共享 state 的版本，不从 DOM 回捞——
    hero 已不展示版本号，DOM 里没有可捞的东西。"""
    kernel = _js_function_body(app_src, "function currentVersion()")
    assert "state.version" in kernel, "当前版本未取共享 state（会退化成抓 DOM 文本）"
    shell = SHELL_APP_JS.read_text(encoding="utf-8")
    assert "state.version = data.version" in shell, "app.js 未把版本落到共享 state"
    rpc = RPC_JS.read_text(encoding="utf-8")
    assert "version: ''" in rpc, "共享 state 未声明 version 字段"


def test_apply_critical_section_hides_cancel(app_src: str):
    """提交临界区（preparing/applying）不渲染取消：这两个分支没有附加区与取消动作。"""
    view = _js_function_body(app_src, "function describe(snapshot)")
    m = re.search(r"case 'preparing':(.*?)\n      default:", view, re.S)
    assert m, "describe() 缺提交临界区分支（preparing/applying）"
    critical = m.group(1)
    assert "case 'applying':" in critical, "preparing 分支未与 applying 合流"
    assert "detail" not in critical, "提交临界区仍渲染附加区（虚假取消）"
    assert "cancelDownload" not in critical, "提交临界区渲染了取消动作"
    assert "disabled: true" in critical, "提交临界区主按钮未禁用"


def test_unmanaged_install_offers_manual_entry(app_src: str):
    """非受管安装：动作明确给出「前往下载」，而不是只留一句错误。"""
    view = _js_function_body(app_src, "function describe(snapshot)")
    branch = _case(view, "unavailable")
    assert "前往下载" in branch, "非受管安装缺少手动下载入口"
    assert "openTarget('download')" in branch, "手动下载按钮未绑定前往下载动作"


def test_progress_render_is_text_only(app_src: str):
    """附加区与动作区的动态文案一律 textContent / DOM API 落地（不得出现 innerHTML）。"""
    for signature in ("function renderDetail(kind, snapshot)", "async function checkUpdate()",
                      "async function applyUpdate()"):
        body = _js_function_body(app_src, signature)
        assert "innerHTML" not in body, f"{signature} 出现 HTML 拼接"


def test_action_slot_carries_phase_specific_intent(app_src: str):
    """一个按钮槽按相位换动作与主次：推进流程的（下载 / 重启）是主按钮，
    检查 / 前往下载 / 重新检查是次按钮——红底不再随相位乱跳。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="btn-update-action"' in html, "更新卡动作按钮缺失"
    assert 'id="btn-update-action-label"' in html, "动作按钮文字挂点缺失"
    assert 'id="upd-detail"' in html, "附加区挂点缺失"

    setter = _js_function_body(app_src, "function setAction(view)")
    assert "action.onclick = spec.run" in setter, "动作按钮未按相位重绑处理器"
    assert "label.textContent = spec.label" in setter, "动作按钮未按相位改文字"
    assert "primary === true" in setter, "主按钮配色未显式受 primary 控制"

    view = _js_function_body(app_src, "function describe(snapshot)")
    for phase, handler in (("idle", "checkUpdate"), ("available", "downloadUpdate"),
                           ("ready", "applyUpdate")):
        assert handler in _case(view, phase), f"{phase} 分支未绑定 {handler}"
    # 只有推进流程的两步用主按钮
    assert "primary: true" in _case(view, "available"), "下载应为主按钮"
    assert "primary: true" in _case(view, "ready"), "重启升级应为主按钮"
    for phase in ("idle", "up_to_date", "unavailable"):
        assert "primary: true" not in _case(view, phase), f"{phase} 不该用主按钮"


def test_available_phase_shows_dismiss_control_and_updates_dot(app_src: str):
    """available 相位展示「本次更新不再提示」；勾选状态回显自快照，
    提醒点按「目标版本 != 已忽略版本」决定是否亮起。"""
    view = _js_function_body(app_src, "function describe(snapshot)")
    assert "detail: 'remind'" in _case(view, "available"), "发现更新相位未挂提醒勾选"

    detail = _js_function_body(app_src, "function renderDetail(kind, snapshot)")
    assert "本次更新不再提示" in detail, "缺少不再提示选项"
    assert "下个版本会重新提示" in detail, "缺少按版本生效的说明"

    dot = _js_function_body(app_src, "function renderUpdateDot(snapshot)")
    assert "snapshot.phase === 'available'" in dot, "提醒点未限定在发现更新相位"
    assert "snapshot.dismissed_version !== target" in dot, "提醒点未按已忽略版本判断"
    assert "classList.toggle('is-on', on)" in dot, "提醒点缺少显隐切换"

    dismissed = _js_function_body(app_src, "async function setDismissed(value)")
    assert "update_set_dismissed" in dismissed, "不再提示未接宿主动作"


def test_available_error_keeps_a_download_retry(app_src: str):
    """取消下载 / 下载启动被拒后，宿主仍持有已确认目标：卡片要给「重试下载」，
    不能只剩「重新检查」把玩家赶回检查流程（本轮 GPT 验收指出的回退）。"""
    branch = _case(_js_function_body(app_src, "function describe(snapshot)"), "available")
    assert "重试下载" in branch, "下载未完成时缺少重试入口"
    assert "downloadUpdate" in branch, "重试入口未接下载动作"
    assert "label: '重新检查'" not in branch, "下载未完成不该只给重新检查"


def test_unmanaged_or_unconfigured_falls_back_to_manual_download(app_src: str):
    """非受管安装与来源未配置：自动升级这条路本来不通，给手动下载入口，
    而不是让「重新检查」反复刷同一句错误。"""
    branch = _case(_js_function_body(app_src, "function describe(snapshot)"), "idle")
    assert "snapshot.managed === false" in branch, "未按 managed 判非受管"
    assert "未配置" in branch, "未按「来源未配置」判手动入口"
    assert "前往下载" in branch and "openTarget('download')" in branch, "缺少手动下载动作"


def test_version_arrival_rerenders_the_card(app_src: str):
    """update_status 可能先于 get_initial_state 回包：版本落地后要重画一次卡片，
    否则标题栏已经有版本号、卡片却停在「当前版本」。"""
    assert "refreshUpdateCard" in app_src, "更新卡未暴露版本到位后的重画入口"
    shell = SHELL_APP_JS.read_text(encoding="utf-8")
    assert "window.MaaRM.refreshUpdateCard()" in shell, "app.js 版本落地后未重画更新卡"


def test_about_tab_dot_is_anchored_to_label_text():
    """提醒点锚在「关于」文字外框上（不是整个按钮），并带两圈错峰扩散的圆环。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'class="maarm-tab-label"' in html and 'id="about-tab-dot"' in html, (
        "关于标签缺少文字外框或提醒点挂点"
    )
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".maarm-tab-label { position: relative" in css, "提醒点未锚定到文字外框"
    assert "maarm-tab-dot-echo" in css and "animation-delay: -1.2s" in css, (
        "提醒点缺少与底栏状态球同款的两圈错峰扩散"
    )


def test_status_text_meets_contrast_and_lives_in_live_region():
    """状态行是更新卡唯一的结果出口：颜色取深化 token（白底 ≥4.5:1），
    并且只在相位切换时播报（下载轮询不刷屏）。"""
    css = STYLE_CSS.read_text(encoding="utf-8")
    for token in ("--maarm-success-strong", "--maarm-danger-strong", "--maarm-primary-strong"):
        assert token in css, f"缺少深化色 token: {token}"
    assert ".upd-meta--err { color: var(--maarm-danger-strong)" in css, (
        "错误说明行未使用可读的深化色"
    )
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'role="status"' in html and 'aria-live="polite"' in html, "状态区未进无障碍树"
    app = APP_JS.read_text(encoding="utf-8")
    assert "phase === lastPhase ? 'off' : 'polite'" in app, "同相位刷新未关闭播报（进度会刷屏）"


def test_progress_bar_has_progressbar_semantics(app_src: str):
    """下载进度条补 progressbar 语义：读屏能拿到当前百分比。"""
    detail = _js_function_body(app_src, "function renderDetail(kind, snapshot)")
    assert "'progressbar'" in detail, "进度条缺 role=progressbar"
    assert "aria-valuenow" in detail, "进度条缺 aria-valuenow"


def test_primary_button_rules_override_base_button():
    """主按钮规则必须排在基础按钮之后：同权重下提前定义会被背景/文字/边框覆盖，
    只剩一层红阴影（历史 bug）。"""
    css = STYLE_CSS.read_text(encoding="utf-8")
    base = css.index("\n.maarm-btn {")
    primary = css.index("\n.maarm-btn--primary {")
    assert primary > base, "主按钮规则被基础按钮覆盖，只剩红阴影"
    assert ".maarm-btn--primary:hover:not(:disabled)" in css, (
        "主按钮 hover 权重不足，会被基础按钮的 hover 背景盖掉"
    )


def test_settings_popover_wires_to_host_actions(app_src: str):
    """设置浮窗含下载源与版本渠道两组；选择发固定宿主动作（只带枚举值），
    真源回显来自快照，检查主流程本身仍不带渠道/来源参数。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="btn-update-settings"' in html, "更新卡应有设置入口"
    assert 'data-source="cnb"' in html and 'data-source="github"' in html, "缺下载源选项"
    assert 'data-channel="stable"' in html and 'data-channel="preview"' in html, "缺版本渠道选项"
    ch = _js_function_body(app_src, "async function setUpdateChannel(channel)")
    src = _js_function_body(app_src, "async function setUpdateSource(source)")
    assert "maarm.call('update_set_channel', { value })" in ch, "渠道选择未发宿主动作"
    assert "maarm.call('update_set_source', { value })" in src, "下载源选择未发宿主动作"
    # 设置回包必须经同一渲染入口：卡头文案、动作与提醒点都要跟着快照走
    assert "renderSnapshot(d)" in ch, "渠道回包未落到统一渲染入口"
    assert "renderSnapshot(d)" in src, "下载源回包未落到统一渲染入口"
    # 临界区/非受管拒绝时要当场说明，不能让设置看起来生效了
    assert "showError(d.error)" in ch and "showError(d.error)" in src, "设置被拒未给反馈"
    render = _js_function_body(app_src, "function renderSnapshot(snapshot)")
    assert "applySettingsSnapshot(snapshot)" in render, "统一渲染入口未回显渠道/源/开关"
    # 前端不持有 URL/路径：设置动作只带枚举值
    for body in (ch, src):
        assert "http" not in body and "localStorage" not in body, "设置动作不得拼 URL 或写本地偏好"
    check = _js_function_body(app_src, "async function checkUpdate()")
    assert "channel" not in check and "source" not in check, "检查主流程不得携带渠道/来源参数"


def test_auto_check_switch_and_push_share_one_render_path(app_src: str):
    """自动检查开关与后台推送：开关走宿主动作并经 renderSnapshot 回显；
    推送订阅在页面初始化、经同一修订守卫落盘（初次 update_status 可补回错过推送）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="upd-auto-check"' in html, "设置浮窗缺自动检查开关"
    init = _js_function_body(app_src, "function initAbout()")
    assert "update_set_auto_check" in init, "自动检查开关未接宿主动作"
    assert "renderSnapshot(await maarm.call('update_set_auto_check'" in init, (
        "开关回包未经统一渲染入口"
    )
    listen = _js_function_body(app_src, "maarm.onNativeMessage((msg) =>")
    assert "'update-status'" in listen, "未订阅后台更新快照推送"
    assert "renderSnapshot(msg.data)" in listen, "推送未经统一渲染入口"
    render = _js_function_body(app_src, "function renderSnapshot(snapshot)")
    assert "snapshot.revision < lastUpdateRevision" in render, "推送路径缺修订守卫"


def test_download_and_dismiss_carry_seen_revision(app_src: str):
    """下载与忽略带渲染时的修订号：后台已换目标时由后端拒绝，不替旧界面操作新目标。"""
    download = _js_function_body(app_src, "async function downloadUpdate()")
    assert "maarm.call('update_start_download', { revision: lastUpdateRevision })" in download, (
        "下载未带玩家看到的修订号"
    )
    dismissed = _js_function_body(app_src, "async function setDismissed(value)")
    assert "revision: lastUpdateRevision" in dismissed, "忽略操作未带玩家看到的修订号"


def test_manual_check_failure_restores_main_button(app_src: str):
    """检查失败不得把按钮留在禁用的「检查中…」：catch 分支先按快照恢复，再写失败原因。"""
    body = _js_function_body(app_src, "async function checkUpdate()")
    catch = body[body.rindex("} catch (e) {"):]
    assert "await refreshUpdateStatus()" in catch, "失败分支未恢复快照（按钮会卡在检查中）"
    assert "paintFailure(" in catch, "失败分支未显示错误"


def test_shell_auto_closes_on_applying():
    """提交临界区后壳自动关窗：协调器在 applying 翻转时触发回调，主窗订阅它关闭。"""
    coord = COORDINATOR.read_text(encoding="utf-8")
    assert "ApplyingStarted" in coord, "协调器缺少 applying 触发回调"
    mw = MAINWINDOW.read_text(encoding="utf-8")
    assert re.search(r"ApplyingStarted\s*\+=", mw), "主窗未订阅 applying 关窗回调"


def test_announcement_refresh_follows_update_check_and_tab_entry(app_src: str):
    """公告刷新以更新检查为主，切进关于页补一次（受间隔约束）。

    **判据**：公告只在发版时变；每次切进关于页都全量重拉，弱网下会把卡片清空再重建
    （玩家看到的就是「一直在刷新」），还白打三条 CDN。所以主刷新与版本检查同源：启动
    一次，之后每次检查收尾（手动检查 / 后台自动检查推送）再拉一次；切页只做一次
    受最小间隔约束的补拉——普通 ZIP 没有后台检查，启动网络不通时它是唯一的重试入口。
    """
    shell = SHELL_APP_JS.read_text(encoding="utf-8")
    switch = _js_function_body(shell, "function switchTab(name)")
    assert "pollDashboard()" in switch, "switchTab 抽取结果可疑，静态锁会空过"
    assert "fetchAnnouncement()" in switch, "切进关于页缺少公告补拉（非受管安装没有重试入口）"
    assert "force" not in switch, "切页补拉不得绕过最小间隔"

    check = _js_function_body(app_src, "async function checkUpdate()")
    assert "fetchAnnouncement({ force: true })" in check, "检查更新收尾未刷新公告"
    listen = _js_function_body(app_src, "maarm.onNativeMessage((msg) =>")
    assert "fetchAnnouncement()" in listen, "后台检查推送未刷新公告"

    fetch = _js_function_body(app_src, "async function fetchAnnouncement(opts)")
    assert "ANNOUNCE_RETRY_GAP_MS" in fetch, "上次失败后的补拉间隔缺失，切页重试会被吞掉"


def test_dismiss_checkbox_updates_in_place(app_src: str):
    """勾选「本次更新不再提示」会回一张新快照：同一目标原地更新勾选状态，不重建节点——
    重建会把键盘焦点从复选框换到 body 上。"""
    detail = _js_function_body(app_src, "function renderDetail(kind, snapshot)")
    assert "dataset.kind === 'remind'" in detail, "缺少提醒区的原地更新分支"
    assert "dataset.target === String(snapshot.target_version)" in detail, (
        "原地更新未按目标版本区分，换目标后仍复用旧节点")
    assert 'input[type="checkbox"]' in detail, "原地更新未找回原复选框"


def test_announcement_rerender_skips_unchanged_payload(app_src: str):
    """同内容不重建卡片、拉取失败不撤掉已显示的卡片——重复刷新不该表现为界面闪一下。"""
    body = _js_function_body(app_src, "async function fetchAnnouncement(opts)")
    assert "'fetch_announcement'" in body, "公告 RPC 锚点缺失，静态锁会空过"
    assert "=== announceShown" in body, "缺内容去重：同内容也会重建卡片"
    assert "bodyEl.textContent = ''" not in body, "拉取前清空容器会让卡片先闪一下空白"


def _js_function_body(src: str, signature: str) -> str:
    """按大括号配对取出 JS 函数体（含首尾大括号），与远程渲染锁同一抽取规则。"""
    start = src.index(signature)
    line_end = src.index("\n", start)
    brace = src.index("{", start)
    assert brace < line_end, f"{signature} 的开体大括号不在签名行，抽取规则已失效"
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError(f"{signature} 未找到闭合大括号，抽取规则已失效")


def test_progress_updates_in_place(app_src: str):
    """下载进度每 500ms 一拍：已处于进度态时就地改宽度/百分比，不重建容器——
    重建会把「取消」按钮连同焦点一起换掉。"""
    body = _js_function_body(app_src, "function renderDetail(kind, snapshot)")
    assert "detail.dataset.kind === 'progress'" in body, "进度态未识别，仍会每拍重建"
    assert "aria-valuenow" in body and ".style.width" in body, "就地更新未覆盖数值与宽度"
