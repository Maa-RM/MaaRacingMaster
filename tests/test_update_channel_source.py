# -*- coding: utf-8 -*-
"""宿主更新协调器的渠道 / 下载源契约锁。

**语义基线**（与计划文档一致）：两个下载地址指向同一发行货架，名字只决定
首选顺序；渠道是用户设置，总是显式传给更新器、默认稳定轨；不做降级安装，
从测试轨切回后等稳定轨追上（数据只升不降）。

**分工**：本文件只锁「形态不被改回旧 bug」，不冒充行为验证。行为真值由壳
合同探针承担，它直接链接本仓库的 UpdateCoordinator.cs 并跑便携源夹具：

    dotnet run --project apps/MaaRacingMaster.Shell/tools/ShellContractProbe \\
        -c Debug -- settings

该模式覆盖：默认显式稳定轨、切回稳定轨真的换索引、等待标记且无降级目标、
切下载源保留就绪目标、首选不可达回退、渠道切换作废旧世代检查。本文件末尾
的用例会在有 dotnet 的环境里自动驱动它，避免「静态锁全绿但功能没接线」。

**空过防护**：每处抽取都锚定具体字面量，规则失效时报错而非静默通过。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHELL = REPO / "apps" / "MaaRacingMaster.Shell"
COORD = SHELL / "UpdateCoordinator.cs"
PREFS = SHELL / "UpdatePreferences.cs"
RPC = SHELL / "RpcBridge.cs"
ASSEMBLE = REPO / "scripts" / "release" / "assemble.ps1"
RELEASE_YML = REPO / ".github" / "workflows" / "release.yml"
PROBE = SHELL / "tools" / "ShellContractProbe" / "Program.cs"
PROBE_PROJ = SHELL / "tools" / "ShellContractProbe" / "ShellContractProbe.csproj"


def test_snapshot_carries_channel_source_and_awaiting():
    """快照带 channel/source、等待标记与提醒标记；被否决的「分组可切换」伪状态不得回潮。"""
    src = COORD.read_text(encoding="utf-8")
    for field in ("channel", "source", "awaiting_stable", "dismissed_version"):
        assert re.search(rf'JsonPropertyName\("{field}"\)\]', src), f"快照缺 {field} 字段"
    assert "source_switchable" not in src, "「分组 switchable」伪状态应已删除"
    assert "ChannelStable = \"stable\"" in src and "ChannelPreview = \"preview\"" in src
    assert "SourceCnb = \"cnb\"" in src and "SourceGithub = \"github\"" in src


def test_channel_is_always_explicit_and_never_downgrades():
    """渠道总是显式传入（用户设置、默认稳定轨）；降级开关不得存在：
    从测试轨切回后等稳定轨追上，数据只升不降。"""
    src = COORD.read_text(encoding="utf-8")
    assert "AllowVersionDowngrade" not in src, "降级开关应已整体删除"
    assert "_channelExplicit" not in src, "「未选过渠道」隐藏态应已删除"
    checker = _cs_method(src, "private async Task<UpdateSnapshot> DoPortableCheckAsync(CheckIntent intent,")
    assert "FetchMarkerAsync(endpoint, intent.Channel" in checker, "未按用户选择的渠道读标记"
    assert "release.Channel, intent.Channel" in checker, "清单渠道与请求渠道不符时未拒绝"
    switch = _cs_method(src, "private async Task<UpdateSnapshot> SwitchSettingAsync(string? value, bool isChannel)")
    assert "_awaitingStable = value == UpdateSnapshot.ChannelStable" in switch, (
        "切回稳定轨时未如实设置等待标记"
    )
    assert "UpdateSnapshot.ChannelPreview" in switch, "未区分切到测试轨与切回稳定轨"


def test_sources_are_named_addresses_not_shelves():
    """配置 = 命名地址行，名字只决定首选顺序：偏好在前、其余地址作回退；
    不存在分组筛选。无标签行按无名地址参与回退，空地址行跳过。"""
    catalog = REPO / "apps" / "MaaRacingMaster.Shell" / "UpdateSourceClient.cs"
    cat_src = catalog.read_text(encoding="utf-8")
    parse = _cs_method(cat_src, "public static IReadOnlyList<UpdateSourceEndpoint> Parse(string? config, string preference)")
    assert "OrderByDescending(e => string.Equals(e.Tag, preference" in parse, "偏好未排到首位"
    assert "Where(e => e.Base.Length > 0)" in parse, "空地址行未跳过"


def test_switch_semantics_differ_by_dimension():
    """切渠道清空在途与就绪、当场重查，且取消旧索引后等它真正退出再放行新检查；
    切源保留在途与就绪、只换首选并前进修订号（旧界面点击据此失效），不当场重查。"""
    src = COORD.read_text(encoding="utf-8")
    switch = _cs_method(src, "private async Task<UpdateSnapshot> SwitchSettingAsync(string? value, bool isChannel)")
    assert "ResetForNewEpoch()" in switch, "渠道切换未重置在途状态"
    assert "if (!isChannel) return GetStatus()" in switch, "源切换不应触发重查"
    assert "return await CheckAsync().ConfigureAwait(false)" in switch, "渠道切换未当场重查"
    assert "draining is { IsCompleted: false }" in switch, "切渠道未等旧检查事务退出"
    assert "if (_epoch != epoch || _channel != value) return Snapshot();" in switch, (
        "排水后的续体未复查世代与最新意图（连点两次渠道切换会为旧意图启检查）"
    )
    assert "if (changed) _revision++" in switch, "切下载源未前进修订号（旧界面点击不会失效）"
    assert "Preparing or" in switch and "Applying)" in switch, "提交临界区未禁止切换"
    assert "_savePreferences(_channel, _source, _awaitingStable)" in switch, "偏好未落盘"
    reset = _cs_method(src, "private void ResetForNewEpoch()")
    assert "_epoch++" in reset, "渠道切换未开启新世代"
    assert "_activeIntent = null" in reset and "_release = null" in reset
    assert "_checkCts?.Cancel()" in reset, "切入新世代未取消旧索引请求"
    assert "_phase = _portable ? UpdateSnapshot.Idle : UpdateSnapshot.Unavailable" in reset, (
        "非便携运行切渠道后必须保持 unavailable，不得刷成 Idle+更新源未配置"
    )


def test_epoch_guards_every_async_write():
    """所有异步回写（检查/下载完成/取消回退/进度/镜像）都携带世代，
    经 SetState(epoch) 唯一出口校验。"""
    src = COORD.read_text(encoding="utf-8")
    setter = _cs_method(
        src,
        "private UpdateSnapshot SetState(long epoch, string phase, int? progress = null, string? error = null)",
    )
    assert "if (epoch != _epoch) return Snapshot();" in setter, "状态翻转出口未校验世代"
    back = _cs_method(src, "private void BackToAvailable(CheckIntent intent, string error)")
    assert "intent.Epoch == _epoch" in back, "旧下载取消后的回写未校验世代（竞态窗口）"
    stage = _cs_method(src, "private async Task<StagedUpdate> StagePortableAsync(UpdateRelease release,")
    assert "intent.Epoch == _epoch" in stage, "增量暂存进度回调未校验世代"
    mirror = _cs_method(src, "private async Task<StagedUpdate> StagePortableAsync(UpdateRelease release,")
    assert "intent.Endpoints" in mirror, "镜像候选未取自事务上下文"
    assert "FetchManifestAtAsync(candidate, release.Tag, release.Version" in mirror, "镜像未按同目标版本取清单"
    assert "epoch != Epoch || phase != StartPhase" in src, "检查意图缺相位/世代双重作废判据"


def test_awaiting_marker_persists_and_clears():
    """等待标记随偏好落盘；稳定轨追上（出现可安装目标）时清除并落盘，
    避免重启后旧标记让已升级的用户看到错误文案。"""
    src = COORD.read_text(encoding="utf-8")
    checker = _cs_method(
        src,
        "private async Task<UpdateSnapshot> DoPortableCheckAsync(CheckIntent intent,",
    )
    assert "_awaitingStable = false" in checker, "稳定轨追上时未清除等待标记"
    assert "_savePreferences(_channel, _source, false)" in checker, (
        "清除后未落盘，重启后旧标记会显示错误文案"
    )
    prefs = PREFS.read_text(encoding="utf-8")
    assert "from_preview" in prefs, "偏好文件未携带等待标记"
    assert "channel == UpdateSnapshot.ChannelStable && fromPreview" in prefs, (
        "等待标记在测试轨上无意义，应一律落 false"
    )
    assert "(string Channel, string Source, bool FromPreview) Load()" in prefs, (
        "偏好不再区分「未选过渠道」——渠道与源都有显式默认值"
    )


def test_dismissed_version_is_per_target_and_preserved_on_save():
    """「本次更新不再提示」按目标版本记忆：切换渠道/源落盘时不得覆盖它，
    单独保存提醒标记时也不得丢渠道/源；没有目标时拒绝写入空标记。"""
    prefs = PREFS.read_text(encoding="utf-8")
    assert 'JsonPropertyName("dismissed_version")' in prefs, "偏好文件未携带提醒标记"
    assert "LoadDismissedVersion" in prefs and "SaveDismissedVersion" in prefs
    assert "DismissedVersion = existing?.DismissedVersion" in prefs, (
        "保存渠道/源时覆盖了提醒标记"
    )
    for field in ("Channel = channel", "Source = source"):
        assert field in prefs, "单独保存提醒标记时丢失了渠道/源"

    coord = COORD.read_text(encoding="utf-8")
    setter = _cs_method(coord, "public UpdateSnapshot SetDismissed(bool dismissed, long? expectedRevision = null)")
    assert "value = dismissed ? _targetVersion : null" in setter, "提醒标记未绑定当前目标版本"
    assert "string.IsNullOrEmpty(_targetVersion)" in setter, "无目标时未拒绝写入空标记"
    assert "_saveDismissedVersion(value)" in setter, "提醒标记未落盘"
    assert "value == _dismissedVersion" in setter, "重复设置未走幂等分支"
    assert "seen != _revision" in setter, "忽略操作未校验玩家看到的修订号"

    rpc = RPC.read_text(encoding="utf-8")
    assert '"update_set_dismissed"' in rpc, "RPC 白名单缺提醒标记动作"


def test_publisher_requires_both_named_addresses():
    """发布端是命名行的唯一生产者且两个地址必填：缺任一报错不写文件，
    不合规配置不得流出；单源兼容模式不得回潮。"""
    script = ASSEMBLE.read_text(encoding="utf-8-sig")
    assert "-UpdateSourceCnb" in script and "-UpdateSourceGithub" in script
    assert "$UpdateSource " not in script and "IsNullOrWhiteSpace($UpdateSource)" not in script, (
        "单源兼容参数 -UpdateSource 应已删除"
    )
    assert "$sourceLines.Add(\"cnb|" in script and "$sourceLines.Add(\"github|" in script, (
        "发布端未产出与客户端对称的命名行"
    )
    assert "同时提供" in script, "缺任一地址未报错"
    ci = RELEASE_YML.read_text(encoding="utf-8")
    assert "assemble.ps1" in ci, "发行工作流未调用组装脚本"
    for banned in ("-Managed", "vpk", "make_update_release"):
        assert banned not in ci, f"发行工作流残留受管发布链痕迹：{banned}"


def test_rpc_exposes_setting_and_auto_check_actions():
    """RPC 白名单含两个设置动作与自动检查开关；设置动作只带枚举值/布尔值，前端不传 URL。"""
    src = RPC.read_text(encoding="utf-8")
    for action in ("update_set_channel", "update_set_source", "update_set_auto_check"):
        assert f'"{action}"' in src, f"RPC 白名单缺 {action}"
    assert 'ReadParam(paramsEl, "value")' in src, "设置动作未读取枚举参数"
    assert 'ReadBoolParam(paramsEl, "value")' in src, "布尔开关未读取布尔参数"
    assert 'ReadLongParam(paramsEl, "revision")' in src, "下载/忽略未读取修订号参数"


def test_contract_probe_settings_mode_is_reachable():
    """行为验证链必须存在且覆盖切渠道/切源/等待标记/世代守卫，不能只剩文本锁。"""
    probe = PROBE.read_text(encoding="utf-8")
    assert 'case "settings"' in probe, "合同探针缺 settings 模式"
    body = _cs_method(probe, "private static async Task CheckUpdateSettingsAsync()")
    for marker in ("SetChannelAsync", "SetSourceAsync", "AwaitingStable", "inFlight"):
        assert marker in body, f"settings 行为用例未覆盖 {marker}"


def test_contract_probe_settings_assertions_actually_pass():
    """有 dotnet 的环境直接跑便携源行为断言；无 dotnet 时明确跳过，
    不把「没跑」伪装成通过。"""
    if shutil.which("dotnet") is None:
        import pytest

        pytest.skip("本机无 dotnet；行为真值由合同探针 settings 模式在开发机验证")
    result = subprocess.run(
        ["dotnet", "run", "--project", str(PROBE_PROJ), "-c", "Debug", "--", "settings"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "ALL PASS" in output, (
        "合同探针 settings 行为断言未全过:\n" + output
    )
    assert output.count("[PASS]") >= 10, "行为断言数量不足以覆盖渠道/源/等待三类语义"


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
