# -*- coding: utf-8 -*-
"""应用自动检查更新（后台检查 / 调度 / 并发 / 短预算 / 修订准入）的契约锁。

**为什么单独成文件**：这套行为跨壳层四个部件——协调器的检查事务、进程内调度器、
偏好落盘、原生推送与前端渲染入口；静态锁只钉「入口与形状不被改回」，真实语义
（请求计数、取消时序、预算、锚点）由合同探针 `-- auto` 模式承担，它直连本仓库
的 `UpdateCoordinator.cs` / `UpdateAutoCheck.cs` 与便携源夹具：

    dotnet run --project apps/MaaRacingMaster.Shell/tools/ShellContractProbe \\
        -c Debug -- auto

**空过防护**：每处抽取都锚定具体字面量，规则失效时报错而不是静默通过。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SHELL = REPO / "apps" / "MaaRacingMaster.Shell"
COORD = SHELL / "UpdateCoordinator.cs"
AUTO = SHELL / "UpdateAutoCheck.cs"
PREFS = SHELL / "UpdatePreferences.cs"
RPC = SHELL / "RpcBridge.cs"
MAINWINDOW = SHELL / "MainWindow.xaml.cs"
APP_JS = SHELL / "frontend" / "js" / "about-update.js"
PROBE = SHELL / "tools" / "ShellContractProbe" / "Program.cs"
PROBE_PROJ = SHELL / "tools" / "ShellContractProbe" / "ShellContractProbe.csproj"


def test_preferences_carry_auto_check_without_dropping_other_fields():
    """自动检查开关落盘：老配置缺字段按开启处理；每次保存都保留其他字段
    （设置与提醒标记在不同交互序列里先后写盘，后写的一方不得覆盖另一方）。"""
    prefs = PREFS.read_text(encoding="utf-8")
    assert 'JsonPropertyName("auto_check_enabled")' in prefs, "偏好文件未携带自动检查开关"
    assert "LoadAutoCheck()" in prefs and "SaveAutoCheck(" in prefs
    assert "AutoCheckEnabled ?? true" in prefs, "老配置缺字段时应默认开启"
    saves = prefs.count("AutoCheckEnabled = ")
    assert saves >= 3, "渠道/源、提醒标记、开关三条保存路径都要保留该字段"
    assert prefs.count("DismissedVersion = existing?.DismissedVersion") >= 2, (
        "保存渠道/源或开关时覆盖了提醒标记"
    )


def test_coordinator_merges_inflight_checks_and_guards_actions():
    """检查入口：同世代在途合并（不重复发请求）；下载/忽略带修订号准入；
    切源前进修订号；自动检查不允许 ready。"""
    src = COORD.read_text(encoding="utf-8")
    check = _cs_method(
        src, "public Task<UpdateSnapshot> CheckAsync(UpdateCheckTrigger trigger = UpdateCheckTrigger.Manual)"
    )
    assert "_checkTask is { IsCompleted: false }" in check and "_checkTaskEpoch == _epoch" in check, (
        "同世代在途检查未合并"
    )
    assert "CanRunCheck(trigger)" in check, "检查准入未区分发起方"
    ready = _cs_method(
        src,
        "private bool CanRunCheck(UpdateCheckTrigger trigger)",
    )
    assert "UpdateSnapshot.Ready => trigger == UpdateCheckTrigger.Manual" in ready, (
        "自动检查不得在 ready 时撤掉已下载入口"
    )
    download = _cs_method(src, "public Task<UpdateSnapshot> StartDownloadAsync(long? expectedRevision = null)")
    assert "seen != _revision" in download, "下载未校验玩家看到的修订号"
    switch = _cs_method(src, "private async Task<UpdateSnapshot> SwitchSettingAsync(string? value, bool isChannel)")
    assert "if (changed) _revision++" in switch, "切下载源未前进修订号"
    assert "ResetForNewEpoch()" in switch, "切渠道未清空在途与就绪"


def test_portable_check_uses_cancellable_short_budget():
    """便携检查预算：标记+清单读取绑定检查令牌与单源短预算，取消时不被 catch 吞成源不可达。"""
    src = COORD.read_text(encoding="utf-8")
    checker = _cs_method(src, "private async Task<UpdateSnapshot> DoPortableCheckAsync(CheckIntent intent,")
    assert "CreateLinkedTokenSource(ct)" in checker, "检查预算未绑定检查令牌"
    assert "budget.CancelAfter(_indexTimeout)" in checker, "单源索引预算未生效"
    assert "if (markerAhead) sawUnavailableNewer = true" in checker, "超时源不应误判已是最新"


def test_portable_mirror_reuses_same_target_manifest():
    """镜像回退：只换地址不换发行，按已选定的 tag/版本取清单并核对整包摘要。"""
    src = COORD.read_text(encoding="utf-8")
    stage = _cs_method(src, "private async Task<StagedUpdate> StagePortableAsync(UpdateRelease release,")
    assert "FetchManifestAtAsync(candidate, release.Tag, release.Version" in stage, "镜像未取同目标清单"
    assert "mirror.Full.Sha256, release.Full.Sha256" in stage, "镜像整包摘要未核对"


def test_auto_check_failure_keeps_state_and_manual_failure_reports():
    """自动检查全源失败保留已有目标/相位/错误；手动检查失败才切空闲并报错。"""
    src = COORD.read_text(encoding="utf-8")
    check = _cs_method(
        src,
        "private async Task<UpdateSnapshot> DoPortableCheckAsync(CheckIntent intent,",
    )
    assert "if (trigger == UpdateCheckTrigger.Auto)" in check, "全源失败未区分发起方"
    assert "自动检查全源失败，保留当前状态" in check, "自动失败未保留状态"
    assert "所有更新源均不可达" in check, "手动失败未如实报错"


def test_auto_check_events_and_anchor():
    """自动检查提交成功触发推送回调；计时锚点由检查收尾与忙态跳过共同推进。"""
    src = COORD.read_text(encoding="utf-8")
    assert "public event Action<UpdateSnapshot>? AutoCheckCommitted" in src, "缺少自动结果推送回调"
    assert "public event Action? AutoCheckSettingChanged" in src, "缺少开关变化唤醒回调"
    settled = _cs_method(src, "private void NoteCheckSettled(DateTimeOffset now)")
    assert "_autoAnchorUtc = now" in settled and "_lastCheckCompletedUtc = now" in settled, (
        "检查收尾未推进锚点"
    )
    check = _cs_method(
        src, "public Task<UpdateSnapshot> CheckAsync(UpdateCheckTrigger trigger = UpdateCheckTrigger.Manual)"
    )
    assert "if (trigger == UpdateCheckTrigger.Auto) _autoAnchorUtc = _time.GetUtcNow();" in check, (
        "忙态/不可用时的自动尝试未推进锚点（会立刻重试）"
    )
    delay = _cs_method(src, "public TimeSpan? NextAutoCheckDelay(DateTimeOffset now, TimeSpan interval)")
    assert "if (!_portable || !_autoCheckEnabled) return null" in delay, (
        "非便携运行/关闭时仍会安排检查"
    )
    assert "return TimeSpan.Zero" in delay, "首次检查未立即到期"


def test_scheduler_uses_time_provider_and_token_delay():
    """调度等待用 TimeProvider + 带令牌的 Task.Delay，且能被开关变化打断；
    关窗 Stop 取消在途索引。"""
    src = AUTO.read_text(encoding="utf-8")
    assert "TimeProvider" in src and "Task.Delay(delay, _time, linked.Token)" in src, (
        "调度等待未用可注入时钟/可打断令牌"
    )
    assert "AutoCheckSettingChanged += Wake" in src, "开关变化未唤醒调度"
    assert "CancelPendingCheck()" in src, "停止调度未取消在途索引"
    loop = _cs_method(src, "private async Task LoopAsync(CancellationToken stop)")
    assert "NextAutoCheckDelay" in loop, "循环未按锚点重算到期"


def test_shell_wires_scheduler_push_and_auto_check_preference():
    """主窗：装配调度器并在关窗时停止；订阅自动检查推送；加载/保存自动检查偏好。"""
    mw = MAINWINDOW.read_text(encoding="utf-8")
    assert "LoadAutoCheck()" in mw, "启动未读取自动检查偏好"
    assert "saveAutoCheck: UpdatePreferences.SaveAutoCheck" in mw, "开关未落盘"
    assert "AutoCheckCommitted += PostUpdateSnapshot" in mw, "后台结果未接原生推送"
    assert "_autoCheck = new UpdateAutoCheck(coordinator)" in mw and "_autoCheck.Start()" in mw, (
        "未装配进程内自动检查调度"
    )
    assert "_autoCheck?.Dispose()" in mw, "关窗未停止调度"
    post = _cs_method(mw, "private void PostUpdateSnapshot(UpdateSnapshot snapshot)")
    assert 'type = "update-status"' in post, "推送消息类型不符"
    assert "_windowClosed" in post and "CoreWebView2?.PostWebMessageAsJson" in post, (
        "推送未核对关窗态与 WebView2 可用性"
    )


def test_frontend_subscribes_push_and_switch():
    """前端：初始化前订阅推送并经 renderSnapshot 渲染；开关走宿主动作。"""
    js = APP_JS.read_text(encoding="utf-8")
    assert "maarm.onNativeMessage((msg) =>" in js, "未订阅原生推送"
    assert "msg.type !== 'update-status'" in js, "推送类型不匹配"
    assert "maarm.call('update_set_auto_check', { value })" in js, "开关未接宿主动作"


def test_contract_probe_auto_mode_is_reachable():
    """行为验证链必须存在：探针 auto 模式覆盖调度/并发/预算/锁/修订。"""
    probe = PROBE.read_text(encoding="utf-8")
    assert 'case "auto"' in probe, "合同探针缺 auto 模式"
    body = _cs_method(probe, "private static async Task CheckUpdateAutoCheckAsync()")
    for marker in (
        "NextAutoCheckDelay",
        "IndexRequests",
        "SetIndexDelay",
        "indexTimeout",
        "installRoot",
        "StartDownloadAsync(stale)",
        "AutoCheckCommitted",
        "CancelPendingCheck",
    ):
        assert marker in body, f"auto 行为用例未覆盖 {marker}"
    project = PROBE_PROJ.read_text(encoding="utf-8")
    assert "UpdateAutoCheck.cs" in project, "探针未链接调度器源码"


def test_contract_probe_auto_assertions_actually_pass():
    """有 dotnet 的环境直接跑便携源行为断言；无 dotnet 时明确跳过，
    不把「没跑」伪装成通过。"""
    if shutil.which("dotnet") is None:
        pytest.skip("本机无 dotnet；行为真值由合同探针 auto 模式在开发机验证")
    result = subprocess.run(
        ["dotnet", "run", "--project", str(PROBE_PROJ), "-c", "Debug", "--", "auto"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "ALL PASS" in output, (
        "合同探针 auto 行为断言未全过:\n" + output
    )
    assert output.count("[PASS]") >= 25, "行为断言数量不足以覆盖调度/并发/预算/锁/修订"


def _cs_method(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError(f"{signature} 未闭合")
