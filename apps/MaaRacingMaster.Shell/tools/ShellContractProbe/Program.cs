using System;
using System.Collections.Generic;
using System.IO;
using MaaRacingMaster.Shell;

namespace MaaRacingMaster.Shell.ContractProbe;

/// <summary>
/// 壳合同探针（P1 门禁「先建可失败的验证」）：对正式源码的路径解析/更新钩子/侧车退出
/// 行为做可机检断言。全过 → 退出码 0；任何失败逐条打印并退出码 1。
/// 用法：dotnet run --project tools/ShellContractProbe -- approot|hook|sidecar
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
        var mode = args.Length > 0 ? args[0] : "";
        switch (mode)
        {
            case "approot": CheckAppRoot(); break;
            default:
                Console.Error.WriteLine("用法: ShellContractProbe approot|hook|sidecar");
                return 2;
        }
        Console.WriteLine(s_failures == 0 ? "ALL PASS" : $"FAILURES: {s_failures}");
        return s_failures == 0 ? 0 : 1;
    }

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
            new[] { "MaaRM.Shell.exe" }, envOverride, contentRoot, @"C:\Users\x\AppData\Roaming");
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
            new[] { "MaaRM.Shell.exe", "--app-root", argOverride }, null, contentRoot, @"C:\Users\x\AppData\Roaming");
        Check("受管+冲突参数覆盖 → 内容根为准", r2.Root == contentRoot && r2.Managed,
              $"Root={r2.Root} Managed={r2.Managed}");
        Check("受管+冲突参数覆盖 → 记录被拒覆盖值",
              r2.IgnoredOverride is not null && r2.IgnoredOverride.Contains(argOverride, StringComparison.OrdinalIgnoreCase),
              $"IgnoredOverride={r2.IgnoredOverride}");

        // 受管：覆盖值指向内容根本身（大小写/尾分隔符差异）→ 不算冲突
        var r3 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe", "--app-root", contentRoot + "\\" }, null, contentRoot, @"C:\Users\x\AppData\Roaming");
        Check("受管+覆盖值=内容根 → 非冲突", r3.Root == contentRoot && r3.IgnoredOverride is null,
              $"Root={r3.Root} IgnoredOverride={r3.IgnoredOverride}");

        // 受管：%APPDATA% 缺失 → 拒绝启动（用户数据不得落进可被整体替换的内容树）
        var r4 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, contentRoot, "");
        Check("受管+APPDATA 缺失 → 拒绝启动", r4.RefusedReason is not null,
              $"RefusedReason={r4.RefusedReason ?? "<空>"}");
        var r5 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, contentRoot, @"C:\Users\x\AppData\Roaming");
        Check("受管+APPDATA 可用 → 不拒绝", r5.RefusedReason is null, $"RefusedReason={r5.RefusedReason}");

        // 非受管：历史口径不变（参数 > 环境变量 > 向上探测）
        var proj = MakeTempDir("proj");
        var deep = Path.Combine(proj, "sub", "deep");
        Directory.CreateDirectory(deep);
        File.WriteAllText(Path.Combine(proj, "pyproject.toml"), string.Empty);

        var argRoot = Norm(Path.Combine(proj, "via-arg"));
        var r6 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe", "--app-root", argRoot }, @"D:\ignored-env", deep, @"C:\Users\x\AppData\Roaming");
        Check("非受管+参数 → 参数优先", r6.Root == argRoot && !r6.Managed, $"Root={r6.Root}");

        var envRoot = Norm(Path.Combine(proj, "via-env"));
        var r7 = AppRootResolver.ResolveFresh(
            new[] { "MaaRM.Shell.exe" }, envRoot, deep, @"C:\Users\x\AppData\Roaming");
        Check("非受管+环境变量 → 环境变量优先于探测", r7.Root == envRoot && !r7.Managed, $"Root={r7.Root}");

        var r8 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, deep, @"C:\Users\x\AppData\Roaming");
        Check("非受管+无覆盖 → 向上探测 pyproject.toml", r8.Root == proj && !r8.Managed, $"Root={r8.Root}");

        var emptyBase = Path.Combine(MakeTempDir("bare"), "a", "b");
        Directory.CreateDirectory(emptyBase);
        var r9 = AppRootResolver.ResolveFresh(new[] { "MaaRM.Shell.exe" }, null, emptyBase, @"C:\Users\x\AppData\Roaming");
        Check("非受管+无任何锚 → null（壳自行降级）", r9.Root is null && !r9.Managed, $"Root={r9.Root}");

        // 用户数据根：与 paths.py 同口径
        Check("用户数据根:APPDATA 优先",
              UserDataRoot.Resolve(@"D:\app", @"C:\Users\x\AppData\Roaming") == @"C:\Users\x\AppData\Roaming\MaaRacingMaster",
              UserDataRoot.Resolve(@"D:\app", @"C:\Users\x\AppData\Roaming") ?? "<空>");
        Check("用户数据根:无 APPDATA 回退应用根旁 data",
              UserDataRoot.Resolve(@"D:\app", "") == @"D:\app\data",
              UserDataRoot.Resolve(@"D:\app", "") ?? "<空>");
    }
}
