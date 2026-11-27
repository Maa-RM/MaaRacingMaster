using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace MaaRacingMaster.Shell;

/// <summary>应用根解析结果。Root=最终采用的应用根；Decision=采用的判据（落日志）。</summary>
public sealed record AppRootResolution(
    string? Root,
    string Decision);

/// <summary>
/// 应用根（含 pyproject.toml 的目录）解析单一真源：Program 启动前检查与 MainWindow 共用，
/// 进程内首次解析后缓存（不变量：启动期只解析一次，此后不因 cwd/exe 位置再推导）。
/// 口径：--app-root 参数 > MaaRM_APP_ROOT 环境变量 > exe 向上探测 pyproject.toml。
/// </summary>
public static class AppRootResolver
{
    private static AppRootResolution? _cached;

    /// <summary>进程内缓存版解析：输入为进程级常量（参数/环境变量/exe 位置），首次结果即最终结果。</summary>
    public static AppRootResolution Resolve(
        IReadOnlyList<string> commandLineArgs,
        string? envAppRoot,
        string baseDirectory)
        => _cached ??= ResolveCore(commandLineArgs, envAppRoot, baseDirectory);

    /// <summary>绕过缓存的解析：合同探针按场景注入输入时使用。</summary>
    public static AppRootResolution ResolveFresh(
        IReadOnlyList<string> commandLineArgs,
        string? envAppRoot,
        string baseDirectory)
        => ResolveCore(commandLineArgs, envAppRoot, baseDirectory);

    private static AppRootResolution ResolveCore(
        IReadOnlyList<string> commandLineArgs,
        string? envAppRoot,
        string baseDirectory)
    {
        // --app-root 参数（未来 CLI 用，当前 Launcher 未走此路径）
        var argRoot = commandLineArgs
            .SkipWhile(a => a != "--app-root").Skip(1).FirstOrDefault();
        if (argRoot is not null && argRoot.Length > 0)
        {
            try { return new AppRootResolution(Path.GetFullPath(argRoot), "--app-root 参数"); }
            catch { /* 非法路径，继续降级 */ }
        }

        // MaaRM_APP_ROOT 环境变量（第一版 PoC transport）
        if (!string.IsNullOrWhiteSpace(envAppRoot))
        {
            try { return new AppRootResolution(Path.GetFullPath(envAppRoot), "MaaRM_APP_ROOT 环境变量"); }
            catch { /* 非法路径，继续降级 */ }
        }

        // 开发模式回退：从 exe 运行目录逐级向上找含 pyproject.toml 的目录（repo marker）
        var dir = new DirectoryInfo(baseDirectory);
        while (dir is not null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "pyproject.toml")))
                return new AppRootResolution(dir.FullName, "向上探测 pyproject.toml");
            dir = dir.Parent;
        }
        return new AppRootResolution(null, "未找到应用根");
    }
}

/// <summary>用户数据根（与 maaracing_master/core/paths.py 同口径）：APPDATA/MaaRacingMaster。
/// APPDATA 缺失时回退应用根旁 data/（源码运行 / 解压即用 ZIP）。</summary>
public static class UserDataRoot
{
    public static string? Resolve(string? appRoot, string? appData)
    {
        if (!string.IsNullOrWhiteSpace(appData))
            return Path.Combine(appData, "MaaRacingMaster");
        return string.IsNullOrEmpty(appRoot) ? null : Path.Combine(appRoot, "data");
    }
}
