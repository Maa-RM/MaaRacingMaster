using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MaaRacingMaster.Shell;

namespace MaaRacingMaster.Shell.ContractProbe;

/// <summary>
/// 壳合同探针：对正式源码的路径解析/更新设置/自动检查/侧车退出做可机检断言。
/// 全过 → 退出码 0；任何失败逐条打印并退出码 1。
/// 用法：dotnet run --project tools/ShellContractProbe -- approot|sidecar|cleanup|settings|auto|portable|stage
/// </summary>
internal static class Program
{
    private static int s_failures;

    internal static void Check(string name, bool ok, string detail)
    {
        Console.WriteLine((ok ? "[PASS] " : "[FAIL] ") + name + (ok ? string.Empty : " —— " + detail));
        if (!ok) s_failures++;
    }

    private static int Main(string[] args)
    {
        var mode = args.Length > 0 ? args[0] : "";
        switch (mode)
        {
            case "approot": CheckAppRoot(); break;
            case "sidecar": CheckSidecarAsync(args).GetAwaiter().GetResult(); break;
            case "cleanup": CheckCleanup(args); break;
            case "settings": CheckUpdateSettingsAsync().GetAwaiter().GetResult(); break;
            case "auto": CheckUpdateAutoCheckAsync().GetAwaiter().GetResult(); break;
            case "portable": PortableUpdateProbe.RunAsync(args).GetAwaiter().GetResult(); break;
            case "stage": PortableUpdateProbe.StageRealPackage(args); break;
            default:
                Console.Error.WriteLine(
                    "用法: ShellContractProbe approot|sidecar|cleanup|settings|auto|portable|stage");
                return 2;
        }
        Console.WriteLine(s_failures == 0 ? "ALL PASS" : $"FAILURES: {s_failures}");
        return s_failures == 0 ? 0 : 1;
    }

    // ---------- 便携更新夹具 ----------

    private sealed record ProbeChannel(string Channel, string Tag, string Version);

    private static ProbeChannel Ch(string channel, string version)
        => new(channel, "v" + version, version);

    /// <summary>最小便携来源：latest_release.json（分渠道标记）+ 每渠道一份清单。
    /// 同时铺两份清单路径：assets/&lt;tag&gt;/… 给本地目录源，&lt;tag&gt;/… 给自建 HTTP 源
    /// （UpdateSourceCatalog.Build 把「其他主机」的资产根就定为服务根，没有 assets 段）。</summary>
    private static string MakePortableFeed(string root, string label, params ProbeChannel[] channels)
    {
        var dir = Path.Combine(root, label);
        WritePortableFeed(dir, channels);
        return dir;
    }

    private static void WritePortableFeed(string dir, params ProbeChannel[] channels)
    {
        Directory.CreateDirectory(dir);
        var head = channels[0];
        var marker = new
        {
            tag = head.Tag,
            version = head.Version,
            channels = channels.ToDictionary(c => c.Channel,
                c => new { tag = c.Tag, version = c.Version }),
        };
        File.WriteAllText(Path.Combine(dir, "latest_release.json"),
            JsonSerializer.Serialize(marker, new JsonSerializerOptions { WriteIndented = true }));
        foreach (var channel in channels)
            WritePortableManifest(dir, channel);
    }

    private static void WritePortableManifest(string feedDir, ProbeChannel channel)
    {
        var entry = new UpdateFileEntry("program/MaaRacingMaster.Shell.exe", 1, new string('a', 64));
        var tree = UpdateProtocol.TreeDigest(new[] { entry });
        var manifest = new
        {
            schema = 1,
            tag = channel.Tag,
            version = channel.Version,
            channel = channel.Channel,
            platform = "win-x64",
            compat = new
            {
                layout = UpdateProtocol.SupportedLayout,
                launcher_api = UpdateProtocol.SupportedLauncherApi,
            },
            full = new
            {
                file = $"MaaRacingMaster-{channel.Version}-win-x64.zip",
                size = 1,
                sha256 = new string('b', 64),
                tree_sha256 = tree,
                files = new object[] { new object[] { entry.Path, entry.Size, entry.Sha256 } },
            },
            deltas = Array.Empty<object>(),
            limits = new { max_delta_ratio = 0.30 },
        };
        var text = JsonSerializer.Serialize(manifest,
            new JsonSerializerOptions { WriteIndented = true });
        foreach (var assets in new[] { Path.Combine(feedDir, "assets"), feedDir })
        {
            var tagDir = Path.Combine(assets, channel.Tag);
            Directory.CreateDirectory(tagDir);
            File.WriteAllText(
                Path.Combine(tagDir, $"MaaRacingMaster-{channel.Version}-update.json"), text);
        }
    }

    // ---------- 更新设置合同 ----------

    /// <summary>渠道/下载源设置的真实行为：自检只针对便携协议（版本标记 + 清单）。
    /// 覆盖：默认稳定轨、切渠道换索引、等待稳定轨不降级、切源保留目标、首选不可达
    /// 回退、世代守卫、本次更新不再提示，以及非便携运行 ResetForNewEpoch 后仍不可用。
    /// 夹具自包含，不依赖任何实验输出目录。</summary>
    private static async Task CheckUpdateSettingsAsync()
    {
        var root = MakeTempDir("settings");
        var installRoot = Path.Combine(root, "install");
        Directory.CreateDirectory(installRoot);

        UpdateCoordinator Coord(string? feed = null, string? channel = null,
                                string? source = null, string? dismissed = null,
                                Action<string?>? saveDismissed = null)
            => new(feed, initialChannel: channel, initialSource: source,
                   initialDismissedVersion: dismissed, installRoot: installRoot,
                   currentVersion: "1.0.0", saveDismissedVersion: saveDismissed);

        // ① 渠道是用户设置：默认显式 stable，不依赖安装包烧录的渠道。
        var defaultFeed = MakePortableFeed(root, "default",
            Ch("stable", "9.0.0"), Ch("preview", "2.0.0"));
        var defSnap = await Coord(defaultFeed).CheckAsync();
        Check("默认构造 → 显式走稳定轨索引",
              defSnap.Phase == UpdateSnapshot.Available && defSnap.TargetVersion == "9.0.0"
              && defSnap.Channel == UpdateSnapshot.ChannelStable,
              $"phase={defSnap.Phase} channel={defSnap.Channel} target={defSnap.TargetVersion} error={defSnap.Error}");

        // ② 切渠道：preview → stable，真的换索引。
        var channelFeed = MakePortableFeed(root, "channel",
            Ch("stable", "9.0.0"), Ch("preview", "2.0.0"));
        var ch = Coord(channelFeed, channel: UpdateSnapshot.ChannelPreview);
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
        var downgradeFeed = MakePortableFeed(root, "downgrade",
            Ch("stable", "0.5.0"), Ch("preview", "2.0.0"));
        var dg = Coord(downgradeFeed, channel: UpdateSnapshot.ChannelPreview);
        await dg.CheckAsync();
        var dgSwitch = await dg.SetChannelAsync(UpdateSnapshot.ChannelStable);
        Check("切回稳定轨且稳定版未追上 → 等待标记为真且无降级目标",
              dgSwitch.Phase == UpdateSnapshot.UpToDate && dgSwitch.AwaitingStable
              && dgSwitch.TargetVersion is null,
              $"phase={dgSwitch.Phase} awaiting={dgSwitch.AwaitingStable} target={dgSwitch.TargetVersion} error={dgSwitch.Error}");

        // ④ 下载源是同一货架的地址选择：切换只换首选——就绪目标保留，下次检查换新首选。
        //    便携协议按「全源最高版本」选目标，故 github 地址先不就绪，切源后再补上清单。
        var cnbFeed = MakePortableFeed(root, "cnb", Ch("stable", "3.0.0"));
        var ghPending = Path.Combine(root, "github");
        var sc = Coord($"cnb|{cnbFeed}\ngithub|{ghPending}");
        var scCnb = await sc.CheckAsync();
        Check("首选 cnb → available 且带目标",
              scCnb.Phase == UpdateSnapshot.Available && scCnb.TargetVersion == "3.0.0",
              $"phase={scCnb.Phase} target={scCnb.TargetVersion} error={scCnb.Error}");
        var scGh = await sc.SetSourceAsync(UpdateSnapshot.SourceGithub);
        Check("切下载源 → 就绪目标保留（不清空），仅换首选",
              scGh.Phase == UpdateSnapshot.Available && scGh.TargetVersion == "3.0.0"
              && scGh.Source == UpdateSnapshot.SourceGithub,
              $"phase={scGh.Phase} target={scGh.TargetVersion} source={scGh.Source} error={scGh.Error}");
        WritePortableFeed(ghPending, Ch("stable", "4.0.0"));
        var scRecheck = await sc.CheckAsync();
        Check("切源后重查 → 按新首选读到 github 索引",
              scRecheck.Phase == UpdateSnapshot.Available && scRecheck.TargetVersion == "4.0.0",
              $"phase={scRecheck.Phase} target={scRecheck.TargetVersion} error={scRecheck.Error}");

        // ⑤ 首选不可达 → 回退其余等价地址（同一货架，不存在「跨组回退」）。
        var deadDir = Path.Combine(root, "dead");
        Directory.CreateDirectory(deadDir); // 空目录 = 镜像未就绪
        var fallback = Coord($"cnb|{deadDir}\ngithub|{ghPending}");
        var fbSnap = await fallback.CheckAsync();
        Check("首选不可达 → 回退其余地址完成检查",
              fbSnap.Phase == UpdateSnapshot.Available && fbSnap.TargetVersion == "4.0.0",
              $"phase={fbSnap.Phase} target={fbSnap.TargetVersion} error={fbSnap.Error}");

        // ⑥ 世代守卫：慢源在途检查时切渠道（开启新世代），旧世代的迟到结果
        //    不得覆盖新渠道的结果。慢源按渠道给出不同版本（同 tag/version 不能同时
        //    属于两个渠道），切渠道后再换首选地址重查：快检查先落定，慢检查后返回被丢弃。
        var slowFeed = MakePortableFeed(root, "slow-epoch",
            Ch("stable", "2.0.0"), Ch("preview", "1.5.0"));
        using var slow = new SlowHttpServer(slowFeed,
            TimeSpan.FromMilliseconds(1000), TimeSpan.FromSeconds(6));
        var epochFeed = $"cnb|http://127.0.0.1:{slow.Port}\ngithub|{cnbFeed}";
        var ep = Coord(epochFeed, channel: UpdateSnapshot.ChannelPreview);
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
              recheck.Phase == UpdateSnapshot.Available && recheck.TargetVersion == "3.0.0",
              $"phase={recheck.Phase} target={recheck.TargetVersion} error={recheck.Error}");
        var late = await inFlight; // 同步点：确保旧事务彻底结束
        var final = ep.GetStatus();
        Check("旧世代的迟到检查不覆盖新渠道结果",
              final.TargetVersion == "3.0.0" && final.Channel == UpdateSnapshot.ChannelStable
              && !final.AwaitingStable,
              $"late={late.TargetVersion} final={final.TargetVersion}/{final.Channel}/{final.AwaitingStable}");

        // ⑦ 本次更新不再提示：按目标版本记住并落盘；取消后清除。
        var dismissFeed = MakePortableFeed(root, "dismiss", Ch("stable", "6.0.0"));
        string? savedDismissed = null;
        var dm = Coord(dismissFeed, saveDismissed: v => savedDismissed = v);
        var dmUp = await dm.CheckAsync();
        var dmSet = dm.SetDismissed(true);
        Check("标记本次更新不再提示 → 记住目标版本并落盘",
              dmUp.Phase == UpdateSnapshot.Available
              && dmSet.DismissedVersion == dmUp.TargetVersion
              && savedDismissed == dmUp.TargetVersion,
              $"phase={dmUp.Phase} target={dmUp.TargetVersion} dismissed={dmSet.DismissedVersion} saved={savedDismissed}");
        var dmClear = dm.SetDismissed(false);
        Check("取消不再提示 → 清除标记并落盘",
              dmClear.DismissedVersion is null && savedDismissed is null,
              $"dismissed={dmClear.DismissedVersion} saved={savedDismissed}");
        var dmRestart = Coord(dismissFeed, dismissed: "6.0.0");
        var dmRestartUp = await dmRestart.CheckAsync();
        Check("重启后同一目标仍保持不再提示",
              dmRestartUp.DismissedVersion == "6.0.0",
              $"dismissed={dmRestartUp.DismissedVersion} target={dmRestartUp.TargetVersion}");

        // ⑧ 回归：非便携运行（不传 installRoot）没有自动升级能力，ResetForNewEpoch
        //    之后必须保持 Unavailable 且带原因，不能被切渠道/切源刷成「源未配置」。
        var nonPortable = new UpdateCoordinator(dismissFeed);
        var np0 = nonPortable.GetStatus();
        Check("非便携运行 → 初始 unavailable 且 Managed=false",
              np0.Phase == UpdateSnapshot.Unavailable && !np0.Managed
              && !string.IsNullOrEmpty(np0.Error),
              $"phase={np0.Phase} managed={np0.Managed} error={np0.Error}");
        var npChannel = await nonPortable.SetChannelAsync(UpdateSnapshot.ChannelPreview);
        var npSource = await nonPortable.SetSourceAsync(UpdateSnapshot.SourceGithub);
        Check("非便携运行 → 切渠道/切源后仍 unavailable 且 error 非空",
              npChannel.Phase == UpdateSnapshot.Unavailable && !npChannel.Managed
              && !string.IsNullOrEmpty(npChannel.Error)
              && npSource.Phase == UpdateSnapshot.Unavailable && !npSource.Managed
              && !string.IsNullOrEmpty(npSource.Error),
              $"channel={npChannel.Phase}/{npChannel.Error} source={npSource.Phase}/{npSource.Error}");
    }

    // ---------- 自动检查合同 ----------

    /// <summary>自动检查合同（调度决策 + 并发/取消 + 预算 + 修订准入），全部用环回 HTTP
    /// 直接计数或用可注入时钟断言，不靠耗时猜测：
    /// ① 调度锚点：启动即到期、完成后等一个间隔、手动顺延、忙态跳过、开关、重启重算；
    /// ② 同世代并发检查只发一次索引；切渠道取消旧请求并发出新请求；
    /// ③ 单镜像短预算：黑洞镜像被放弃、回退到可用源，整轮不拖到默认 30 分钟；
    /// ④ 自动检查全源失败保留已有目标；手动检查失败如实切空闲并报错；
    /// ⑤ 修订号准入：过期界面的下载/忽略被拒，当前修订生效；
    /// ⑥ 自动检查提交成功触发推送回调，手动检查不触发；
    /// ⑦ 调度器启动立即执行首轮。
    /// 所有来源均为便携协议：latest_release.json + &lt;tag&gt;/清单，资产判据 .zip。</summary>
    private static async Task CheckUpdateAutoCheckAsync()
    {
        var root = MakeTempDir("auto");
        var installRoot = Path.Combine(root, "install");
        Directory.CreateDirectory(Path.Combine(installRoot, "program"));
        var interval = TimeSpan.FromHours(1);

        UpdateCoordinator Coord(string feed, TimeProvider? clock = null, TimeSpan? timeout = null)
            => new(feed, installRoot: installRoot, currentVersion: "1.0.0",
                   timeProvider: clock, indexTimeout: timeout);

        // ① 调度锚点（可控时钟）
        var clock = new ManualTimeProvider(new DateTimeOffset(2026, 1, 1, 0, 0, 0, TimeSpan.Zero));
        var schedFeed = MakePortableFeed(root, "sched", Ch("stable", "6.0.0"));
        var sched = Coord(schedFeed, clock);
        Check("调度：启动即到期（首次检查不额外延迟）",
              sched.NextAutoCheckDelay(clock.GetUtcNow(), interval) == TimeSpan.Zero,
              $"delay={sched.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
        var firstAuto = await sched.CheckAsync(UpdateCheckTrigger.Auto).ConfigureAwait(false);
        Check("调度：首轮自动检查建立目标",
              firstAuto.Phase == UpdateSnapshot.Available && firstAuto.TargetVersion == "6.0.0",
              $"phase={firstAuto.Phase} target={firstAuto.TargetVersion} error={firstAuto.Error}");
        Check("调度：本轮结束起等一个完整间隔",
              sched.NextAutoCheckDelay(clock.GetUtcNow(), interval) == interval,
              $"delay={sched.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
        clock.Advance(TimeSpan.FromMinutes(30));
        Check("调度：未到期给出剩余等待",
              sched.NextAutoCheckDelay(clock.GetUtcNow(), interval) == TimeSpan.FromMinutes(30),
              $"delay={sched.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
        await sched.CheckAsync().ConfigureAwait(false);
        Check("调度：手动检查顺延一个完整间隔",
              sched.NextAutoCheckDelay(clock.GetUtcNow(), interval) == interval,
              $"delay={sched.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
        clock.Advance(interval);
        Check("调度：到期后返回零等待",
              sched.NextAutoCheckDelay(clock.GetUtcNow(), interval) == TimeSpan.Zero,
              $"delay={sched.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
        sched.SetAutoCheck(false);
        Check("调度：关闭自动检查 → 停用",
              sched.NextAutoCheckDelay(clock.GetUtcNow(), interval) is null,
              $"delay={sched.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
        sched.SetAutoCheck(true);
        Check("调度：重新启用且锚点已过期 → 立即到期",
              sched.NextAutoCheckDelay(clock.GetUtcNow(), interval) == TimeSpan.Zero,
              $"delay={sched.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
        var resumeClock = new ManualTimeProvider(new DateTimeOffset(2026, 2, 1, 0, 0, 0, TimeSpan.Zero));
        var resume = Coord(schedFeed, resumeClock);
        await resume.CheckAsync(UpdateCheckTrigger.Auto).ConfigureAwait(false);
        resume.SetAutoCheck(false);
        resumeClock.Advance(TimeSpan.FromMinutes(10));
        resume.SetAutoCheck(true);
        Check("调度：重新启用且锚点未过期 → 剩余等待",
              resume.NextAutoCheckDelay(resumeClock.GetUtcNow(), interval) == TimeSpan.FromMinutes(50),
              $"delay={resume.NextAutoCheckDelay(resumeClock.GetUtcNow(), interval)}");
        var restarted = Coord(schedFeed, clock);
        Check("调度：重启进程（本进程无已完成检查）→ 立即到期",
              restarted.NextAutoCheckDelay(clock.GetUtcNow(), interval) == TimeSpan.Zero,
              $"delay={restarted.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");

        // 忙态跳过：下载在途时自动检查不入场，但同样推进锚点（下个周期再试）
        var busyFeed = MakePortableFeed(root, "busy", Ch("stable", "7.0.0"));
        using (var slowPackage = new SlowHttpServer(busyFeed,
                   TimeSpan.Zero, TimeSpan.FromSeconds(30)))
        {
            var busy = Coord($"cnb|http://127.0.0.1:{slowPackage.Port}", clock);
            await busy.CheckAsync(UpdateCheckTrigger.Auto).ConfigureAwait(false);
            var dl = await busy.StartDownloadAsync().ConfigureAwait(false);
            var skipped = await busy.CheckAsync(UpdateCheckTrigger.Auto).ConfigureAwait(false);
            Check("忙态：自动检查跳过（不打断下载）",
                  dl.Phase == UpdateSnapshot.Downloading && skipped.Phase == UpdateSnapshot.Downloading
                  && (skipped.Error ?? "").Contains("下载进行中", StringComparison.Ordinal),
                  $"dl={dl.Phase} skipped={skipped.Phase} error={skipped.Error}");
            Check("忙态：跳过也推进锚点（下个周期再试）",
                  busy.NextAutoCheckDelay(clock.GetUtcNow(), interval) == interval,
                  $"delay={busy.NextAutoCheckDelay(clock.GetUtcNow(), interval)}");
            busy.CancelDownload();
        }

        // ② 同世代并发检查合并 + 切渠道取消旧请求
        var ccFeed = MakePortableFeed(root, "concurrent",
            Ch("stable", "3.0.0"), Ch("preview", "4.0.0"));
        using (var slowIndex = new SlowHttpServer(ccFeed,
                   TimeSpan.FromSeconds(3), TimeSpan.FromSeconds(3)))
        {
            var cc = Coord($"cnb|http://127.0.0.1:{slowIndex.Port}");
            var first = cc.CheckAsync();
            await Task.Delay(150).ConfigureAwait(false);
            var second = cc.CheckAsync(); // 同世代在途 → 合并
            var r1 = await first.ConfigureAwait(false);
            var r2 = await second.ConfigureAwait(false);
            Check("并发：同世代两次检查只发一次索引请求",
                  slowIndex.IndexRequests == 1,
                  $"requests={slowIndex.IndexRequests}");
            Check("并发：两次调用得到同一结果",
                  r1.TargetVersion == "3.0.0" && r2.TargetVersion == "3.0.0",
                  $"r1={r1.TargetVersion} r2={r2.TargetVersion}");

            var before = slowIndex.IndexRequests;
            var inflight = cc.CheckAsync(); // 第二次真实请求（慢）
            await Task.Delay(150).ConfigureAwait(false);
            slowIndex.SetIndexDelay(TimeSpan.Zero); // 新渠道检查立即返回
            var sw = Stopwatch.StartNew();
            var switched = await cc.SetChannelAsync(UpdateSnapshot.ChannelPreview).ConfigureAwait(false);
            sw.Stop();
            await inflight.ConfigureAwait(false); // 旧事务以取消收尾
            Check("切渠道：旧索引被取消，不等满慢响应",
                  sw.ElapsedMilliseconds < 1500, $"{sw.ElapsedMilliseconds}ms");
            Check("切渠道：新渠道检查另发一次请求",
                  slowIndex.IndexRequests == before + 2,
                  $"before={before} after={slowIndex.IndexRequests}");
            Check("切渠道：新渠道结果生效",
                  switched.Phase == UpdateSnapshot.Available
                  && switched.Channel == UpdateSnapshot.ChannelPreview
                  && switched.TargetVersion == "4.0.0",
                  $"phase={switched.Phase} channel={switched.Channel} target={switched.TargetVersion} error={switched.Error}");
        }

        // 关窗取消：在途索引请求随令牌立即结束，不必等满默认超时
        var hangFeed = MakePortableFeed(root, "hang", Ch("stable", "1.1.0"));
        using (var hang = new SlowHttpServer(hangFeed,
                   TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30)))
        {
            var closing = Coord($"cnb|http://127.0.0.1:{hang.Port}",
                                timeout: TimeSpan.FromSeconds(20));
            var inflight = closing.CheckAsync();
            await Task.Delay(150).ConfigureAwait(false);
            var sw = Stopwatch.StartNew();
            closing.CancelPendingCheck();
            var afterCancel = await inflight.ConfigureAwait(false);
            sw.Stop();
            Check("关窗取消：在途索引在时限内结束",
                  sw.ElapsedMilliseconds < 2000, $"{sw.ElapsedMilliseconds}ms");
            Check("关窗取消：不改变相位（由关窗路径收尾）",
                  afterCancel.Phase == UpdateSnapshot.Idle, $"phase={afterCancel.Phase}");
        }

        // ③ 单镜像短预算：黑洞镜像放弃、回退可用源；④ 自动失败保留 / 手动失败如实报错
        var blackholeFeed = MakePortableFeed(root, "blackhole", Ch("stable", "7.0.0"));
        var goodFeed = MakePortableFeed(root, "good", Ch("stable", "8.0.0"));
        using (var blackhole = new SlowHttpServer(blackholeFeed,
                   TimeSpan.FromSeconds(30), TimeSpan.FromSeconds(30)))
        using (var good = new SlowHttpServer(goodFeed, TimeSpan.Zero, TimeSpan.Zero))
        {
            var hedge = Coord(
                $"cnb|http://127.0.0.1:{blackhole.Port}\ngithub|http://127.0.0.1:{good.Port}",
                timeout: TimeSpan.FromMilliseconds(600));
            var sw = Stopwatch.StartNew();
            var hedged = await hedge.CheckAsync().ConfigureAwait(false);
            sw.Stop();
            Check("短预算：黑洞镜像被放弃并回退到可用源",
                  hedged.Phase == UpdateSnapshot.Available && hedged.TargetVersion == "8.0.0",
                  $"phase={hedged.Phase} target={hedged.TargetVersion} error={hedged.Error}");
            Check("短预算：整轮在预算量级内结束（不拖到默认 30 分钟）",
                  sw.ElapsedMilliseconds < 5000, $"{sw.ElapsedMilliseconds}ms");
            Check("短预算：黑洞源请求数受预算约束",
                  blackhole.IndexRequests <= 2, $"requests={blackhole.IndexRequests}");

            good.Down = true;
            blackhole.Down = true;
            var autoFailed = await hedge.CheckAsync(UpdateCheckTrigger.Auto).ConfigureAwait(false);
            Check("自动检查全源失败：保留已有目标、相位与按钮",
                  autoFailed.Phase == UpdateSnapshot.Available
                  && autoFailed.TargetVersion == "8.0.0"
                  && string.IsNullOrEmpty(autoFailed.Error),
                  $"phase={autoFailed.Phase} target={autoFailed.TargetVersion} error={autoFailed.Error}");
            var manualFailed = await hedge.CheckAsync().ConfigureAwait(false);
            Check("手动检查全源失败：切到空闲并如实报错",
                  manualFailed.Phase == UpdateSnapshot.Idle
                  && (manualFailed.Error ?? "").Contains("所有更新源均不可达", StringComparison.Ordinal),
                  $"phase={manualFailed.Phase} error={manualFailed.Error}");
            good.Down = false;
            blackhole.Down = false;
        }

        // ⑤ 修订号准入：过期界面的下载/忽略被拒，当前修订生效
        var revFeed = MakePortableFeed(root, "rev", Ch("stable", "4.0.0"));
        var rev = Coord(revFeed);
        var revSnap = await rev.CheckAsync().ConfigureAwait(false);
        var stale = revSnap.Revision + 100; // 一定不是当前修订
        var staleDownload = await rev.StartDownloadAsync(stale).ConfigureAwait(false);
        Check("过期修订：下载被拒且不改变相位",
              staleDownload.Phase == UpdateSnapshot.Available
              && (staleDownload.Error ?? "").Contains("确认", StringComparison.Ordinal),
              $"phase={staleDownload.Phase} error={staleDownload.Error}");
        var staleDismiss = rev.SetDismissed(true, stale);
        Check("过期修订：忽略被拒且不写入标记",
              staleDismiss.DismissedVersion is null
              && (staleDismiss.Error ?? "").Contains("确认", StringComparison.Ordinal),
              $"dismissed={staleDismiss.DismissedVersion} error={staleDismiss.Error}");
        var currentRevision = rev.GetStatus().Revision;
        var okDismiss = rev.SetDismissed(true, currentRevision);
        Check("当前修订：忽略生效",
              okDismiss.DismissedVersion == "4.0.0",
              $"dismissed={okDismiss.DismissedVersion} error={okDismiss.Error}");

        // ⑥ 推送回调：自动检查成功触发，手动检查不触发
        var pushFeed = MakePortableFeed(root, "push", Ch("stable", "3.0.0"));
        var push = Coord(pushFeed);
        UpdateSnapshot? pushed = null;
        push.AutoCheckCommitted += s => pushed = s;
        await push.CheckAsync(UpdateCheckTrigger.Auto).ConfigureAwait(false);
        // 回调在检查任务的完成信号之后触发：给续体一个到达窗口，再判定是否推送。
        var pushDeadline = DateTime.UtcNow.AddSeconds(2);
        while (pushed is null && DateTime.UtcNow < pushDeadline)
            await Task.Delay(50).ConfigureAwait(false);
        Check("推送：自动检查提交成功 → 回调收到快照",
              pushed is not null && pushed.TargetVersion == "3.0.0",
              $"pushed={pushed?.TargetVersion}");
        pushed = null;
        await push.CheckAsync().ConfigureAwait(false);
        Check("推送：手动检查不触发后台推送", pushed is null, $"pushed={pushed?.TargetVersion}");

        // ⑦ 调度器启动立即执行首轮
        var loopFeed = MakePortableFeed(root, "loop", Ch("stable", "2.0.0"));
        var loopCoord = Coord(loopFeed);
        using (var scheduler = new UpdateAutoCheck(loopCoord, interval: interval))
        {
            scheduler.Start();
            var loopSnap = await WaitUntilAsync(
                s => s.Phase == UpdateSnapshot.Available, loopCoord.GetStatus, 5000).ConfigureAwait(false);
            Check("调度器启动 → 立即执行首轮检查",
                  loopSnap.Phase == UpdateSnapshot.Available && loopSnap.TargetVersion == "2.0.0",
                  $"phase={loopSnap.Phase} target={loopSnap.TargetVersion}");
        }
    }

    /// <summary>探测用的可控时钟：只提供 GetUtcNow，调度决策（NextAutoCheckDelay）
    /// 是纯函数式读取，不需要驱动 timer。</summary>
    private sealed class ManualTimeProvider : TimeProvider
    {
        private DateTimeOffset _now;

        public ManualTimeProvider(DateTimeOffset start) => _now = start;

        public override DateTimeOffset GetUtcNow() => _now;

        public void Advance(TimeSpan delta) => _now += delta;
    }

    /// <summary>轮询等待条件成立（检查/下载都是后台任务）：返回最后一次快照。</summary>
    private static async Task<UpdateSnapshot> WaitUntilAsync(Func<UpdateSnapshot, bool> done,
                                                            Func<UpdateSnapshot> read, int timeoutMs)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        var last = read();
        while (!done(last) && DateTime.UtcNow < deadline)
        {
            await Task.Delay(50).ConfigureAwait(false);
            last = read();
        }
        return last;
    }

    /// <summary>本地最小 HTTP 便携源（TcpListener，无 URL ACL 依赖）：
    /// latest_release.json 计索引请求，标记与清单共用 _indexDelay/Down/取消令牌，
    /// .zip 请求挂起模拟大文件下载。Dispose 即停服。</summary>
    private sealed class SlowHttpServer : IDisposable
    {
        private readonly TcpListener _listener;
        private readonly CancellationTokenSource _cts = new();
        private readonly Dictionary<string, byte[]> _files;
        private TimeSpan _indexDelay;
        private readonly TimeSpan _packageDelay;
        private int _indexRequests;

        public SlowHttpServer(string root, TimeSpan indexDelay, TimeSpan packageDelay)
        {
            _indexDelay = indexDelay;
            _packageDelay = packageDelay;
            _files = new Dictionary<string, byte[]>(StringComparer.OrdinalIgnoreCase);
            foreach (var file in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories))
            {
                var rel = Path.GetRelativePath(root, file).Replace('\\', '/');
                _files[rel] = File.ReadAllBytes(file);
            }
            _listener = new TcpListener(System.Net.IPAddress.Loopback, 0);
            _listener.Start();
            Port = ((System.Net.IPEndPoint)_listener.LocalEndpoint).Port;
            _ = AcceptLoopAsync();
        }

        public int Port { get; }

        /// <summary>到达本服务的标记请求数。取消/合并/镜像短路都可据此直接计数。</summary>
        public int IndexRequests => Volatile.Read(ref _indexRequests);

        /// <summary>true = 所有请求返回 500（模拟全源故障，不依赖延迟）。</summary>
        public bool Down { get; set; }

        /// <summary>调整后续请求的索引延迟（已开始的请求仍按旧值等待）。</summary>
        public void SetIndexDelay(TimeSpan delay) => _indexDelay = delay;

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
                var relative = path.TrimStart('/');
                if (relative.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
                {
                    // 挂起不响应：模拟大文件下载；取消后由 Dispose/断连收尾
                    await Task.Delay(_packageDelay, _cts.Token);
                    return;
                }
                var isMarker = relative.EndsWith("latest_release.json",
                    StringComparison.OrdinalIgnoreCase);
                if (isMarker) Interlocked.Increment(ref _indexRequests);
                if (Down)
                {
                    await stream.WriteAsync(
                        System.Text.Encoding.ASCII.GetBytes("HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"),
                        _cts.Token);
                    return;
                }
                await Task.Delay(_indexDelay, _cts.Token);
                if (!_files.TryGetValue(relative, out var body))
                {
                    await stream.WriteAsync(
                        System.Text.Encoding.ASCII.GetBytes("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"),
                        _cts.Token);
                    return;
                }
                await stream.WriteAsync(
                    System.Text.Encoding.ASCII.GetBytes(
                        $"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"),
                    _cts.Token);
                await stream.WriteAsync(body, _cts.Token);
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

    private static string MakeTempDir(string label)
    {
        var dir = Path.Combine(Path.GetTempPath(),
            "maarm-shell-probe-" + label + "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(dir);
        return dir;
    }

    private static string Norm(string p) => Path.GetFullPath(p);

    // ---------- 应用根解析 ----------

    private static void CheckAppRoot()
    {
        // 历史口径不变（参数 > 环境变量 > 向上探测）
        var proj = MakeTempDir("proj");
        var deep = Path.Combine(proj, "sub", "deep");
        Directory.CreateDirectory(deep);
        File.WriteAllText(Path.Combine(proj, "pyproject.toml"), string.Empty);

        var argRoot = Norm(Path.Combine(proj, "via-arg"));
        var r6 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe", "--app-root", argRoot }, @"ignored-env", deep);
        Check("参数 → 参数优先", r6.Root == argRoot,
              $"Root={r6.Root} Decision={r6.Decision}");

        var envRoot = Norm(Path.Combine(proj, "via-env"));
        var r7 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe" }, envRoot, deep);
        Check("环境变量 → 优先于探测", r7.Root == envRoot,
              $"Root={r7.Root} Decision={r7.Decision}");

        var r8 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, deep);
        Check("无覆盖 → 向上探测 pyproject.toml", r8.Root == proj,
              $"Root={r8.Root} Decision={r8.Decision}");

        var emptyBase = Path.Combine(MakeTempDir("bare"), "a", "b");
        Directory.CreateDirectory(emptyBase);
        var r9 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, emptyBase);
        Check("无任何锚 → null（壳自行降级）", r9.Root is null,
              $"Root={r9.Root} Decision={r9.Decision}");

        // 用户数据根：与 paths.py 同口径（夹具用相对路径，避免源码落本机绝对路径）
        Check("用户数据根:APPDATA 优先",
              UserDataRoot.Resolve(@"AppRoot", @"AppData\Roaming") == @"AppData\Roaming\MaaRacingMaster",
              UserDataRoot.Resolve(@"AppRoot", @"AppData\Roaming") ?? "<空>");
        Check("用户数据根:无 APPDATA 回退应用根旁 data",
              UserDataRoot.Resolve(@"AppRoot", "") == @"AppRoot\data",
              UserDataRoot.Resolve(@"AppRoot", "") ?? "<空>");
    }
}
