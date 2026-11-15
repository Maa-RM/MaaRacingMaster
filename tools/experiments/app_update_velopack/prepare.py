"""准备可复算的隔离工具与真实壳副本，不修改正式工程。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "_out"
VERSION = "1.2.161"
SDK_SHA256 = "d57275692b299751a12ee3e18283b5f7fe3e55339f96b1623345da095dc484ed"
# 已组装运行时缓存（embed python + packages + 生产 _pth），默认取当前检出的
# build/runtime-cache，跨检出复用时用环境变量覆盖。仅实验使用，不入正式流程。
RUNTIME_SOURCE = Path(os.environ.get(
    "MAARM_RUNTIME_SOURCE", str(REPO / "build" / "runtime-cache")))

# 生产同款发布口径：assemble.ps1 §2 用 -r win-x64 --self-contained true（.NET 运行时随包）。
MANAGED_PUBLISH_ARGS = ["-r", "win-x64", "--self-contained", "true", "-p:Version=0.1.0"]

# 注入真实壳 Main 开头的探针块：SDK 初始化在单实例检查之前（P0 门禁口径），
# 版本报告经环境变量落盘（stub→Update.exe→壳 两跳下 stdout 不可靠）；
# 下载/提交走本地源探针参数，正式接入由 P2 壳宿主实现，不入正式工程。
MAIN_INJECT = '''\n        Velopack.VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
        var __reportPath = Environment.GetEnvironmentVariable("MAARM_PROBE_REPORT");
        if (!string.IsNullOrEmpty(__reportPath))
        {
            System.IO.File.WriteAllText(__reportPath, System.Text.Json.JsonSerializer.Serialize(new {
                version = Velopack.Locators.VelopackLocator.Current.CurrentlyInstalledVersion?.ToString(),
                appContentRoot = Velopack.Locators.VelopackLocator.Current.AppContentDir,
                binaryRoot = AppContext.BaseDirectory }));
        }
        if (args.Contains("--update-adapter-probe")) { Console.WriteLine("REAL_SHELL_PRE_UI_OK"); return; }
        for (int __i = 0; __i + 1 < args.Length; __i++)
        {
            if (args[__i] == "--download-local") { ProbeLocalUpdate(args[__i + 1], apply: false); return; }
            if (args[__i] == "--apply-local") { ProbeLocalUpdate(args[__i + 1], apply: true); return; }
        }'''

# 拆分实验变体：与完整注入唯一差异是版本报告不访问 VelopackLocator（读磁盘标记）。
MAIN_INJECT_NL = '''\n        Velopack.VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
        var __reportPath = Environment.GetEnvironmentVariable("MAARM_PROBE_REPORT");
        if (!string.IsNullOrEmpty(__reportPath))
        {
            var __marker = System.IO.Path.Combine(AppContext.BaseDirectory, "payload-version.txt");
            System.IO.File.WriteAllText(__reportPath, System.Text.Json.JsonSerializer.Serialize(new {
                version = System.IO.File.Exists(__marker) ? System.IO.File.ReadAllText(__marker).Trim() : (string)null,
                binaryRoot = AppContext.BaseDirectory }));
        }
        if (args.Contains("--update-adapter-probe")) { Console.WriteLine("REAL_SHELL_PRE_UI_OK"); return; }
        for (int __i = 0; __i + 1 < args.Length; __i++)
        {
            if (args[__i] == "--download-local") { ProbeLocalUpdate(args[__i + 1], apply: false); return; }
            if (args[__i] == "--apply-local") { ProbeLocalUpdate(args[__i + 1], apply: true); return; }
        }'''

HELPER_INJECT = '''    // 实验专用探针：本地源下载/提交更新；与已验证样本 probe 同 API。
    private static void ProbeLocalUpdate(string feed, bool apply)
    {
        if (!System.IO.Directory.Exists(feed)) throw new ArgumentException("实验仅接受存在的本地更新源");
        var manager = new Velopack.UpdateManager(System.IO.Path.GetFullPath(feed));
        var update = manager.CheckForUpdatesAsync().GetAwaiter().GetResult()
            ?? throw new InvalidOperationException("隔离更新源没有新版");
        manager.DownloadUpdatesAsync(update).GetAwaiter().GetResult();
        Console.WriteLine("UPDATE_DOWNLOADED");
        if (!apply) return;
        manager.WaitExitThenApplyUpdates(update.TargetFullRelease, silent: true, restart: true);
        Console.WriteLine("UPDATE_QUEUED");
    }

'''

# MainWindow 注入：WebView2 初始化成败都落盘（白窗定位仪器）。正式接入不需要。
WEBVIEW_PROBE_METHOD = '''    // 实验专用：WebView2 初始化证据（白窗定位）。正式接入不需要。
    private static async System.Threading.Tasks.Task ProbeWebViewInit(
        Microsoft.UI.Xaml.Controls.WebView2 web)
    {
        var __p = Environment.GetEnvironmentVariable("MAARM_PROBE_REPORT");
        if (string.IsNullOrEmpty(__p)) return;
        try
        {
            await web.EnsureCoreWebView2Async();
            System.IO.File.AppendAllText(__p + ".webview2.txt", "ENSURE_OK\\n");
        }
        catch (Exception __ex)
        {
            System.IO.File.AppendAllText(__p + ".webview2.txt", "ENSURE_FAILED " + __ex + "\\n");
        }
    }

'''


def sdk() -> None:
    OUT.mkdir(exist_ok=True)
    archive = OUT / "sdk.nupkg"
    if not archive.is_file():
        url = f"https://api.nuget.org/v3-flatcontainer/velopack/{VERSION}/velopack.{VERSION}.nupkg"
        with urllib.request.urlopen(url, timeout=30) as response:
            archive.write_bytes(response.read())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == SDK_SHA256
    (OUT / "sdk").mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for name in ("Velopack.dll", "Velopack.xml"):
            (OUT / "sdk" / name).write_bytes(z.read("lib/net8.0/" + name))


def publish(project: Path, target: Path, extra: list[str] | None = None,
            timeout: int = 120, config: str = "Release") -> None:
    cmd = ["dotnet", "publish", str(project), "-c", config, "-o", str(target),
           "--configfile", str(HERE / "NuGet.Config"), "--nologo"]
    cmd += extra or ["--self-contained", "false"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    (OUT / (target.name + ".log")).write_text(p.stdout + p.stderr, encoding="utf-8")
    assert p.returncode == 0, p.stdout + p.stderr
    print("PUBLISH_OK", target.name)


def tool() -> None:
    cli = OUT / "cli"
    if not (cli / "vpk.exe").is_file():
        cmd = ["dotnet", "tool", "install", "vpk", "--version", VERSION,
               "--tool-path", str(cli), "--configfile", str(HERE / "NuGet.Config"),
               "--disable-parallel"]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=150,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        (OUT / "tool-install.log").write_text(p.stdout + p.stderr, encoding="utf-8")
        assert p.returncode == 0, p.stdout + p.stderr
    listed = subprocess.run(["dotnet", "tool", "list", "--tool-path", str(cli)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert listed.returncode == 0 and VERSION in listed.stdout, listed.stdout + listed.stderr
    print("TOOL_OK", VERSION)


def _isolated_shell_copy(copy: Path) -> Path:
    """隔离壳源码副本 + SDK 引用；不触碰正式工程。"""
    project = copy / "apps" / "MaaRacingMaster.Shell"
    project.mkdir(parents=True, exist_ok=True)
    for src in (REPO / "apps" / "MaaRacingMaster.Shell").iterdir():
        if src.is_file() and src.suffix in (".cs", ".csproj", ".xaml", ".manifest"):
            shutil.copy2(src, project / src.name)
    (copy / "assets").mkdir(exist_ok=True)
    shutil.copy2(REPO / "assets/icon.ico", copy / "assets/icon.ico")
    csproj = project / "MaaRacingMaster.Shell.csproj"
    text = csproj.read_text(encoding="utf-8")
    ref = ('<ItemGroup><Reference Include="Velopack"><HintPath>'
           + str(OUT / "sdk/Velopack.dll")
           + '</HintPath></Reference></ItemGroup>')
    csproj.write_text(text.replace("</Project>", ref + "\n</Project>"), encoding="utf-8")
    return csproj


# 最小注入形态（本机实测必正常渲染）：仅更新器初始化 + 探针参数早退。
MINIMAL_INJECT = '''\n        Velopack.VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
        if (args.Contains("--update-adapter-probe")) { Console.WriteLine("REAL_SHELL_PRE_UI_OK"); return; }'''


def real_shell() -> None:
    copy = OUT / "shell-copy"
    csproj = _isolated_shell_copy(copy)
    program = csproj.parent / "Program.cs"
    text = program.read_text(encoding="utf-8")
    needle = "private static void Main(string[] args)\n    {"
    assert needle in text
    program.write_text(text.replace(needle, needle + MINIMAL_INJECT, 1), encoding="utf-8")
    publish(csproj, OUT / "real-shell-publish")


def managed_shell(*, nl: bool = False, fd: bool = False, stage: bool = True,
                  mw_inject: bool = True, out_name: str = "",
                  minimal: bool = False) -> None:
    """真实壳接入 SDK 并组装标准受管布局内容树。

    minimal=True：壳只保留最小注入（已验证必正常渲染的形态），下载/提交改由
    内容树里的独立更新助手 MaaRM.UpdateProbe.exe 承担；nl=True：版本报告改读
    磁盘标记、不触碰 VelopackLocator（挂起机制拆分实验）；fd=True 按依赖框架
    发布（默认生产口径自包含）；stage=False 只发布不组树；mw_inject=False 不
    注入 MainWindow 的 WebView2 双初始化仪器（v1 复刻）。
    """
    suffix = "-nl" if nl else ""
    if out_name:
        suffix = "-" + out_name
    if minimal:
        suffix = "-minimal"
    copy = OUT / f"managed-copy{suffix}"
    csproj = _isolated_shell_copy(copy)
    program = csproj.parent / "Program.cs"
    text = program.read_text(encoding="utf-8")
    needle = "private static void Main(string[] args)\n    {"
    assert needle in text
    if minimal:
        inject = MINIMAL_INJECT
    elif nl:
        inject = MAIN_INJECT_NL
    else:
        inject = MAIN_INJECT
    body = (needle + inject) if minimal else (HELPER_INJECT + needle + inject)
    program.write_text(text.replace(needle, body, 1), encoding="utf-8")
    if mw_inject and not minimal:
        mainwindow = csproj.parent / "MainWindow.xaml.cs"
        text = mainwindow.read_text(encoding="utf-8")
        nav_needle = "            web.Source = new Uri(indexPath);"
        assert nav_needle in text, "MainWindow 导航锚点缺失"
        text = text.replace(nav_needle,
                            nav_needle + "\n            _ = ProbeWebViewInit(web);", 1)
        method_needle = ("    private void OnWebMessageReceived(WebView2 sender, "
                         "CoreWebView2WebMessageReceivedEventArgs args)")
        assert method_needle in text, "MainWindow 方法锚点缺失"
        text = text.replace(method_needle, WEBVIEW_PROBE_METHOD + method_needle, 1)
        mainwindow.write_text(text, encoding="utf-8")
    out_pub = OUT / f"managed-publish{suffix}"
    pub_args = (["-r", "win-x64", "--self-contained", "false", "-p:Version=0.1.0"]
                if fd else MANAGED_PUBLISH_ARGS)
    publish(csproj, out_pub, pub_args, timeout=900,
            config="Debug" if nl else "Release")
    if not stage:
        print("PUBLISH_ONLY_OK", out_pub)
        return
    out_pub = OUT / ("managed-publish-nl" if nl else "managed-publish")
    pub_args = (["-r", "win-x64", "--self-contained", "false", "-p:Version=0.1.0"]
                if fd else MANAGED_PUBLISH_ARGS)
    publish(csproj, out_pub, pub_args, timeout=900,
            config="Debug" if nl else "Release")
    if not stage:
        print("PUBLISH_ONLY_OK", out_pub)
        return

    stage = _stage_content_tree(out_pub, OUT / "managed-stage")
    print("MANAGED_STAGE_OK", stage)


def _stage_content_tree(out_pub: Path, stage: Path) -> Path:
    """把 GUI 发布产物与生产内容组装成标准受管内容树（受管链 / 正式壳链共用一套组装真相）。"""
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    # 标准布局首选：GUI 发布文件直接放内容根（非嵌套 app/）。
    for item in out_pub.iterdir():
        dst = stage / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    # 生产白名单相对结构（assemble.ps1 §3 口径）：根锚 pyproject.toml、assets、
    # frontend 按 apps/.../frontend 相对路径、Python 代码整树、自带运行时。
    for rel in ("pyproject.toml", "LICENSE", "THIRD_PARTY_LICENSES.md"):
        src = REPO / rel
        if src.is_file():
            shutil.copy2(src, stage / rel)
    stage_assets = stage / "assets"
    stage_assets.mkdir(exist_ok=True)
    for rel in ("icon.ico", "maarm_icon.png"):
        src = REPO / "assets" / rel
        if src.is_file():
            shutil.copy2(src, stage_assets / rel)
    shutil.copytree(REPO / "apps/MaaRacingMaster.Shell/frontend",
                    stage / "apps" / "MaaRacingMaster.Shell/frontend")
    shutil.copytree(REPO / "maaracing_master", stage / "maaracing_master",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    assert RUNTIME_SOURCE.is_dir(), (
        f"缺少已组装运行时缓存 {RUNTIME_SOURCE}；可设 MAARM_RUNTIME_SOURCE 指向 assemble 产物")
    shutil.copytree(RUNTIME_SOURCE, stage / "runtime" / "python")
    # 独立更新助手进内容根：下载/提交由它驱动，壳保持最小注入形态
    helper_dir = OUT / "update-helper"
    publish(HERE / "Probe/HookProbe.csproj", helper_dir, timeout=600)
    for name in ("MaaRM.UpdateProbe.exe", "MaaRM.UpdateProbe.dll",
                 "MaaRM.UpdateProbe.deps.json", "MaaRM.UpdateProbe.runtimeconfig.json"):
        src = helper_dir / name
        assert src.is_file(), f"更新助手产物缺失: {src}"
        shutil.copy2(src, stage / name)
    assert (stage / "pyproject.toml").is_file()
    assert (stage / "MaaRacingMaster.Shell.exe").is_file()
    assert (stage / "MaaRM.UpdateProbe.exe").is_file()
    assert (stage / "apps/MaaRacingMaster.Shell/frontend/index.html").is_file()
    assert (stage / "runtime/python/python.exe").is_file()
    return stage


def formal_shell() -> None:
    """P1 正式壳整链准备：直接发布正式工程（已含启动钩子、受管内容根解析、
    WebView2 用户数据根、侧车关停确认），组装标准受管内容树。
    无任何注入——本链验证的就是正式代码形态。"""
    out_pub = OUT / "formal-publish"
    publish(REPO / "apps/MaaRacingMaster.Shell/MaaRacingMaster.Shell.csproj",
            out_pub, MANAGED_PUBLISH_ARGS, timeout=900)
    stage = _stage_content_tree(out_pub, OUT / "formal-stage")
    print("FORMAL_STAGE_OK", stage)


def _vpk_pack(stage: Path, version: str, feed: Path) -> None:
    cmd = [str(OUT / "cli/vpk.exe"), "pack", "--packId", "MaaRM.RealShellProbe",
           "--packTitle", "MaaRMRealShellProbe", "--packVersion", version,
           "--runtime", "win-x64", "--packDir", str(stage),
           "--mainExe", "MaaRacingMaster.Shell.exe", "--outputDir", str(feed),
           "--noInst", "--delta", "None"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    (OUT / f"managed-pack-{version}.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    assert p.returncode == 0, p.stdout + p.stderr
    print("PACK_OK", version)


def pack_managed() -> None:
    """把内容树打成两个版本的受管便携包（同一 feed），并把 0.1.0 解压为 live 副本。

    便携 zip 文件名不含版本号，第二次打包会覆盖同名文件；因此必须在打 0.2.0
    之前把 0.1.0 便携包解压出来（与样本 probe 的 update 流程同口径）。
    """
    _pack_two_versions(("0.1.0", "0.2.0"))


def pack_p0_tail() -> None:
    """P0 收尾用例环境：0.1.0 新 live 副本 + feed 追加 0.3.0 目标（孤儿/毒化用例）。"""
    _pack_two_versions(("0.1.0", "0.3.0"))


def pack_formal() -> None:
    """P1 正式壳整链环境：正式壳内容树打两个版本（0.1.0→0.2.0），0.1.0 解压为 live 副本。"""
    _pack_two_versions(("0.1.0", "0.2.0"), tag="formal")


def _pack_two_versions(versions: tuple[str, str], tag: str = "managed") -> None:
    stage = OUT / (tag + "-stage")
    assert (stage / "MaaRacingMaster.Shell.exe").is_file(), (
        f"先运行对应内容树准备（{tag}-stage 缺壳）")
    feed = OUT / (f"{tag}-feed-" + uuid.uuid4().hex[:8])
    case = OUT / (f"{tag}-case-" + uuid.uuid4().hex[:8])
    live = case / "中文 目录 空格" / "live"
    if tag == "formal":
        # P3 整链受控源：随包更新源配置在打包前写入内容树（两版都带上，升级后仍有效）。
        # 正式发行由发布流程写入真实索引地址（同机制，不另开通道）；本地路径只存在于
        # 实验输出，不入库。
        feed.mkdir(parents=True, exist_ok=True)
        (stage / "update-source.txt").write_text(str(feed), encoding="utf-8")
    for version in versions:
        (stage / "payload-version.txt").write_text(version, encoding="utf-8")
        _vpk_pack(stage, version, feed)
        if version == versions[0]:
            archives = list(feed.glob("*Portable.zip"))
            assert len(archives) == 1, [str(p) for p in feed.iterdir()]
            with zipfile.ZipFile(archives[0]) as z:
                for member in z.infolist():
                    dst = live / member.filename
                    assert dst.resolve().is_relative_to(live.resolve())
                    if member.is_dir():
                        dst.mkdir(parents=True, exist_ok=True)
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(z.read(member))
            assert (live / "current" / "payload-version.txt").read_text(
                encoding="utf-8") == versions[0]
    (OUT / f"{tag}-feed.txt").write_text(feed.name, encoding="utf-8")
    info = {"case": str(case), "live": str(live), "feed": str(feed),
            "pack_id": "MaaRM.RealShellProbe", "stub": "MaaRMRealShellProbe.exe",
            "versions": list(versions)}
    (OUT / f"{tag}-case.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PACK_OK_PAIR", versions, live.name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["sdk", "tool", "probe", "real-shell",
                                           "pack-real-shell", "managed-shell",
                                           "managed-shell-minimal", "pack-managed",
                                           "pack-p0-tail", "publish-fd",
                                           "managed-shell-nl", "managed-shell-v1",
                                           "managed-shell-v1fd", "pure-sc-publish",
                                           "formal-shell", "pack-formal"])
    args = parser.parse_args()
    sdk()
    if args.action == "tool":
        tool()
    elif args.action == "probe":
        publish(HERE / "Probe/HookProbe.csproj", OUT / "publish")
    elif args.action == "real-shell":
        real_shell()
    elif args.action == "pack-real-shell":
        feed = OUT / ("real-shell-feed-" + uuid.uuid4().hex[:8])
        cmd = [str(OUT / "cli/vpk.exe"), "pack", "--packId", "MaaRM.RealShellProbe",
               "--packTitle", "MaaRMRealShellProbe", "--packVersion", "0.1.0",
               "--runtime", "win-x64", "--packDir", str(OUT / "real-shell-publish"),
               "--mainExe", "MaaRacingMaster.Shell.exe", "--outputDir", str(feed),
               "--noInst", "--delta", "None"]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        (OUT / "real-shell-pack.log").write_text(p.stdout + p.stderr, encoding="utf-8")
        assert p.returncode == 0, p.stdout + p.stderr
        (OUT / "real-shell-feed.txt").write_text(feed.name, encoding="utf-8")
        print("REAL_SHELL_PACK_OK")
    elif args.action == "managed-shell":
        managed_shell()
    elif args.action == "managed-shell-minimal":
        managed_shell(minimal=True)
    elif args.action == "publish-fd":
        # 同一份仪器化代码按「依赖框架」发布（对照自包含发布的 WebView2 挂起）
        copy = OUT / "managed-copy"
        csproj = copy / "apps" / "MaaRacingMaster.Shell" / "MaaRacingMaster.Shell.csproj"
        assert csproj.is_file(), "先运行 managed-shell 准备隔离副本"
        publish(csproj, OUT / "managed-publish-fd",
                ["-r", "win-x64", "--self-contained", "false", "-p:Version=0.1.0"],
                timeout=600)
    elif args.action == "managed-shell-nl":
        managed_shell(nl=True, fd=True, stage=False)
    elif args.action == "managed-shell-v1":
        # v1 复刻：生产同款自包含发布 + 完整 Program 注入，但无 MainWindow 双初始化仪器
        managed_shell(fd=False, stage=False, mw_inject=False, out_name="v1")
    elif args.action == "managed-shell-v1fd":
        # K2 对照：依赖框架发布 + 完整 Program 注入，无 MainWindow 双初始化仪器
        managed_shell(fd=True, stage=False, mw_inject=False, out_name="v1fd")
    elif args.action == "pure-sc-publish":
        # K1 对照：纯净工程自包含发布（无 Velopack、无任何注入）
        publish(REPO / "apps/MaaRacingMaster.Shell/MaaRacingMaster.Shell.csproj",
                OUT / "pure-sc-publish", MANAGED_PUBLISH_ARGS, timeout=900)
    elif args.action == "pack-managed":
        pack_managed()
    elif args.action == "pack-p0-tail":
        pack_p0_tail()
    elif args.action == "formal-shell":
        formal_shell()
    elif args.action == "pack-formal":
        pack_formal()


if __name__ == "__main__":
    main()
