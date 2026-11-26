using System;
using System.IO;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 与原生启动器的安装事务握手。只在 program\ 布局的正式安装里生效
/// （启动器设置 MaaRM_INSTALL_ROOT）；源码/旧解压布局没有该变量，所有动作直通。
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
            var value = Environment.GetEnvironmentVariable("MaaRM_INSTALL_ROOT");
            return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
        }
    }

    /// <summary>旧启动器 PID（由启动器通过环境变量传入）；未知时为 0。</summary>
    public static int LauncherPid
    {
        get
        {
            var value = Environment.GetEnvironmentVariable("MaaRM_LAUNCHER_PID");
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

    /// <summary>
    /// 升级后的首次启动：事务处于 swapped 且目标版本正是本次运行的版本时，写健康回执。
    /// 启动器据此确认升级成功并清理备份；超时未收到回执会自动回滚到旧版本。
    /// 任何异常都只记日志——回执失败最多导致回滚，不能影响正常启动。
    /// </summary>
    public static void AckHealthIfPending(string runningVersion)
    {
        var root = InstallRoot;
        if (root is null || string.IsNullOrWhiteSpace(runningVersion))
            return;
        var txPath = TransactionPath(root);
        try
        {
            if (!File.Exists(txPath))
                return;
            var state = ReadValue(txPath, "state");
            var target = ReadValue(txPath, "target");
            if (!string.Equals(state, StateSwapped, StringComparison.OrdinalIgnoreCase)
                || !string.Equals(target, runningVersion, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }
            var updateDir = UpdateDir(root);
            Directory.CreateDirectory(updateDir);
            var temp = Path.Combine(updateDir, "health.tmp");
            File.WriteAllText(temp, $"version={runningVersion}\n");
            File.Move(temp, HealthPath(root), overwrite: true);
            ShellLog.Append($"[update] 已向启动器写健康回执: {runningVersion}");
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[update] 健康回执写入失败（启动器将按回滚处理）: {ex.Message}");
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
