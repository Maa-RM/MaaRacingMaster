using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Threading;
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
            case "settings": CheckUpdateSettingsAsync().GetAwaiter().GetResult(); break;
            default:
                Console.Error.WriteLine("用法: ShellContractProbe approot|hook|sidecar|cleanup|update|settings");
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
        var (live, feedRaw) = ResolveUpdateFixture(args);
        var marker = Path.Combine(live, "current", "payload-version.txt");
        var liveVersion = File.ReadAllText(marker).Trim();
        // 协调器总是显式传渠道（默认 stable）：夹具索引若是历史 win 命名，
        // 先在临时目录改名 staging，夹具本身保持渠道无关。
        var feed = EnsureStableIndex(feedRaw);

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

        // ⑥ 交接失败恢复（评审回归二轮）：真实闸门在通过前会确认后端退出——闸门桩把
        // 后端置为已退出，交接失败（定位器指向不存在的 Update.exe）后必须恢复后端，
        // 否则「旧版可继续使用」是假话（业务查询与重试升级都撞上已退出的后端）。
        var applyFired = 0;
        var backendAlive = true;
        var brokenGateCalls = 0;
        var recoveryCalls = 0;
        var broken = new UpdateCoordinator(feed, managed: true,
            locator: new TestVelopackLocator("MaaRM.RealShellProbe", liveVersion,
                Path.Combine(live, "packages"), Path.Combine(live, "current"), live,
                Path.Combine(live, "missing-dir", "Update.exe")),
            prepareGate: () => { brokenGateCalls++; backendAlive = false; return Task.FromResult<string?>(null); },
            handoffRecovery: () => { recoveryCalls++; backendAlive = true; return Task.FromResult(true); });
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
        Check("交接失败 → 恢复委托被调用且后端恢复可用",
              backendAlive && recoveryCalls == 1, $"backendAlive={backendAlive} recoveryCalls={recoveryCalls}");
        Check("恢复成功 → 提示与实际能力一致（可重试升级）",
              (sb3.Error ?? "").Contains("可重试", StringComparison.Ordinal), $"error={sb3.Error}");
        var sb4 = await broken.ApplyAsync(); // 恢复后重试升级：闸门再次走完整生命周期
        Check("恢复后重试升级 → 闸门再次冻结并再次恢复",
              sb4.Phase == UpdateSnapshot.Ready && brokenGateCalls == 2 && recoveryCalls == 2 && backendAlive,
              $"phase={sb4.Phase} gateCalls={brokenGateCalls} recoveryCalls={recoveryCalls} backendAlive={backendAlive}");

        // ⑥-2 恢复失败路径：提示必须诚实（不谎称旧版可继续使用）
        var broken2 = new UpdateCoordinator(feed, managed: true,
            locator: new TestVelopackLocator("MaaRM.RealShellProbe", liveVersion,
                Path.Combine(live, "packages"), Path.Combine(live, "current"), live,
                Path.Combine(live, "missing-dir", "Update.exe")),
            prepareGate: () => Task.FromResult<string?>(null),
            handoffRecovery: () => Task.FromResult(false));
        await broken2.CheckAsync();
        var b2d = await broken2.StartDownloadAsync();
        var b2Deadline = DateTime.UtcNow.AddSeconds(120);
        UpdateSnapshot b2w;
        do
        {
            await Task.Delay(200).ConfigureAwait(false);
            b2w = broken2.GetStatus();
        } while (b2w.Phase == UpdateSnapshot.Downloading && DateTime.UtcNow < b2Deadline);
        var b3 = await broken2.ApplyAsync();
        Check("恢复失败 → 提示明确要求重启应用（不谎称可继续使用）",
              (b3.Error ?? "").Contains("重启", StringComparison.Ordinal), $"error={b3.Error}");

        // ⑦ 检查与下载并发（评审回归二轮）：已有目标后再次检查、检查在途时点击仍在
        // 显示的下载按钮——迟到的检查结果不得覆盖 downloading（否则进度消失、取消失效、
        // 下载完成进不了 ready）。本地慢 HTTP 源：索引响应延迟制造检查窗口。
        using var slowHttp = new SlowHttpServer(
            File.ReadAllText(Path.Combine(feed, "releases.stable.json")),
            TimeSpan.FromMilliseconds(1200), TimeSpan.FromSeconds(6));
        var concurrent = new UpdateCoordinator($"http://127.0.0.1:{slowHttp.Port}", managed: true,
            locator: MakeExplicitLocator(live, liveVersion));
        foreach (var cached in Directory.GetFiles(Path.Combine(live, "packages"), "*.nupkg"))
            File.Delete(cached);
        var s7a = await concurrent.CheckAsync(); // 第一次检查（慢）→ 锁定 HTTP 源
        Check("HTTP 源检查 → available 且带目标版本",
              s7a.Phase == UpdateSnapshot.Available && !string.IsNullOrEmpty(s7a.TargetVersion),
              $"phase={s7a.Phase} target={s7a.TargetVersion} error={s7a.Error}");
        var lateCheck = concurrent.CheckAsync(); // 第二次检查在途（索引延迟中）
        await Task.Delay(100);
        var s7b = await concurrent.StartDownloadAsync(); // 检查在途时点仍显示的下载按钮
        Check("检查在途时下载可发起 → downloading",
              s7b.Phase == UpdateSnapshot.Downloading, $"phase={s7b.Phase} error={s7b.Error}");
        var s7c = await lateCheck; // 迟到的检查结果返回
        Check("迟到的检查结果不覆盖下载状态",
              s7c.Phase == UpdateSnapshot.Downloading, $"phase={s7c.Phase} error={s7c.Error}");
        var s7d = concurrent.CancelDownload();
        var c7Deadline = DateTime.UtcNow.AddSeconds(15);
        UpdateSnapshot s7e;
        do
        {
            await Task.Delay(100).ConfigureAwait(false); // 前端状态轮询的等价物：取消异步生效
            s7e = concurrent.GetStatus();
        } while (s7e.Phase == UpdateSnapshot.Downloading && DateTime.UtcNow < c7Deadline);
        Check("下载中取消有效（不被迟到检查破坏）",
              s7e.Phase == UpdateSnapshot.Available && (s7e.Error ?? "").Contains("取消", StringComparison.Ordinal),
              $"phase={s7e.Phase} error={s7e.Error}");
    }

    /// <summary>渠道/下载源设置的真实行为验证：自建最小 feed 索引，让协调器真的跑 Velopack。
    /// 静态文本锁抓不到「切了到底有没有换索引」——这里覆盖：渠道是用户设置（默认显式
    /// stable）、切回稳定轨真的换索引、从测试轨切回且稳定版未追上时如实标记等待（不降级）、
    /// 下载源只是同一货架的地址选择（切换保留就绪目标、下次检查换新首选）、首选不可达
    /// 回退其余地址、渠道切换作废旧世代检查。不依赖任何实验输出目录，夹具自包含。</summary>
    private static async Task CheckUpdateSettingsAsync()
    {
        var root = MakeTempDir("settings");
        Directory.CreateDirectory(Path.Combine(root, "packages"));
        Directory.CreateDirectory(Path.Combine(root, "current"));
        var locator = new TestVelopackLocator(
            "MaaRM.RealShellProbe", "1.0.0",
            Path.Combine(root, "packages"), Path.Combine(root, "current"), root,
            Path.Combine(root, "Update.exe"));

        // 一个目录里按渠道各放一份索引，版本刻意错开，便于断言「切了真的换了读取目标」。
        string MakeFeed(string label, params (string Channel, string Version)[] channels)
        {
            var dir = Path.Combine(root, label);
            Directory.CreateDirectory(dir);
            foreach (var (channel, version) in channels)
                File.WriteAllText(Path.Combine(dir, $"releases.{channel}.json"), FeedIndex(version));
            return dir;
        }

        // ① 渠道是用户设置：默认显式 stable，不依赖安装包烧录的渠道。
        var defaultFeed = MakeFeed("default", ("stable", "9.0.0"), ("preview", "2.0.0"));
        var def = new UpdateCoordinator(defaultFeed, managed: true, locator: locator);
        var defSnap = await def.CheckAsync();
        Check("默认构造 → 显式走稳定轨索引",
              defSnap.Phase == UpdateSnapshot.Available && defSnap.TargetVersion == "9.0.0"
              && defSnap.Channel == UpdateSnapshot.ChannelStable,
              $"phase={defSnap.Phase} channel={defSnap.Channel} target={defSnap.TargetVersion} error={defSnap.Error}");

        // ② 切渠道：preview → stable，真的换索引。
        var channelFeed = MakeFeed("channel", ("stable", "9.0.0"), ("preview", "2.0.0"));
        var ch = new UpdateCoordinator(channelFeed, managed: true, locator: locator,
            initialChannel: UpdateSnapshot.ChannelPreview);
        var chPreview = await ch.CheckAsync();
        Check("显式测试渠道 → 读 preview 索引",
              chPreview.Phase == UpdateSnapshot.Available && chPreview.TargetVersion == "2.0.0",
              $"phase={chPreview.Phase} target={chPreview.TargetVersion} error={chPreview.Error}");
        var chStable = await ch.SetChannelAsync(UpdateSnapshot.ChannelStable);
        Check("切回稳定轨 → 真的读 stable 索引",
              chStable.Phase == UpdateSnapshot.Available && chStable.TargetVersion == "9.0.0",
              $"phase={chStable.Phase} target={chStable.TargetVersion} error={chStable.Error}");

        // ③ 等待标记：stable 索引版本低于已装版本 → 不做降级安装、无目标，
        //    如实标记「等待稳定轨追上」，不假装「已是最新」。
        var downgradeFeed = MakeFeed("downgrade", ("stable", "0.5.0"), ("preview", "2.0.0"));
        var dg = new UpdateCoordinator(downgradeFeed, managed: true, locator: locator,
            initialChannel: UpdateSnapshot.ChannelPreview);
        await dg.CheckAsync();
        var dgSwitch = await dg.SetChannelAsync(UpdateSnapshot.ChannelStable);
        Check("切回稳定轨且稳定版未追上 → 等待标记为真且无降级目标",
              dgSwitch.Phase == UpdateSnapshot.UpToDate && dgSwitch.AwaitingStable
              && dgSwitch.TargetVersion is null,
              $"phase={dgSwitch.Phase} awaiting={dgSwitch.AwaitingStable} target={dgSwitch.TargetVersion} error={dgSwitch.Error}");

        // ④ 下载源是同一货架的地址选择：切换只换首选——就绪目标保留，下次检查换新首选。
        var cnbFeed = MakeFeed("cnb", ("stable", "3.0.0"));
        var ghFeed = MakeFeed("github", ("stable", "4.0.0"));
        var sc = new UpdateCoordinator($"cnb|{cnbFeed}\ngithub|{ghFeed}", managed: true, locator: locator);
        var scCnb = await sc.CheckAsync();
        Check("首选 cnb → available 且带目标",
              scCnb.Phase == UpdateSnapshot.Available && scCnb.TargetVersion == "3.0.0",
              $"phase={scCnb.Phase} target={scCnb.TargetVersion} error={scCnb.Error}");
        var scGh = await sc.SetSourceAsync(UpdateSnapshot.SourceGithub);
        Check("切下载源 → 就绪目标保留（不清空），仅换首选",
              scGh.Phase == UpdateSnapshot.Available && scGh.TargetVersion == "3.0.0"
              && scGh.Source == UpdateSnapshot.SourceGithub,
              $"phase={scGh.Phase} target={scGh.TargetVersion} source={scGh.Source} error={scGh.Error}");
        var scRecheck = await sc.CheckAsync();
        Check("切源后重查 → 按新首选读到 github 索引",
              scRecheck.Phase == UpdateSnapshot.Available && scRecheck.TargetVersion == "4.0.0",
              $"phase={scRecheck.Phase} target={scRecheck.TargetVersion} error={scRecheck.Error}");

        // ⑤ 首选不可达 → 回退其余等价地址（同一货架，不存在「跨组回退」）。
        var deadDir = Path.Combine(root, "dead");
        Directory.CreateDirectory(deadDir); // 空目录 = 镜像未就绪
        var fallback = new UpdateCoordinator($"cnb|{deadDir}\ngithub|{ghFeed}", managed: true, locator: locator);
        var fbSnap = await fallback.CheckAsync();
        Check("首选不可达 → 回退其余地址完成检查",
              fbSnap.Phase == UpdateSnapshot.Available && fbSnap.TargetVersion == "4.0.0",
              $"phase={fbSnap.Phase} target={fbSnap.TargetVersion} error={fbSnap.Error}");

        // ⑥ 世代守卫：慢检查在途时切渠道（开启新世代），旧世代的迟到结果
        // 不得覆盖新渠道的结果。慢源不分渠道（固定返回旧版本内容），因此
        // 切渠道后再换首选地址重查：快检查先落定，慢检查后返回必须被丢弃。
        using var slow = new SlowHttpServer(FeedIndex("2.0.0"),
            TimeSpan.FromMilliseconds(1000), TimeSpan.FromSeconds(6));
        var epochFeed = $"cnb|http://127.0.0.1:{slow.Port}\ngithub|{ghFeed}";
        var ep = new UpdateCoordinator(epochFeed, managed: true, locator: locator,
            initialChannel: UpdateSnapshot.ChannelPreview);
        var inFlight = ep.CheckAsync();
        await Task.Delay(100);
        var epSwitch = await ep.SetChannelAsync(UpdateSnapshot.ChannelStable);
        Check("切渠道 → 开启新世代并当场重查",
              epSwitch.Phase == UpdateSnapshot.Available
              && epSwitch.Channel == UpdateSnapshot.ChannelStable,
              $"phase={epSwitch.Phase} channel={epSwitch.Channel} target={epSwitch.TargetVersion} error={epSwitch.Error}");
        await ep.SetSourceAsync(UpdateSnapshot.SourceGithub); // 换首选地址，不动世代
        var recheck = await ep.CheckAsync();
        Check("按新首选重查 → 拿到稳定轨目标",
              recheck.Phase == UpdateSnapshot.Available && recheck.TargetVersion == "4.0.0",
              $"phase={recheck.Phase} target={recheck.TargetVersion} error={recheck.Error}");
        var late = await inFlight; // 同步点：确保旧事务彻底结束（丢弃分支的返回快照不保证携带新结果）
        var final = ep.GetStatus();
        Check("旧世代的迟到检查不覆盖新渠道结果",
              final.TargetVersion == "4.0.0" && final.Channel == UpdateSnapshot.ChannelStable
              && !final.AwaitingStable,
              $"late={late.TargetVersion} final={final.TargetVersion}/{final.Channel}/{final.AwaitingStable}");
    }

    /// <summary>最小可用渠道索引：检查阶段只解析 JSON，不需要真实 nupkg（探针既有
    /// 「索引可达包缺失」用例已证实）。SHA 用占位值，只用于版本比较。</summary>
    private static string FeedIndex(string version) =>
        "{\"Assets\":[{\"PackageId\":\"MaaRM.RealShellProbe\",\"Version\":\"" + version +
        "\",\"Type\":\"Full\",\"FileName\":\"MaaRM.RealShellProbe-" + version +
        "-full.nupkg\",\"SHA1\":\"" + new string('0', 40) +
        "\",\"SHA256\":\"" + new string('0', 64) + "\",\"Size\":1}]}";

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

    /// <summary>协调器总是显式传渠道（默认 stable）：夹具索引若是历史 win 命名，
    /// 复制到临时目录并改名，原夹具保持渠道无关、不被写入。</summary>
    private static string EnsureStableIndex(string feed)
    {
        if (File.Exists(Path.Combine(feed, "releases.stable.json"))) return feed;
        var staged = Path.Combine(MakeTempDir("feed-stable"), "feed");
        Directory.CreateDirectory(staged);
        foreach (var f in Directory.GetFiles(feed))
            File.Copy(f, Path.Combine(staged, Path.GetFileName(f)));
        var win = Path.Combine(staged, "releases.win.json");
        if (File.Exists(win))
            File.Move(win, Path.Combine(staged, "releases.stable.json"));
        return staged;
    }

    /// <summary>本地最小 HTTP 源（TcpListener，无 URL ACL 依赖）：索引响应延迟制造
    /// 检查在途窗口，包请求挂起模拟大文件下载（取消会打断真实 HTTP 下载任务）。
    /// 仅 update 用例并发场景用；Dispose 即停服。</summary>
    private sealed class SlowHttpServer : IDisposable
    {
        private readonly TcpListener _listener;
        private readonly CancellationTokenSource _cts = new();
        private readonly string _indexJson;
        private readonly TimeSpan _indexDelay;
        private readonly TimeSpan _packageDelay;

        public SlowHttpServer(string indexJson, TimeSpan indexDelay, TimeSpan packageDelay)
        {
            _indexJson = indexJson;
            _indexDelay = indexDelay;
            _packageDelay = packageDelay;
            _listener = new TcpListener(System.Net.IPAddress.Loopback, 0);
            _listener.Start();
            Port = ((System.Net.IPEndPoint)_listener.LocalEndpoint).Port;
            _ = AcceptLoopAsync();
        }

        public int Port { get; }

        private async Task AcceptLoopAsync()
        {
            while (!_cts.IsCancellationRequested)
            {
                System.Net.Sockets.TcpClient client;
                try { client = await _listener.AcceptTcpClientAsync().WaitAsync(_cts.Token); }
                catch { break; }
                _ = HandleClientAsync(client);
            }
        }

        private async Task HandleClientAsync(System.Net.Sockets.TcpClient client)
        {
            try
            {
                using var _ = client;
                var stream = client.GetStream();
                var head = new byte[4096];
                var n = await stream.ReadAsync(head, _cts.Token);
                var text = System.Text.Encoding.ASCII.GetString(head, 0, n);
                var m0 = text.IndexOf(' ') + 1;
                var m1 = m0 > 0 ? text.IndexOf(' ', m0) : -1;
                var path = m1 > m0 ? text[m0..m1] : "/";
                if (path.Contains("releases", StringComparison.OrdinalIgnoreCase))
                {
                    await Task.Delay(_indexDelay, _cts.Token);
                    var body = System.Text.Encoding.UTF8.GetBytes(_indexJson);
                    await stream.WriteAsync(
                        System.Text.Encoding.ASCII.GetBytes(
                            $"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"),
                        _cts.Token);
                    await stream.WriteAsync(body, _cts.Token);
                }
                else if (path.EndsWith(".nupkg", StringComparison.OrdinalIgnoreCase))
                {
                    // 挂起不响应：模拟大文件下载；取消后由 Dispose/断连收尾
                    await Task.Delay(_packageDelay, _cts.Token);
                }
                else
                {
                    await stream.WriteAsync(
                        System.Text.Encoding.ASCII.GetBytes("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"),
                        _cts.Token);
                }
            }
            catch
            {
                // 客户端断开 / 服务停止：收尾
            }
        }

        public void Dispose()
        {
            _cts.Cancel();
            try { _listener.Stop(); } catch { /* 已停止 */ }
        }
    }

    // ---------- 启动清场（遗留侧车扫描与终止） ----------

    /// <summary>造一个命令行含侧车模块标记的 python 子进程模拟遗留侧车，实测
    /// 清理路径的命中与耗时（命令行经 ntdll PEB 直读）。</summary>
    /// <summary>递归复制目录（自包含解释器迁移用）；目标不存在时创建。</summary>
    private static void CopyDirectory(string src, string dst)
    {
        Directory.CreateDirectory(dst);
        foreach (var f in Directory.GetFiles(src))
            File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), overwrite: true);
        foreach (var d in Directory.GetDirectories(src))
            CopyDirectory(d, Path.Combine(dst, Path.GetFileName(d)));
    }

    private static void CheckCleanup(string[] args)
    {
        // 隔离根（评审回归：扫描根由调用者解释器推导会对整棵目录真实清场——共享虚拟
        // 环境下会波及另一会话的收尾后端）。探针自建唯一临时根并复制自包含解释器，
        // 清场影响范围被构造性限制在本次新建的根内，与调用环境完全解耦。
        var runtimeDir = GetOption(args, "--runtime-dir")
            ?? throw new InvalidOperationException(
                "cleanup 用例必须传 --runtime-dir <自包含 python 目录>（如主线 build/runtime-cache）");
        if (!File.Exists(Path.Combine(runtimeDir, "python.exe")))
            throw new InvalidOperationException($"--runtime-dir 缺 python.exe: {runtimeDir}");
        var isolatedRoot = MakeTempDir("cleanup-root");
        var pyDir = Path.Combine(isolatedRoot, "runtime", "python");
        CopyDirectory(runtimeDir, pyDir);
        var python = Path.Combine(pyDir, "python.exe");
        var appRoot = Path.GetDirectoryName(Path.GetDirectoryName(python))!;

        // ① 孤儿命中例：中间进程 spawn 侧车标记子进程后退出——子进程成为孤儿（父已死），
        // 正是启动清场的清理对象。孙进程 stdout 落文件，父把孙 pid 落文件供探针追踪。
        var work = MakeTempDir("cleanup");
        var spawner = Path.Combine(work, "orphan_spawner.py");
        File.WriteAllText(spawner, string.Join("\n",
            "import subprocess, sys, time",
            $"out = open(sys.argv[1], \"w\", encoding=\"utf-8\")",
            $"pidf = open(sys.argv[2], \"w\", encoding=\"utf-8\")",
            "p = subprocess.Popen([sys.executable, \"-c\",",
            "    \"import time; print('READY', flush=True); time.sleep(60)\",",
            "    \"maaracing_master.core.sidecar\"],",
            "    stdout=out, stderr=subprocess.DEVNULL)",
            "pidf.write(str(p.pid)); pidf.flush()",
            "out.close(); pidf.close()",
            "time.sleep(0.2)"));
        var readyFile = Path.Combine(work, "orphan_ready.txt");
        var pidFile = Path.Combine(work, "orphan_pid.txt");
        using (var mid = Process.Start(new ProcessStartInfo
        {
            FileName = python,
            Arguments = $"\"{spawner}\" \"{readyFile}\" \"{pidFile}\"",
            UseShellExecute = false,
            CreateNoWindow = true,
        }))
        {
            mid!.WaitForExit(10_000); // 中间进程退出 → 孙成孤儿
        }
        var orphanDeadline = DateTime.UtcNow.AddSeconds(15);
        while (!File.Exists(readyFile) && DateTime.UtcNow < orphanDeadline)
            Thread.Sleep(100);
        Check("孤儿遗留侧车已就绪", File.Exists(readyFile), $"readyFile={File.Exists(readyFile)}");
        var orphanPid = int.Parse(File.ReadAllText(pidFile).Trim());
        var sw = Stopwatch.StartNew();
        var killed = PythonSidecar.CleanupLeftovers(appRoot);
        Check("孤儿遗留后端被清场命中 1 个", killed == 1, $"killed={killed}");
        Check("清理毫秒级完成（<3s）", sw.ElapsedMilliseconds < 3_000, $"{sw.ElapsedMilliseconds}ms");
        Process? orphan = null;
        try { orphan = Process.GetProcessById(orphanPid); } catch (ArgumentException) { }
        Check("孤儿后端已终止",
              orphan is null || (orphan.WaitForExit(10_000) && orphan.HasExited),
              $"exit={orphan?.HasExited.ToString() ?? "<已消失>"}");

        // ② 父进程存活反例（评审回归：独立调试后端是贡献指南明确入口，且存在借用
        // 解释器的活跃后端）——父活 = 仍被使用，清场不得终止。
        using var live = Process.Start(new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-c \"import time; print('READY', flush=True); time.sleep(60)\" " +
                        "maaracing_master.core.sidecar",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
        });
        var liveReady = live!.StandardOutput.ReadLine();
        Check("父进程存活的调试后端已就绪", liveReady == "READY", $"stdout={liveReady ?? "<空>"}");
        var killedLive = PythonSidecar.CleanupLeftovers(appRoot);
        Check("父进程存活的调试后端不被清场终止",
              killedLive == 0 && live.HasExited == false,
              $"killed={killedLive} exited={live.HasExited}");
        if (!live.HasExited) // 探针收尾：终止反例进程，不留残留
            Process.GetProcessById(live.Id).Kill(entireProcessTree: true);

        // ③ 跨检出不误杀：其它检出内的侧车标记进程不在清场范围——
        // 多检出并行时，一个检出的壳启动清场绝不能杀另一个检出的后端。
        var foreignPython = GetOption(args, "--python-foreign");
        if (foreignPython is not null)
        {
            using var foreign = Process.Start(new ProcessStartInfo
            {
                FileName = foreignPython,
                Arguments = "-c \"import time; print('FOREIGN_READY', flush=True); time.sleep(60)\" " +
                            "maaracing_master.core.sidecar",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
            });
            var foreignReady = foreign!.StandardOutput.ReadLine();
            Check("外部检出模拟侧车已就绪", foreignReady == "FOREIGN_READY",
                  $"stdout={foreignReady ?? "<空>"}");
            var killedForeign = PythonSidecar.CleanupLeftovers(appRoot);
            Check("跨检出侧车不被清场误杀",
                  killedForeign == 0 && foreign.HasExited == false,
                  $"killed={killedForeign} exited={foreign.HasExited}");
            if (!foreign.HasExited) // 探针收尾：终止反例进程，不留残留
                Process.GetProcessById(foreign.Id).Kill(entireProcessTree: true);
        }
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
