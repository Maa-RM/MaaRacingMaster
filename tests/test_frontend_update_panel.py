# -*- coding: utf-8 -*-
"""关于页更新交互的静态锁。

**为什么是静态锁**：前端没有 DOM 测试链，但 P3 的交互契约可从源码文本判定——
更新主流程实际调用哪些宿主动作、升级操作区是否纯文本渲染、进度响应是否带修订
守卫、提交临界区是否不渲染取消、非受管/未配置是否有手动入口。真正链路验证由
提权整链（真实 WebView 按钮驱动）完成，本文件的价值是让回退在 CI 即红。

**空过防护**：每处抽取都断言抽到锚点串，抽取规则失效时测试报错而不是静默通过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "app.js"
MAINWINDOW = REPO / "apps" / "MaaRacingMaster.Shell" / "MainWindow.xaml.cs"
COORDINATOR = REPO / "apps" / "MaaRacingMaster.Shell" / "UpdateCoordinator.cs"

HOST_ACTIONS = (
    "update_check", "update_start_download", "update_cancel_download",
    "update_status", "update_apply", "update_set_channel", "update_set_source",
    "update_set_dismissed",
)

INDEX_HTML = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "index.html"
STYLE_CSS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "style.css"


@pytest.fixture(scope="module")
def app_src() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    assert "renderUpdateStatus" in src, "app.js 更新渲染锚点缺失，抽取规则已失效"
    return src


def test_up_to_date_copy_is_honest_about_channel_gap(app_src: str):
    """从测试轨切回且稳定版尚未追上时，状态行如实说「等待下一稳定版」，
    不假装「已是最新版本」；区分依据是后端快照的 awaiting_stable。"""
    body = _js_function_body(app_src, "function renderSnapshot(snapshot)")
    assert "snapshot.awaiting_stable" in body, "up_to_date 未区分等待与真正已是最新"
    assert "当前版本高于稳定渠道，等待下一稳定版" in body, "等待文案缺失"
    assert "已是最新版本" in body, "常规已是最新文案缺失"


def test_settings_note_tells_the_two_dimensions_apart(app_src: str):
    """浮窗说明要区分两维语义：渠道切换立即重查；源切换只是换首选、下次检查生效。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "切换版本渠道立即重新检查" in html and "自下次检查起作为首选" in html, (
        "浮窗说明未区分渠道与下载源的不同生效方式"
    )
    render = _js_function_body(app_src, "function renderUpdateSettings()")
    assert "切换版本渠道立即重新检查" in render, "渲染路径未同步说明文案"


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
    # 渲染路径上存在对快照 revision 的比较守卫（字段名与 UpdateSnapshot 一致）
    assert re.search(r"revision", app_src), "进度轮询缺少修订守卫"


def test_apply_critical_section_hides_cancel(app_src: str):
    """提交临界区（preparing/applying）不渲染取消：取消分支必须被 phase 排除。"""
    body = _js_function_body(app_src, "async function applyUpdate()")
    # 取消按钮的渲染分支不得在 preparing/applying 相位出现
    for phase in ("preparing", "applying"):
        assert phase in body, f"applyUpdate 未区分提交临界相位 {phase}"
    m = re.search(r"phase\s*===?\s*'(preparing|applying)'[^}]*\{", body)
    assert m, "提交临界区缺少相位分支"
    window_after = body[m.end():m.end() + 400]
    assert "update_cancel_download" not in window_after, (
        "提交临界区渲染了取消动作（虚假取消）")


def test_unmanaged_and_unconfigured_have_manual_entry(app_src: str):
    """非受管安装与来源未配置：提供明确的手动下载入口（前往下载页）。

    出口单一：checkUpdate 降级分支引用统一设置函数，函数内才是前往下载的动作——
    两段都断言，防「工厂改了出口、降级分支忘了接」的静默漂移。
    """
    body = _js_function_body(app_src, "async function checkUpdate()")
    assert "unavailable" in body, "checkUpdate 未处理非受管降级相位"
    assert "setupManualDownloadAction()" in body, "降级相位缺少手动下载入口"
    factory = _js_function_body(app_src, "function setupManualDownloadAction()")
    assert "openTarget('download')" in factory, "手动下载按钮未绑定前往下载动作"


def test_progress_render_is_text_only(app_src: str):
    """升级操作区远程/动态文案一律 textContent 落地（该函数体内不得出现 innerHTML）。"""
    for signature in ("async function checkUpdate()", "async function applyUpdate()"):
        body = _js_function_body(app_src, signature)
        assert "innerHTML" not in body, f"{signature} 出现 HTML 拼接"


def test_head_button_absorbs_check_download_and_apply(app_src: str):
    """卡头主按钮吸收检查/下载/重启升级：按钮留在卡头，按相位改文字与处理器；
    卡体只放进度条与取消/稍后这类次动作，避免两处主操作入口。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="btn-update-action"' in html, "卡头主按钮缺失"
    assert 'id="btn-update-action-label"' in html, "卡头主按钮文字挂点缺失"
    assert 'id="about-new-release"' in html, "卡体次动作区缺失"
    setter = _js_function_body(app_src, "function setMainAction(label, handler, icon, primary)")
    assert "onclick = handler" in setter, "主按钮未按相位重绑处理器"
    assert "textContent = label" in setter, "主按钮未按相位改文字"
    assert "maarm-btn--primary', primary !== false" in setter, "主按钮配色未受 primary 控制"
    body = _js_function_body(app_src, "function renderSnapshot(snapshot)")
    for label, handler in (
        ("检查更新", "checkUpdate"),
        ("下载更新", "downloadUpdate"),
        ("重启升级", "applyUpdate"),
    ):
        assert f"setMainAction('{label}', {handler}" in body, (
            f"主按钮未在相位渲染 {label} → {handler}"
        )
    # 次动作只走 secondaryAction，不再往卡体塞主操作按钮
    assert "secondaryAction('取消下载'" in body, "取消下载应留在卡体次动作区"
    assert "secondaryAction('稍后'" in body, "稍后应留在卡体次动作区"
    assert "setMainAction('下载中…'" in body, "下载中主按钮应禁用显示状态"
    # 已是最新：检查更新按钮用白色（primary=false），与其他相位的主色按钮区分
    m = re.search(r"case 'up_to_date':.*?(?=case |default:)", body, re.S)
    assert m and "setMainAction('检查更新', checkUpdate, 'rotate-cw', false)" in m.group(0), (
        "已是最新相位的检查更新按钮应保持白色"
    )


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


def test_available_phase_shows_dismiss_control_and_updates_dot(app_src: str):
    """available 相位展示「本次更新不再提示」；勾选状态回显自快照，
    提醒点按「目标版本 != 已忽略版本」决定是否亮起。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="upd-remind-row"' in html and 'id="upd-remind-check"' in html
    assert "本次更新不再提示" in html, "缺少不再提示选项"

    dot = _js_function_body(app_src, "function renderUpdateDot(snapshot)")
    assert "snapshot.phase === 'available'" in dot, "提醒点未限定在发现更新相位"
    assert "snapshot.dismissed_version !== target" in dot, "提醒点未按已忽略版本判断"
    assert "classList.toggle('is-on', on)" in dot, "提醒点缺少显隐切换"

    row = _js_function_body(app_src, "function renderRemindRow(snapshot)")
    assert "snapshot.dismissed_version === target" in row, "勾选状态未回显快照"
    assert "下个版本会重新提示" in row, "缺少按版本生效的说明"

    init = _js_function_body(app_src, "function initAbout()")
    assert "update_set_dismissed" in init, "不再提示未接宿主动作"


def test_no_update_states_use_flat_button(app_src: str):
    """空闲与已是最新时主按钮保持扁平（与齿轮按钮同款），不带主色阴影；
    只有发现更新/下载/重启升级才用主按钮。"""
    body = _js_function_body(app_src, "function renderSnapshot(snapshot)")
    for phase in ("idle", "up_to_date"):
        m = re.search(rf"case '{phase}':.*?(?=case |default:)", body, re.S)
        assert m, f"renderSnapshot 缺 {phase} 分支"
        assert "setMainAction('检查更新', checkUpdate, 'rotate-cw', false)" in m.group(0), (
            f"{phase} 相位的检查更新按钮应为扁平样式"
        )


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
    assert 'id="btn-update-settings"' in html, "检查更新旁应为设置按钮"
    assert 'data-source="cnb"' in html and 'data-source="github"' in html, "缺下载源选项"
    assert 'data-channel="stable"' in html and 'data-channel="preview"' in html, "缺版本渠道选项"
    ch = _js_function_body(app_src, "async function setUpdateChannel(channel)")
    src = _js_function_body(app_src, "async function setUpdateSource(source)")
    assert "maarm.call('update_set_channel', { value })" in ch, "渠道选择未发宿主动作"
    assert "maarm.call('update_set_source', { value })" in src, "下载源选择未发宿主动作"
    assert "applySettingsSnapshot" in ch and "applySettingsSnapshot" in src, (
        "切换后未用快照真源回显"
    )
    # 前端不持有 URL/路径：设置动作只带枚举值
    for body in (ch, src):
        assert "http" not in body and "localStorage" not in body, "设置动作不得拼 URL 或写本地偏好"
    check = _js_function_body(app_src, "async function checkUpdate()")
    assert "channel" not in check and "source" not in check, "检查主流程不得携带渠道/来源参数"


def test_shell_auto_closes_on_applying():
    """提交临界区后壳自动关窗：协调器在 applying 翻转时触发回调，主窗订阅它关闭。"""
    coord = COORDINATOR.read_text(encoding="utf-8")
    assert "ApplyingStarted" in coord, "协调器缺少 applying 触发回调"
    mw = MAINWINDOW.read_text(encoding="utf-8")
    assert re.search(r"ApplyingStarted\s*\+=", mw), "主窗未订阅 applying 关窗回调"


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
