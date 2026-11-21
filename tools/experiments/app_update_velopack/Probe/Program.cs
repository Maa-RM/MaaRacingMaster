using System.Text.Json;
using Velopack;
using Velopack.Locators;

public static class Program
{
    public static void Main(string[] args)
    {
        // 必须早于窗口、单实例检查、侧车构造；仅在显式协调后提交更新。
        VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
        var report = JsonSerializer.Serialize(new {
            args,
            version = VelopackLocator.Current.CurrentlyInstalledVersion?.ToString(),
            appContentRoot = VelopackLocator.Current.AppContentDir,
            binaryRoot = AppContext.BaseDirectory,
        });
        Console.WriteLine("APP_START " + report);
        if (Environment.GetEnvironmentVariable("MAARM_UPDATE_PROBE_REPORT") is { Length: > 0 } path)
            File.WriteAllText(path, report);
        if (args is ["--locator-root", var rootOnly])
        {
            ReportRoot(rootOnly, args);
            return;
        }
        if (args is ["--locator-root", var lr, "--download-local", var downloadFeed])
            UpdateLocalWithLocator(lr, downloadFeed, apply: false).GetAwaiter().GetResult();
        if (args is ["--locator-root", var lr2, "--apply-local", var applyFeed])
            UpdateLocalWithLocator(lr2, applyFeed, apply: true).GetAwaiter().GetResult();
        if (args is ["--download-local", var downloadFeed2])
            UpdateLocal(downloadFeed2, apply: false).GetAwaiter().GetResult();
        if (args is ["--apply-local", var applyFeed2])
            UpdateLocal(applyFeed2, apply: true).GetAwaiter().GetResult();
    }

    // 显式定位器模式：助手从受管树外驱动指定根的安装，规避默认定位器在
    // 「受管树内运行次要 exe」场景下的挂起（2026-11 本机实测）。
    private static void ReportRoot(string root, string[] args)
    {
        var marker = Path.Combine(root, "current", "payload-version.txt");
        var version = File.Exists(marker) ? File.ReadAllText(marker).Trim() : null;
        var report = JsonSerializer.Serialize(new {
            args,
            version,
            appContentRoot = Path.Combine(root, "current"),
            binaryRoot = Path.Combine(root, "current"),
            locator = "test-explicit",
        });
        Console.WriteLine("ROOT_REPORT " + report);
        if (Environment.GetEnvironmentVariable("MAARM_UPDATE_PROBE_REPORT") is { Length: > 0 } path)
            File.WriteAllText(path, report);
    }

    private static async Task UpdateLocalWithLocator(string root, string feed, bool apply)
    {
        if (!Directory.Exists(feed)) throw new ArgumentException("实验仅接受存在的本地更新源");
        var marker = Path.Combine(root, "current", "payload-version.txt");
        var version = File.Exists(marker) ? File.ReadAllText(marker).Trim() : "0.0.0";
        var locator = new TestVelopackLocator(
            "MaaRM.RealShellProbe", version,
            Path.Combine(root, "packages"),
            Path.Combine(root, "current"), root,
            Path.Combine(root, "Update.exe"));
        var manager = new UpdateManager(Path.GetFullPath(feed), new UpdateOptions(), locator);
        var update = await manager.CheckForUpdatesAsync()
            ?? throw new InvalidOperationException("隔离更新源没有新版");
        await manager.DownloadUpdatesAsync(update);
        Console.WriteLine("UPDATE_DOWNLOADED");
        if (!apply) return;
        manager.WaitExitThenApplyUpdates(update.TargetFullRelease, silent: true, restart: true);
        Console.WriteLine("UPDATE_QUEUED");
    }

    private static async Task UpdateLocal(string feed, bool apply)
    {
        if (!Directory.Exists(feed)) throw new ArgumentException("实验仅接受存在的本地更新源");
        var manager = new UpdateManager(Path.GetFullPath(feed));
        var update = await manager.CheckForUpdatesAsync()
            ?? throw new InvalidOperationException("隔离更新源没有新版");
        await manager.DownloadUpdatesAsync(update);
        Console.WriteLine("UPDATE_DOWNLOADED");
        if (!apply) return;
        manager.WaitExitThenApplyUpdates(update.TargetFullRelease, silent: true, restart: true,
                                         restartArgs: new[] { "--restarted-probe" });
        Console.WriteLine("UPDATE_QUEUED");
    }
}
