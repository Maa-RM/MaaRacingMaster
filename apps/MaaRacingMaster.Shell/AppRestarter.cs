using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Threading;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 应用重启：先拉起一个等待进程，等本进程完全退出后再启动新实例。
///
/// 为什么不直接自己启动新实例：单实例互斥体要到进程退出才释放，新实例会先撞上旧实例；
/// 等退出再启动同时避开互斥体与 WebView2 用户数据目录的占用竞争。
///
/// 源码模式与打包模式的差异集中在「怎么启动自己」这一处，由 <see cref="Program.CreateSelfStartInfo"/>
/// 统一：apphost 启动时直接复用 exe 路径；dotnet 启动时补回 dll 参数。参数原样透传，
/// 提权 token 与环境变量随子进程继承。
/// </summary>
internal static class AppRestarter
{
    /// <summary>重启已排程：窗口据此走计划内关窗（侧车在 OnClosed 里优雅退出）。</summary>
    public static event Action? Scheduled;

    private static int _scheduled;

    public static (bool Ok, string? Error) Schedule()
    {
        if (Interlocked.CompareExchange(ref _scheduled, 1, 0) != 0)
            return (false, "重启已在计划中");
        try
        {
            var args = new List<string>
            {
                Program.RestartWaiterFlag,
                Environment.ProcessId.ToString(CultureInfo.InvariantCulture),
            };
            args.AddRange(Program.StartupArgs);
            var psi = Program.CreateSelfStartInfo(args, createNoWindow: true);
            Process.Start(psi);
            Scheduled?.Invoke();
            ShellLog.Append("[restart] 已排程重启：等待本进程退出后启动新实例");
            return (true, null);
        }
        catch (Exception ex)
        {
            Interlocked.Exchange(ref _scheduled, 0);
            ShellLog.Append($"[restart] 排程失败: {ex.Message}");
            return (false, ex.Message);
        }
    }
}
