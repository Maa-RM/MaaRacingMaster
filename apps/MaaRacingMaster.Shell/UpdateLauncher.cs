using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json.Serialization;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 与原生启动器的安装事务握手。只在 program\ 布局的正式安装里生效
/// （启动器设置 MAARM_UPDATE_INSTALL_ROOT）；源码/旧解压布局没有该变量，所有动作直通。
/// 事务文件是启动器与壳之间的唯一约定，格式为每行 key=value（UTF-8）。
/// </summary>
public static class UpdateLauncher
{
    public const string StatePrepared = "prepared";
    public const string StateSwapping = "swapping";
    public const string StateSwapped = "swapped";
    public const string StateAborted = "aborted";

    /// <summary>安装根（program\ 的父目录）；非正式安装返回 null。</summary>
    public static string? InstallRoot
    {
        get
        {
            var value = Environment.GetEnvironmentVariable("MAARM_UPDATE_INSTALL_ROOT");
            return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
        }
    }

    /// <summary>旧启动器 PID（由启动器通过环境变量传入）；未知时为 0。</summary>
    public static int LauncherPid
    {
        get
        {
            var value = Environment.GetEnvironmentVariable("MAARM_UPDATE_LAUNCHER_PID");
            return int.TryParse(value, out var pid) && pid > 0 ? pid : 0;
        }
    }

    internal static string UpdateDir(string installRoot) => Path.Combine(installRoot, ".update");

    internal static string TransactionPath(string installRoot) =>
        Path.Combine(UpdateDir(installRoot), "transaction.txt");

    internal static string HealthPath(string installRoot) =>
        Path.Combine(UpdateDir(installRoot), "health.txt");

    internal static string StagingPath(string installRoot) =>
        Path.Combine(UpdateDir(installRoot), "staging");

    /// <summary>已安装版本的清单（下一次安装前的差异基线）；首版安装或旧布局没有这个文件。</summary>
    public static string InstalledManifestPath(string installRoot) =>
        Path.Combine(UpdateDir(installRoot), "installed.json");

    internal static string PendingManifestPath(string installRoot) =>
        Path.Combine(UpdateDir(installRoot), "installed.pending.json");

    internal static string PendingReportPath(string installRoot) =>
        Path.Combine(UpdateDir(installRoot), "pending-report.txt");

    internal static string PendingNotesPath(string installRoot) =>
        Path.Combine(UpdateDir(installRoot), "pending-notes.md");

    /// <summary>更新后首次启动的汇报：怎么升上来的（增量/整包、体积、来源），或上次更新没成功。</summary>
    public sealed record StartupUpdateReport(
        [property: JsonPropertyName("outcome")] string Outcome,
        [property: JsonPropertyName("version")] string Version,
        [property: JsonPropertyName("mode")] string Mode,
        [property: JsonPropertyName("bytes")] long Bytes,
        [property: JsonPropertyName("source")] string Source,
        [property: JsonPropertyName("notes")] string Notes);

    /// <summary>健康回执结果：是否存在待确认事务、回执是否真的写出。</summary>
    public sealed record HealthAckResult(bool Pending, bool Written);

    private static StartupUpdateReport? s_startupReport;
    private static bool s_startupReportTaken;

    /// <summary>试启动确认后消费交接记录（安装成功则把清单副本转成当前基线）。</summary>
    public static void CaptureStartupReport(string runningVersion)
    {
        if (s_startupReportTaken) return;
        s_startupReport = ConsumeStartupReport(runningVersion);
        s_startupReportTaken = true;
    }

    /// <summary>前端取一次启动汇报（取走后清空，避免重复弹窗）。</summary>
    public static StartupUpdateReport? TakeStartupReport()
    {
        var report = s_startupReport;
        s_startupReport = null;
        return report;
    }

    private static StartupUpdateReport? ConsumeStartupReport(string runningVersion)
    {
        var root = InstallRoot;
        if (root is null) return null;
        var reportPath = PendingReportPath(root);
        if (!File.Exists(reportPath))
        {
            TryDelete(PendingManifestPath(root));
            return null;
        }
        try
        {
            var version = ReadValue(reportPath, "version") ?? "";
            var mode = ReadValue(reportPath, "mode") ?? "";
            var source = ReadValue(reportPath, "source") ?? "";
            _ = long.TryParse(ReadValue(reportPath, "bytes"), out var bytes);
            var notesPath = PendingNotesPath(root);
            var notes = File.Exists(notesPath) ? File.ReadAllText(notesPath) : "";
            var installed = string.Equals(version, runningVersion, StringComparison.OrdinalIgnoreCase);
            if (installed)
            {
                var pendingManifest = PendingManifestPath(root);
                if (File.Exists(pendingManifest))
                {
                    Directory.CreateDirectory(UpdateDir(root));
                    File.Move(pendingManifest, InstalledManifestPath(root), overwrite: true);
                }
            }
            TryDelete(reportPath);
            TryDelete(notesPath);
            TryDelete(PendingManifestPath(root));
            return new StartupUpdateReport(installed ? "installed" : "rolled-back",
                version, mode, bytes, source, notes);
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[update] 读取启动汇报失败: {ex.Message}");
            return null;
        }
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); }
        catch { /* 清理失败不阻断启动 */ }
    }

    /// <summary>
    /// 升级后的首次启动：事务处于 swapped 且目标版本正是本次运行的版本时，写健康回执。
    /// 启动器据此确认升级成功并清理备份；超时未收到回执会自动回滚到旧版本。
    /// 任何异常都只记日志——回执失败最多导致回滚，不能影响正常启动。
    /// </summary>
    public static HealthAckResult AckHealthIfPending(string runningVersion)
    {
        var root = InstallRoot;
        if (root is null || string.IsNullOrWhiteSpace(runningVersion))
            return new HealthAckResult(false, false);
        var txPath = TransactionPath(root);
        var pending = false;
        try
        {
            if (!File.Exists(txPath))
                return new HealthAckResult(false, false);
            var state = ReadValue(txPath, "state");
            var target = ReadValue(txPath, "target");
            if (!string.Equals(state, StateSwapped, StringComparison.OrdinalIgnoreCase)
                || !string.Equals(target, runningVersion, StringComparison.OrdinalIgnoreCase))
            {
                return new HealthAckResult(false, false);
            }
            pending = true;
            var updateDir = UpdateDir(root);
            Directory.CreateDirectory(updateDir);
            var temp = Path.Combine(updateDir, "health.tmp");
            File.WriteAllText(temp, $"version={runningVersion}\n");
            File.Move(temp, HealthPath(root), overwrite: true);
            ShellLog.Append($"[update] 已向启动器写健康回执: {runningVersion}");
            return new HealthAckResult(true, true);
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[update] 健康回执写入失败（启动器将按回滚处理）: {ex.Message}");
            return new HealthAckResult(pending, false);
        }
    }

    /// <summary>
    /// 把已暂存好的候选树交给安装事务：写事务文件（state=prepared）并启动根启动器的
    /// --apply 模式。启动器会等本进程退出后替换 program\ 并试启动新版本。
    /// 返回 false 时调用方必须解冻并恢复后端（还没进入替换相位）。
    /// </summary>
    public static bool TryStartApply(string targetVersion, int shellPid, string mode, long bytes,
                                     string source, string notes, string manifestText,
                                     out string error, string? installRoot = null)
    {
        error = string.Empty;
        var root = installRoot ?? InstallRoot;
        if (root is null)
        {
            error = "不是正式安装，无法自动升级";
            return false;
        }
        var staging = StagingPath(root);
        if (!Directory.Exists(staging))
        {
            error = "暂存目录不存在，请重新下载更新";
            return false;
        }
        var launcher = Path.Combine(root, "MaaRacingMaster.exe");
        if (!File.Exists(launcher))
        {
            error = "安装目录缺少启动器，无法自动升级";
            return false;
        }
        try
        {
            var updateDir = UpdateDir(root);
            Directory.CreateDirectory(updateDir);
            // 本次更新怎么升上来的：新壳启动后据此显示汇报卡片；清单副本在确认成功后
            // 转为下一次安装的差异基线（回滚时不转，避免拿错基线）。
            File.WriteAllText(PendingReportPath(root),
                $"version={targetVersion}\nmode={mode}\nbytes={bytes}\nsource={source}\n",
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            if (!string.IsNullOrWhiteSpace(notes))
                File.WriteAllText(PendingNotesPath(root), notes,
                    new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            else
                TryDelete(PendingNotesPath(root));
            if (!string.IsNullOrWhiteSpace(manifestText))
                File.WriteAllText(PendingManifestPath(root), manifestText,
                    new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            var txPath = TransactionPath(root);
            var temp = txPath + ".tmp";
            var text =
                $"state={StatePrepared}\n" +
                $"install={root}\n" +
                $"staging={staging}\n" +
                $"backup={Path.Combine(updateDir, "backup")}\n" +
                $"target={targetVersion}\n" +
                $"shell_pid={shellPid}\n" +
                $"launcher_pid={LauncherPid}\n" +
                "watcher_pid=0\n";
            File.WriteAllText(temp, text, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            File.Move(temp, txPath, overwrite: true);

            var startInfo = new ProcessStartInfo
            {
                FileName = launcher,
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = root,
            };
            startInfo.ArgumentList.Add("--apply");
            startInfo.ArgumentList.Add(txPath);
            Process.Start(startInfo);
            ShellLog.Append($"[update] 安装事务已交接: {targetVersion}");
            return true;
        }
        catch (Exception ex)
        {
            error = $"启动更新器失败: {ex.Message}";
            return false;
        }
    }

    private static string? ReadValue(string path, string key)
    {
        foreach (var line in File.ReadLines(path))
        {
            var separator = line.IndexOf('=');
            if (separator <= 0)
                continue;
            if (string.Equals(line.Substring(0, separator).Trim(), key,
                    StringComparison.OrdinalIgnoreCase))
            {
                return line.Substring(separator + 1).Trim();
            }
        }
        return null;
    }
}
