# -*- coding: utf-8 -*-
"""环境中心「运行环境」页签与 WebView2 更新的静态契约锁。

锁四件事（真实下载/安装依赖联网与管理员权限，不进单测）：

1. 宿主分流的两个方法名同时出现在 AllowedMethods 与 HostMethods；
2. 更新器只使用固定的微软官方跳转，不接受前端传入地址或路径；
3. 执行安装器之前必须完成验签，且安装参数是静默安装；
4. 前端第三个页签存在并调用状态/更新接口，且启动体检仍只覆盖注册表项。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RPC = REPO / "apps" / "MaaRacingMaster.Shell" / "RpcBridge.cs"
UPDATER = REPO / "apps" / "MaaRacingMaster.Shell" / "WebView2RuntimeUpdater.cs"
RESTARTER = REPO / "apps" / "MaaRacingMaster.Shell" / "AppRestarter.cs"
PROGRAM = REPO / "apps" / "MaaRacingMaster.Shell" / "Program.cs"
MAINWINDOW = REPO / "apps" / "MaaRacingMaster.Shell" / "MainWindow.xaml.cs"
ENV_CENTER = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "js" / "env-center.js"
REMOTE = REPO / "maaracing_master" / "core" / "remote_meta.py"


def _cs_set(src: str, field: str) -> set[str]:
    m = re.search(
        rf"{field} = new\(StringComparer\.Ordinal\)\s*\{{(.*?)\}};", src, re.S)
    assert m, f"RpcBridge.cs 找不到 {field} 字面量块"
    return set(re.findall(r'"([a-z_0-9]+)"', m.group(1)))


def test_host_methods_registered_in_both_sets():
    src = RPC.read_text(encoding="utf-8")
    allowed = _cs_set(src, "AllowedMethods")
    host = _cs_set(src, "HostMethods")
    for name in ("webview2_status", "webview2_update", "app_restart"):
        assert name in allowed, f"AllowedMethods 缺 {name}"
        assert name in host, f"HostMethods 缺 {name}"


def test_updater_uses_fixed_microsoft_source():
    src = UPDATER.read_text(encoding="utf-8")
    assert "https://go.microsoft.com/fwlink/p/?linkid=2124703" in src
    assert "ExpectedSigner = \"Microsoft Corporation\"" in src
    assert "MaxDownloadBytes" in src
    # 更新器不读前端参数：没有 JsonElement / params 入口
    assert "JsonElement" not in src
    assert "paramsEl" not in src


def test_updater_verifies_signature_before_executing():
    src = UPDATER.read_text(encoding="utf-8")
    verify_idx = src.index("VerifyAuthenticode(exe")
    exec_idx = src.index("Process.Start(psi)")
    assert verify_idx < exec_idx, "必须先验签再执行安装器"
    assert "/silent /install" in src


def test_temp_artifacts_are_cleaned():
    # 下载落点在同一临时目录；本次结束即删，下次更新前再清残留
    src = UPDATER.read_text(encoding="utf-8")
    assert "CleanupStaleTempDirs" in src
    assert '"webview2-*"' in src
    assert "Directory.Delete(dir, recursive: true)" in src


def test_update_is_single_flight():
    # 关闭弹层再打开可能触发第二次更新；两个安装器并发会互相破坏
    src = UPDATER.read_text(encoding="utf-8")
    assert "Interlocked.CompareExchange" in src
    assert "Interlocked.Exchange" in src


def test_runtime_tab_wired_and_startup_check_unchanged():
    src = ENV_CENTER.read_text(encoding="utf-8")
    assert 'data-pane="pane-runtime"' in src
    assert "maarm.call('webview2_status'" in src
    assert "maarm.call('webview2_update'" in src
    # 启动体检仍只读注册表优化项：运行环境不进启动弹窗
    body = src[src.index("async function checkRegistryOptimizations()"):]
    assert "webview2" not in body.split("openModal")[0]


def test_runtime_buttons_are_state_driven():
    # 用户回归：已就绪时不能再常驻红色“更新到最新”；待重启时必须给“重启本程序”；
    # 就绪态的维护按钮不能叫“检查更新”——它真的会下载并安装，见下一条断言。
    src = ENV_CENTER.read_text(encoding="utf-8")
    item = src[src.index("function runtimeItemHtml"):src.index("async function renderRuntimeList")]
    assert 'data-wv2="restart"' in item
    assert "重启本程序" in item
    assert "当前界面正在使用该版本" in item
    assert "更新到最新" not in item
    assert "更新运行时" in item
    assert "检查更新" not in item


def test_update_button_does_not_pretend_to_be_a_check():
    # 微软没有轻量版本查询：点一次就下载约 1.7MB 并幂等安装，忙碌文案必须如实说明
    src = ENV_CENTER.read_text(encoding="utf-8")
    assert "下载安装中" in src
    assert "约 1.7MB" in src


def test_update_channel_health_is_surfaced():
    # 运行时版本不旧但自动更新通道停摆，是这次最隐蔽的故障；必须把上次成功检查摆出来
    src = UPDATER.read_text(encoding="utf-8")
    assert "LastCheckSuccess" in src
    assert "UpdateChannelHealth" in src
    settings = ENV_CENTER.read_text(encoding="utf-8")
    assert "自动更新上次成功检查" in settings
    assert "90 天" in settings


def test_restart_is_launch_mode_agnostic():
    # 重启统一走「等待旧进程退出 → 用同一进程路径拉起新实例」，源码与打包共用；
    # dotnet 启动时补回 dll 参数由 CreateSelfStartInfo 统一处理。
    program = PROGRAM.read_text(encoding="utf-8")
    assert "RestartWaiterFlag" in program
    assert "TryRunRestartWaiter" in program
    assert "CreateSelfStartInfo" in program
    restarter = RESTARTER.read_text(encoding="utf-8")
    assert "CreateSelfStartInfo" in restarter
    assert "Scheduled" in restarter
    main = MAINWINDOW.read_text(encoding="utf-8")
    assert "ScheduleRestartShutdown" in main


def test_official_page_target_whitelisted():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_rm_probe", REMOTE)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    url = mod.EXTERNAL_TARGETS["webview2"]
    assert url.startswith("https://")
    assert mod.is_openable_url(url)
