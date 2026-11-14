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
        if (Environment.GetEnvironmentVariable("MAARM_PROBE_REPORT") is { Length: > 0 } path)
            File.WriteAllText(path, report);
        if (args is ["--download-local", var downloadFeed])
            UpdateLocal(downloadFeed, apply: false).GetAwaiter().GetResult();
        if (args is ["--apply-local", var applyFeed])
            UpdateLocal(applyFeed, apply: true).GetAwaiter().GetResult();
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
