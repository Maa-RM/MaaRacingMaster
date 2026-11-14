using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace MaaRacingMaster.Shell;

/// <summary>应用根解析结果。Root=最终采用的应用根；Managed=受管安装（内容树可被更新器整体替换）；
/// IgnoredOverride=与内容根冲突、被拒绝采用的旧覆盖值（已归一化）；RefusedReason=启动前拒绝理由（非空则壳应拒绝启动）。</summary>
public sealed record AppRootResolution(
    string? Root,
    bool Managed,
    string? IgnoredOverride,
    string? RefusedReason,
    string Decision);

/// <summary>
/// 应用根（含 pyproject.toml 的目录）解析单一真源：Program 启动前检查与 MainWindow 共用，
/// 进程内首次解析后缓存（不变量：启动期只解析一次，此后不因 cwd/exe 位置再推导）。
/// 非受管（源码 / 旧手动 ZIP）：--app-root 参数 > MaaRM_APP_ROOT 环境变量 > exe 向上探测 pyproject.toml（历史口径不变）。
/// 受管（更新器标准布局）：内容根（exe 所在目录）为唯一权威；旧覆盖值与内容根冲突时忽略并记录，
/// 不得让新版 GUI 启动旧版 Python/旧版前端。
/// </summary>
public static class AppRootResolver
{
    private static AppRootResolution? _cached;

    /// <summary>进程内缓存版解析：输入为进程级常量（参数/环境变量/exe 位置），首次结果即最终结果。</summary>
    public static AppRootResolution Resolve(
        IReadOnlyList<string> commandLineArgs,
        string? envAppRoot,
        string baseDirectory,
        string? appDataPath)
        => _cached ??= ResolveCore(commandLineArgs, envAppRoot, baseDirectory, appDataPath);

    /// <summary>绕过缓存的解析：合同探针按场景注入输入时使用。</summary>
    public static AppRootResolution ResolveFresh(
        IReadOnlyList<string> commandLineArgs,
        string? envAppRoot,
        string baseDirectory,
        string? appDataPath)
        => ResolveCore(commandLineArgs, envAppRoot, baseDirectory, appDataPath);

    private static AppRootResolution ResolveCore(
        IReadOnlyList<string> commandLineArgs,
        string? envAppRoot,
        string baseDirectory,
        string? appDataPath)
    {
        // 受管判定用结构特征（exe 的安装根旁有更新器执行器），不访问更新器定位器 API——
        // 本机实测受管树内的定位器解析对非主程序有挂起异常（2026-11 P0 取证）。
        var baseDir = Path.TrimEndingDirectorySeparator(Path.GetFullPath(baseDirectory));
        var installDir = Path.GetDirectoryName(baseDir);
        var isManaged = !string.IsNullOrEmpty(installDir) &&
                        File.Exists(Path.Combine(installDir, "Update.exe"));

        if (isManaged)
            return ManagedRoot(baseDir, commandLineArgs, envAppRoot, appDataPath);

        // --app-root 参数（未来 CLI 用，当前 Launcher 未走此路径）
        var argRoot = commandLineArgs
            .SkipWhile(a => a != "--app-root").Skip(1).FirstOrDefault();
        if (argRoot is not null && argRoot.Length > 0)
        {
            try { return NonManaged(Path.GetFullPath(argRoot), "--app-root 参数"); }
            catch { /* 非法路径，继续降级 */ }
        }

        // MaaRM_APP_ROOT 环境变量（第一版 PoC transport）
        if (!string.IsNullOrWhiteSpace(envAppRoot))
        {
            try { return NonManaged(Path.GetFullPath(envAppRoot), "MaaRM_APP_ROOT 环境变量"); }
            catch { /* 非法路径，继续降级 */ }
        }

        // 开发模式回退：从 exe 运行目录逐级向上找含 pyproject.toml 的目录（repo marker）
        var dir = new DirectoryInfo(baseDirectory);
        while (dir is not null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "pyproject.toml")))
                return new AppRootResolution(dir.FullName, Managed: false, null, null, "向上探测 pyproject.toml");
            dir = dir.Parent;
        }
        return new AppRootResolution(null, Managed: false, null, null, "未找到应用根");

        AppRootResolution NonManaged(string root, string source)
            => new(root, Managed: false, null, null, source);
    }

    /// <summary>
    /// 受管根解析：内容根（exe 所在目录）为唯一权威。旧覆盖值（--app-root / MaaRM_APP_ROOT）
    /// 指向别处时忽略并记录，不混用——防止新版 GUI 启动旧版 Python/旧版前端（旧根毒化）。
    /// %APPDATA% 缺失时拒绝启动：包根旁数据回退会落进可被整体替换的内容树，升级即丢数据。
    /// </summary>
    private static AppRootResolution ManagedRoot(
        string contentRoot,
        IReadOnlyList<string> commandLineArgs,
        string? envAppRoot,
        string? appDataPath)
    {
        var ignored = new List<string>();
        var argRoot = commandLineArgs.SkipWhile(a => a != "--app-root").Skip(1).FirstOrDefault();
        foreach (var (source, raw) in new[] { ("--app-root 参数", argRoot), ("MaaRM_APP_ROOT 环境变量", envAppRoot) })
        {
            if (string.IsNullOrWhiteSpace(raw))
                continue;
            try
            {
                var normalized = Path.TrimEndingDirectorySeparator(Path.GetFullPath(raw));
                if (!string.Equals(normalized, contentRoot, StringComparison.OrdinalIgnoreCase))
                    ignored.Add($"{source}={normalized}");
            }
            catch { /* 非法路径：与指向别处同性质，忽略并记录 */ }
        }

        string? refused = string.IsNullOrWhiteSpace(appDataPath)
            ? "受管安装要求 %APPDATA%（用户数据目录）可用：包根旁数据回退会落进可被整体替换的内容树，升级即丢数据"
            : null;

        var decision = ignored.Count > 0
            ? $"受管内容根为准；忽略冲突覆盖值: {string.Join("; ", ignored)}"
            : "受管内容根为准（无冲突覆盖值）";
        return new AppRootResolution(contentRoot, Managed: true,
            ignored.Count > 0 ? string.Join("; ", ignored) : null, refused, decision);
    }
}

/// <summary>用户数据根（与 maaracing_master/core/paths.py 同口径）：APPDATA/MaaRacingMaster。
/// APPDATA 缺失时回退应用根旁 data/（仅源码 / 旧 ZIP 模式；受管模式该回退会落在可被整体替换的
/// 内容树内，壳在启动前已按解析结果的拒绝理由拒绝启动，不落到这里）。</summary>
public static class UserDataRoot
{
    public static string? Resolve(string? appRoot, string? appData)
    {
        if (!string.IsNullOrWhiteSpace(appData))
            return Path.Combine(appData, "MaaRacingMaster");
        return string.IsNullOrEmpty(appRoot) ? null : Path.Combine(appRoot, "data");
    }
}
