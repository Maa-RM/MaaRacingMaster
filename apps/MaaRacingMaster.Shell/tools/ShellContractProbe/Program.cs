using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using MaaRacingMaster.Shell;
using Velopack;
using Velopack.Locators;

namespace MaaRacingMaster.Shell.ContractProbe;

/// <summary>
/// 壳合同探针：对正式源码的路径解析/更新钩子/侧车退出/更新协调器行为做可机检断言。
/// 全过 → 退出码 0；任何失败逐条打印并退出码 1。
/// 用法：dotnet run --project tools/ShellContractProbe -- approot|hook|sidecar
///   update 用例需显式夹具：-- --root <受管安装树> --feed <更新源目录>
/// </summary>
internal static class Program
{
    private static int s_failures;

    private static void Check(string name, bool ok, string detail)
    {
        Console.WriteLine((ok ? "[PASS] " : "[FAIL] ") + name + (ok ? string.Empty : " —— " + detail));
        if (!ok) s_failures++;
    }

    private static int Main(string[] args)
    {
        // 子进程形态：先跑更新钩子（与正式壳同一位置语义），普通启动才会到达标记写入。
        var markerIdx = Array.IndexOf(args, "--hook-child");
        if (markerIdx >= 0)
        {
            VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
            File.WriteAllText(args[markerIdx + 1], "after-run");
            return 0;
        }

        var mode = args.Length > 0 ? args[0] : "";
        switch (mode)
        {
            case "approot": CheckAppRoot(); break;
            case "hook": CheckHook(args); break;
            case "sidecar": CheckSidecarAsync(args).GetAwaiter().GetResult(); break;
            case "cleanup": CheckCleanup(args); break;
            case "update": CheckUpdateCoordinator(args).GetAwaiter().GetResult(); break;
            default:
                Console.Error.WriteLine("用法: ShellContractProbe approot|hook|sidecar");
                return 2;
        }
        Console.WriteLine(s_failures == 0 ? "ALL PASS" : $"FAILURES: {s_failures}");
        return s_failures == 0 ? 0 : 1;
    }

    // ---------- 更新钩子自退出 ----------

    /// <summary>以子进程实测固定版本库的钩子契约：钩子参数 → 库内自退出（后续代码不执行）；
    /// 普通启动 → Run 返回继续执行。P0 打包契约检查只能证明「钩子会退出」，这里补证明
    /// 「退出发生在库内、早于任何后续启动逻辑」。</summary>
    private static void CheckHook(string[] args)
    {
        var self = Environment.ProcessPath
            ?? throw new InvalidOperationException("探针需要以独立进程方式运行");
        var work = MakeTempDir("hook");
        var hookMarker = Path.Combine(work, "hook.marker");
        var normalMarker = Path.Combine(work, "normal.marker");
        var hookDir = Path.Combine(work, "install-target");
        Directory.CreateDirectory(hookDir);

        var sw = Stopwatch.StartNew();
        // 钩子参数必须在命令行首位（更新器真实调起形态）；子进程里 Run 之后的参数仅用于定位标记
        var hookRun = RunChild(self, $"--veloapp-install {Quote(hookDir)} --hook-child {Quote(hookMarker)}");
        var hookElapsed = sw.ElapsedMilliseconds;
        Check("钩子进程快速自退出（<10s）", hookRun.TimedOut == false && hookElapsed < 10_000,
              hookRun.TimedOut == true ? "超时被强杀" : $"{hookElapsed}ms");
        // 退出码是库自己的选择（合成假目标下实测 -1，真实钩子流程的退出码归受管链复验）；
        // 壳侧契约只钉：库内自退出、早于任何后续启动逻辑。
        Check("钩子路径不执行后续代码（标记未写）", !File.Exists(hookMarker), "钩子后标记文件存在");

        var normalRun = RunChild(self, $"--hook-child {Quote(normalMarker)}");
        Check("普通启动子进程退出码 0", normalRun.TimedOut == false && normalRun.ExitCode == 0,
              normalRun.TimedOut == true ? "超时被强杀" : $"exit={normalRun.ExitCode?.ToString() ?? "<未知>"}");
        Check("普通启动 Run 返回后继续执行（标记已写）", File.Exists(normalMarker),
              "无钩子参数时 Run 未返回或未继续");

        // 静态断言的另一半：同一进程内无钩子参数时 Run 直接返回（能走到这里本身就是证据）
        VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
        Check("探针自身普通启动 Run 直通（能执行到这里）", true, "");
    }

    private sealed record ChildRun(int? ExitCode, bool TimedOut);

    // ---------- 宿主更新协调器合同 ----------

    /// <summary>协调器合同（真实库 + 本地源 + 显式定位器，树外驱动）：
    /// ①非受管降级 ②来源未配置诚实不可用 ③受管检查→下载启动即回包→状态查询观察到
    /// 就绪→重复下载幂等→非下载态取消被拒→提交闸门拒绝回 ready 且带原因→revision 单调
    /// ④镜像回退检查 ⑤下载失败镜像同目标回退 ⑥交接失败回 ready 且不排关窗。
    /// 夹具显式传入：--root &lt;受管安装树&gt; --feed &lt;更新源目录&gt;。</summary>
    private static async Task CheckUpdateCoordinator(string[] args)
    {
        var (live, feed) = ResolveUpdateFixture(args);
        var marker = Path.Combine(live, "current", "payload-version.txt");
        var liveVersion = File.ReadAllText(marker).Trim();

        // ① 非受管：所有动作诚实降级
        var degraded = new UpdateCoordinator(null, managed: false);
        var s0 = await degraded.CheckAsync();
        Check("非受管 → 检查返回降级快照", s0.Phase == UpdateSnapshot.Unavailable
              && (s0.Error ?? "").Contains("非受管", StringComparison.Ordinal),
              $"phase={s0.Phase} error={s0.Error}");

        // ② 来源未配置：不伪造检查结果
        var noFeed = new UpdateCoordinator(null, managed: true);
        var s1 = await noFeed.CheckAsync();
        Check("来源未配置 → 诚实不可用", (s1.Error ?? "").Contains("未配置", StringComparison.Ordinal),
              $"error={s1.Error}");

        // ③ 受管 + 本地源 + 显式定位器（树外驱动，规避树内次要 exe 挂起）
        var gateCalls = 0;
        string? gateReason = null;
        var coordinator = new UpdateCoordinator(feed, managed: true,
            locator: MakeExplicitLocator(live, liveVersion),
            prepareGate: () => { gateCalls++; return Task.FromResult(gateReason); });

        var s2 = await coordinator.CheckAsync();
        Check("受管检查 → available 且带目标版本",
              s2.Phase == UpdateSnapshot.Available && !string.IsNullOrEmpty(s2.TargetVersion),
              $"phase={s2.Phase} target={s2.TargetVersion} error={s2.Error}");

        // ③-1 启动即回包（评审回归：等待整段下载会让首次下载期间界面拿不到进度、无法取消）：
        // 清掉包缓存走真实拷贝，启动调用应立即返回 downloading，进度由独立状态查询推进。
        foreach (var cached in Directory.GetFiles(Path.Combine(live, "packages"), "*.nupkg"))
            File.Delete(cached);
        var sw = System.Diagnostics.Stopwatch.StartNew();
        var s3 = await coordinator.StartDownloadAsync();
        sw.Stop();
        Check("下载启动即回包 → downloading（不等整段下载）",
              s3.Phase == UpdateSnapshot.Downloading && sw.ElapsedMilliseconds < 1500,
              $"phase={s3.Phase} elapsed={sw.ElapsedMilliseconds}ms error={s3.Error}");
        var deadline = DateTime.UtcNow.AddSeconds(120);
        UpdateSnapshot polled;
        do
        {
            await Task.Delay(200).ConfigureAwait(false); // 前端状态轮询的等价物
            polled = coordinator.GetStatus();
        } while (polled.Phase == UpdateSnapshot.Downloading && DateTime.UtcNow < deadline);
        Check("独立状态查询观察下载完成 → ready 且进度满",
              polled.Phase == UpdateSnapshot.Ready && polled.Progress == 100,
              $"phase={polled.Phase} progress={polled.Progress} error={polled.Error}");

        var s4 = await coordinator.StartDownloadAsync();
        Check("就绪后重复下载 → 幂等返回就绪", s4.Phase == UpdateSnapshot.Ready
              && s4.Revision >= polled.Revision, $"phase={s4.Phase} rev={s4.Revision}");

        var s5 = coordinator.CancelDownload();
        Check("非下载态取消 → 拒绝", s5.Phase == UpdateSnapshot.Ready
              && (s5.Error ?? "").Contains("不允许", StringComparison.Ordinal),
              $"phase={s5.Phase} error={s5.Error}");

        gateReason = "任务运行中，等待停止后再升级"; // 模拟后端拒绝冻结
        var s6 = await coordinator.ApplyAsync();
        Check("提交闸门拒绝 → 状态回 ready 且带原因",
              s6.Phase == UpdateSnapshot.Ready && (s6.Error ?? "").Contains("任务运行中", StringComparison.Ordinal),
              $"phase={s6.Phase} error={s6.Error}");
        Check("闸门拒绝路径确实询问过后端", gateCalls == 1, $"gateCalls={gateCalls}");

        gateReason = null; // 模拟闸门放行——探针到此为止：真实提交留给受管整链（会触发整树替换）
        var revBefore = coordinator.GetStatus().Revision;
        var s7 = await coordinator.CheckAsync(); // 刷新目标：状态实质变化 → revision 前进
        Check("revision 随状态实质变化单调前进", s7.Revision > revBefore,
              $"rev {revBefore} → {s7.Revision}");
        Check("闸门放行前不触发提交（本轮未调 Apply）", gateCalls == 1, $"gateCalls={gateCalls}");

        // ④ 镜像回退（同目标）：多行来源按序尝试，主源失败锁镜像；全部失败诚实报错不换目标
        var deadDir = Path.Combine(MakeTempDir("dead-source"), "empty");
        Directory.CreateDirectory(deadDir);
        var mirror = new UpdateCoordinator(deadDir + "\n" + feed, managed: true,
            locator: MakeExplicitLocator(live, liveVersion));
        var sm = await mirror.CheckAsync();
        Check("主源不可达 → 镜像回退检查成功",
              sm.Phase == UpdateSnapshot.Available && !string.IsNullOrEmpty(sm.TargetVersion),
              $"phase={sm.Phase} target={sm.TargetVersion} error={sm.Error}");

        var allDead = new UpdateCoordinator(deadDir + "\n" + deadDir, managed: true,
            locator: MakeExplicitLocator(live, liveVersion));
        var sd = await allDead.CheckAsync();
        Check("全部源不可达 → 诚实失败（可重试，不换目标）",
              sd.Phase == UpdateSnapshot.Idle && (sd.Error ?? "").Contains("不可达", StringComparison.Ordinal),
              $"phase={sd.Phase} error={sd.Error}");

        // ⑤ 下载失败镜像回退（评审回归：索引可达不等于包可达）——主源只有索引没有包，
        // 镜像完整；下载失败自动换到同目标镜像并完成，最终 ready。
        var indexOnly = Path.Combine(MakeTempDir("index-only"), "feed");
        Directory.CreateDirectory(indexOnly);
        foreach (var f in Directory.GetFiles(feed))
            if (!f.EndsWith(".nupkg", StringComparison.OrdinalIgnoreCase))
                File.Copy(f, Path.Combine(indexOnly, Path.GetFileName(f)));
        foreach (var cached in Directory.GetFiles(Path.Combine(live, "packages"), "*.nupkg"))
            File.Delete(cached);
        var fallback = new UpdateCoordinator(indexOnly + "\n" + feed, managed: true,
            locator: MakeExplicitLocator(live, liveVersion));
        var sf1 = await fallback.CheckAsync();
        Check("索引可达包缺失 → 检查仍 available", sf1.Phase == UpdateSnapshot.Available,
              $"phase={sf1.Phase} error={sf1.Error}");
        var sf2 = await fallback.StartDownloadAsync();
        var fbDeadline = DateTime.UtcNow.AddSeconds(120);
        UpdateSnapshot fb;
        do
        {
            await Task.Delay(200).ConfigureAwait(false);
            fb = fallback.GetStatus();
        } while (fb.Phase == UpdateSnapshot.Downloading && DateTime.UtcNow < fbDeadline);
        Check("包下载失败 → 镜像同目标回退完成下载 → ready",
              fb.Phase == UpdateSnapshot.Ready && fb.Progress == 100,
              $"phase={fb.Phase} progress={fb.Progress} target={fb.TargetVersion} error={fb.Error}");

        // ⑥ 交接失败恢复（评审回归：更新器启动失败不得把关窗变成既成事实）——
        // 定位器指向不存在的 Update.exe：状态回 ready 带原因，关窗事件不触发。
        var applyFired = 0;
        var broken = new UpdateCoordinator(feed, managed: true,
            locator: new TestVelopackLocator("MaaRM.RealShellProbe", liveVersion,
                Path.Combine(live, "packages"), Path.Combine(live, "current"), live,
                Path.Combine(live, "missing-dir", "Update.exe")),
            prepareGate: () => Task.FromResult<string?>(null));
        broken.ApplyingStarted += () => applyFired++;
        var sb1 = await broken.CheckAsync();
        var sb2 = await broken.StartDownloadAsync();
        var sbDeadline = DateTime.UtcNow.AddSeconds(120);
        UpdateSnapshot sb;
        do
        {
            await Task.Delay(200).ConfigureAwait(false);
            sb = broken.GetStatus();
        } while (sb.Phase == UpdateSnapshot.Downloading && DateTime.UtcNow < sbDeadline);
        var sb3 = await broken.ApplyAsync();
        Check("更新器启动失败 → 状态回 ready 带原因（非 applying）",
              sb3.Phase == UpdateSnapshot.Ready && (sb3.Error ?? "").Contains("启动失败", StringComparison.Ordinal),
              $"phase={sb3.Phase} error={sb3.Error}");
        Check("更新器启动失败 → 关窗事件未触发", applyFired == 0, $"applyFired={applyFired}");
    }

    /// <summary>显式定位器：与实验 HookProbe 同形态，从树外驱动指定受管根。</summary>
    private static TestVelopackLocator MakeExplicitLocator(string live, string liveVersion)
    {
        return new TestVelopackLocator(
            "MaaRM.RealShellProbe", liveVersion,
            Path.Combine(live, "packages"),
            Path.Combine(live, "current"), live,
            Path.Combine(live, "Update.exe"));
    }

    private static (string Live, string Feed) ResolveUpdateFixture(string[] args)
    {
        // 夹具显式传入（正式工具不依赖任何实验输出目录）：--root 受管安装树，
        // --feed 更新源目录。隔离夹具的准备属于验证环境，不进正式工程。
        var root = GetOption(args, "--root");
        var feed = GetOption(args, "--feed");
        if (root is null || feed is null)
            throw new InvalidOperationException(
                "update 用例必须显式给夹具：--root <受管安装树> --feed <更新源目录>");
        return (root, feed);
    }

    // ---------- 启动清场（遗留侧车扫描与终止） ----------

    /// <summary>造一个命令行含侧车模块标记的 python 子进程模拟遗留侧车，实测
    /// 清理路径的命中与耗时（命令行经 ntdll PEB 直读）。</summary>
    private static void CheckCleanup(string[] args)
    {
        var python = GetOption(args, "--python") ?? "python";
        using var child = Process.Start(new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-c \"import time; print('READY', flush=True); time.sleep(60)\" " +
                        "maaracing_master.core.sidecar",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
        });
        var ready = child!.StandardOutput.ReadLine();
        Check("模拟遗留侧车已就绪", ready == "READY", $"stdout={ready ?? "<空>"}");

        // 应用根取解释器上两层目录（venv 场景：.venv/Scripts/python.exe → venv 父目录）
        var fullPython = Path.GetFullPath(python);
        var appRoot = Path.GetDirectoryName(Path.GetDirectoryName(fullPython))!;
        var sw = Stopwatch.StartNew();
        var killed = PythonSidecar.CleanupLeftovers(appRoot);
        var elapsed = sw.ElapsedMilliseconds;
        Check("遗留侧车扫描+清理命中 1 个", killed == 1, $"killed={killed}");
        Check("清理毫秒级完成（<3s）", elapsed < 3_000, $"{elapsed}ms");
        Check("目标进程已退出", child.WaitForExit(10_000) && child.HasExited,
              $"exit={child.HasExited}");
    }

    // ---------- 侧车关停合同 ----------

    /// <summary>侧车关停合同：① 自退进程 → 死亡事件带退出码；② 优雅关停（协议应答）→
    /// grace 内确认退出、退出码可得；③ 无应答关停 → 强杀后有界等待确认，不无限等也不吞结果。
    /// 用法可带 --python &lt;解释器路径&gt;，缺省 "python"。</summary>
    private static async Task CheckSidecarAsync(string[] args)
    {
        var python = GetOption(args, "--python") ?? "python";
        var work = MakeTempDir("sidecar");
        var exitScript = Path.Combine(work, "exit_child.py");
        File.WriteAllText(exitScript, "import time\ntime.sleep(0.3)\n");
        var gracefulScript = Path.Combine(work, "graceful_child.py");
        File.WriteAllText(gracefulScript,
            "import json, sys\n" +
            "for line in sys.stdin:\n" +
            "    try:\n" +
            "        req = json.loads(line)\n" +
            "    except Exception:\n" +
            "        continue\n" +
            "    if req.get('type') == 'request' and req.get('method') == 'shutdown':\n" +
            "        sys.stdout.write(json.dumps({'type': 'response', 'id': req.get('id'), 'ok': True}) + '\\n')\n" +
            "        sys.stdout.flush()\n" +
            "        break\n");
        var silentScript = Path.Combine(work, "silent_child.py");
        File.WriteAllText(silentScript, "import time\nwhile True:\n    time.sleep(1)\n");

        // ① 自退：EOF → BackendExited 事件（非关停路径的唯一死亡上报）
        var exited = new TaskCompletionSource<int?>(TaskCreationOptions.RunContinuationsAsynchronously);
        var sc1 = new PythonSidecar(python, exitScript);
        sc1.BackendExited += code => exited.TrySetResult(code);
        try
        {
            var code1 = await exited.Task.WaitAsync(TimeSpan.FromSeconds(10));
            Check("自退进程 → 死亡事件带退出码", true, $"exit={code1?.ToString() ?? "<未知>"}");
        }
        catch (TimeoutException)
        {
            Check("自退进程 → 死亡事件带退出码", false, "10s 内未收到 BackendExited");
        }

        // ② 优雅关停：协议应答后 grace 内退出
        var sc2 = new PythonSidecar(python, gracefulScript);
        var sw = Stopwatch.StartNew();
        var r2 = await sc2.ShutdownAsync(TimeSpan.FromSeconds(5));
        var elapsed2 = sw.ElapsedMilliseconds;
        Check("优雅关停 → 确认退出且退出码可得",
              r2.ConfirmedExit && r2.ExitCode == 0,
              $"ConfirmedExit={r2.ConfirmedExit} exit={r2.ExitCode?.ToString() ?? "<未知>"}");
        Check("优雅关停 → 未走到强杀（耗时 < grace）", elapsed2 < 5_000, $"{elapsed2}ms");

        // ③ 无应答：grace 超时强杀 → 有界等待确认退出
        var sc3 = new PythonSidecar(python, silentScript);
        sw = Stopwatch.StartNew();
        var r3 = await sc3.ShutdownAsync(TimeSpan.FromSeconds(1));
        var elapsed3 = sw.ElapsedMilliseconds;
        Check("无应答强杀 → 有界等待后确认退出",
              r3.ConfirmedExit && elapsed3 < 15_000,
              $"ConfirmedExit={r3.ConfirmedExit} {elapsed3}ms");
    }

    private static string? GetOption(string[] args, string name)
    {
        var idx = Array.IndexOf(args, name);
        return idx >= 0 && idx + 1 < args.Length ? args[idx + 1] : null;
    }

    private static ChildRun RunChild(string fileName, string arguments)
    {
        using var p = Process.Start(new ProcessStartInfo
        {
            FileName = fileName,
            Arguments = arguments,
            UseShellExecute = false,
            CreateNoWindow = true,
        });
        if (p is null)
            return new ChildRun(null, TimedOut: true);
        if (!p.WaitForExit(30_000))
        {
            p.Kill(entireProcessTree: true);
            return new ChildRun(null, TimedOut: true);
        }
        return new ChildRun(p.ExitCode, TimedOut: false);
    }

    private static string Quote(string s) => "\"" + s + "\"";

    // ---------- 应用根解析 ----------

    private static string MakeTempDir(string label)
    {
        var dir = Path.Combine(Path.GetTempPath(),
            "maarm-shell-probe-" + label + "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(dir);
        return dir;
    }

    /// <summary>受管标准布局夹具：install/Update.exe + install/current（壳 exe 所在内容根）。</summary>
    private static (string InstallRoot, string ContentRoot) MakeManagedFixture()
    {
        var tmp = MakeTempDir("managed");
        var content = Path.Combine(tmp, "install", "current");
        Directory.CreateDirectory(content);
        File.WriteAllText(Path.Combine(tmp, "install", "Update.exe"), string.Empty);
        return (Path.Combine(tmp, "install"), content);
    }

    private static string Norm(string p) => Path.GetFullPath(p);

    private static void CheckAppRoot()
    {
        // 受管：旧环境变量覆盖值与内容根冲突 → 忽略并记录，内容根为准
        var (installRoot, contentRoot) = MakeManagedFixture();
        var envOverride = Norm(Path.Combine(MakeTempDir("oldenv"), "old-root"));
        Directory.CreateDirectory(envOverride);
        var r = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe" }, envOverride, contentRoot, @"AppData\Roaming");
        Check("受管+冲突环境覆盖 → 内容根为准", r.Root == contentRoot && r.Managed,
              $"Root={r.Root} Managed={r.Managed}");
        Check("受管+冲突环境覆盖 → 记录被拒覆盖值",
              r.IgnoredOverride is not null && r.IgnoredOverride.Contains(envOverride, StringComparison.OrdinalIgnoreCase),
              $"IgnoredOverride={r.IgnoredOverride}");
        Check("受管+冲突环境覆盖 → 不拒绝启动", r.RefusedReason is null, $"RefusedReason={r.RefusedReason}");

        // 受管：--app-root 参数覆盖值与内容根冲突 → 同样忽略
        var argOverride = Norm(Path.Combine(MakeTempDir("oldarg"), "old-root"));
        Directory.CreateDirectory(argOverride);
        var r2 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe", "--app-root", argOverride }, null, contentRoot, @"AppData\Roaming");
        Check("受管+冲突参数覆盖 → 内容根为准", r2.Root == contentRoot && r2.Managed,
              $"Root={r2.Root} Managed={r2.Managed}");
        Check("受管+冲突参数覆盖 → 记录被拒覆盖值",
              r2.IgnoredOverride is not null && r2.IgnoredOverride.Contains(argOverride, StringComparison.OrdinalIgnoreCase),
              $"IgnoredOverride={r2.IgnoredOverride}");

        // 受管：覆盖值指向内容根本身（大小写/尾分隔符差异）→ 不算冲突
        var r3 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe", "--app-root", contentRoot + "\\" }, null, contentRoot, @"AppData\Roaming");
        Check("受管+覆盖值=内容根 → 非冲突", r3.Root == contentRoot && r3.IgnoredOverride is null,
              $"Root={r3.Root} IgnoredOverride={r3.IgnoredOverride}");

        // 受管：%APPDATA% 缺失 → 拒绝启动（用户数据不得落进可被整体替换的内容树）
        var r4 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, contentRoot, "");
        Check("受管+APPDATA 缺失 → 拒绝启动", r4.RefusedReason is not null,
              $"RefusedReason={r4.RefusedReason ?? "<空>"}");
        var r5 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, contentRoot, @"AppData\Roaming");
        Check("受管+APPDATA 可用 → 不拒绝", r5.RefusedReason is null, $"RefusedReason={r5.RefusedReason}");

        // 非受管：历史口径不变（参数 > 环境变量 > 向上探测）
        var proj = MakeTempDir("proj");
        var deep = Path.Combine(proj, "sub", "deep");
        Directory.CreateDirectory(deep);
        File.WriteAllText(Path.Combine(proj, "pyproject.toml"), string.Empty);

        var argRoot = Norm(Path.Combine(proj, "via-arg"));
        var r6 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe", "--app-root", argRoot }, @"ignored-env", deep, @"AppData\Roaming");
        Check("非受管+参数 → 参数优先", r6.Root == argRoot && !r6.Managed, $"Root={r6.Root}");

        var envRoot = Norm(Path.Combine(proj, "via-env"));
        var r7 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe" }, envRoot, deep, @"AppData\Roaming");
        Check("非受管+环境变量 → 环境变量优先于探测", r7.Root == envRoot && !r7.Managed, $"Root={r7.Root}");

        var r8 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, deep, @"AppData\Roaming");
        Check("非受管+无覆盖 → 向上探测 pyproject.toml", r8.Root == proj && !r8.Managed, $"Root={r8.Root}");

        var emptyBase = Path.Combine(MakeTempDir("bare"), "a", "b");
        Directory.CreateDirectory(emptyBase);
        var r9 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, emptyBase, @"AppData\Roaming");
        Check("非受管+无任何锚 → null（壳自行降级）", r9.Root is null && !r9.Managed, $"Root={r9.Root}");

        // 用户数据根：与 paths.py 同口径（夹具用相对路径，避免源码落本机绝对路径）
        Check("用户数据根:APPDATA 优先",
              UserDataRoot.Resolve(@"AppRoot", @"AppData\Roaming") == @"AppData\Roaming\MaaRacingMaster",
              UserDataRoot.Resolve(@"AppRoot", @"AppData\Roaming") ?? "<空>");
        Check("用户数据根:无 APPDATA 回退应用根旁 data",
              UserDataRoot.Resolve(@"AppRoot", "") == @"AppRoot\data",
              UserDataRoot.Resolve(@"AppRoot", "") ?? "<空>");
    }
}
