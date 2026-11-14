# -*- coding: utf-8 -*-
"""关于页更新交互的静态锁（升级适配 P3）。

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
    "update_status", "update_apply",
)


@pytest.fixture(scope="module")
def app_src() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    assert "renderUpdateStatus" in src, "app.js 更新渲染锚点缺失，抽取规则已失效"
    return src


def test_update_flow_calls_host_actions(app_src: str):
    """更新主流程实际调用全部五个宿主动作（不只搜按钮文字，核调用字面量）。"""
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

    出口单一：checkUpdate 降级分支引用统一按钮工厂，工厂内才是前往下载的动作——
    两段都断言，防「工厂改了出口、降级分支忘了接」的静默漂移。
    """
    body = _js_function_body(app_src, "async function checkUpdate()")
    assert "unavailable" in body, "checkUpdate 未处理非受管降级相位"
    assert "manualDownloadButton()" in body, "降级相位缺少手动下载入口"
    factory = _js_function_body(app_src, "function manualDownloadButton()")
    assert "openTarget('download')" in factory, "手动下载按钮未绑定前往下载动作"


def test_progress_render_is_text_only(app_src: str):
    """升级操作区远程/动态文案一律 textContent 落地（该函数体内不得出现 innerHTML）。"""
    for signature in ("async function checkUpdate()", "async function applyUpdate()"):
        body = _js_function_body(app_src, signature)
        assert "innerHTML" not in body, f"{signature} 出现 HTML 拼接"


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
