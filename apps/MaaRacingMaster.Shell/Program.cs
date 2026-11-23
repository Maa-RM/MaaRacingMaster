using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Velopack;

namespace MaaRacingMaster.Shell;

public static class Program
{
    // 单实例互斥体名：应用 requireAdministrator，用 Global 前缀跨提升级别可见；
    // 权限异常时降级为会话级命名空间（见 AcquireSingleInstance）。
    private const string SingleInstanceMutexName = "Global\\MaaRM_SingleInstance";

    // MessageBox 常量（user32）
    private const uint MB_YESNO = 0x00000004;
    private const uint MB_ICONQUESTION = 0x00000020;
    private const uint MB_ICONERROR = 0x00000030;
    private const int IDYES = 6;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    /// <summary>重启等待标记：由 <see cref="AppRestarter"/> 拉起，等旧进程退出后启动新实例。</summary>
    internal const string RestartWaiterFlag = "--maarm-restart-waiter";

    /// <summary>本次启动的应用参数（不含 exe/dll）：重启时原样透传。</summary>
    internal static string[] StartupArgs { get; private set; } = Array.Empty<string>();

    [STAThread]
    private static void Main(string[] args)
    {
        // 重启等待进程：不进入更新器钩子、单实例与 UI，只等旧进程退出后拉起新实例。
        if (TryRunRestartWaiter(args))
            return;
        StartupArgs = args;

        // 更新器钩子必须最早处理（最小注入形态，壳只在入口调用库的钩子路由）：钩子进程（--veloapp-* 参数）
        // 在库内自退出，绝不进入单实例/窗口/侧车逻辑；普通启动无钩子参数时立即返回直通。
        // 只此一处接更新器运行时，下载/提交等更新动作不进壳（由树外助手驱动，P2 起接线）。
        VelopackApp.Build().SetAutoApplyOnStartup(false).Run();

        // 受管安装前置检查：%APPDATA% 缺失时用户数据回退会落进可被整体替换的内容树，
        // 升级即丢数据——显式拒绝启动，不做静默降级。
        // 用户数据根取 APPDATA 环境变量（与 core/paths.py 同口径）：系统的已知目录
        // API 不认环境变量重定向，隔离/重定向场景下两边会分叉（实测踩过）。
        var appRoot = AppRootResolver.Resolve(
            Environment.GetCommandLineArgs(),
            Environment.GetEnvironmentVariable("MaaRM_APP_ROOT"),
            AppContext.BaseDirectory,
            Environment.GetEnvironmentVariable("APPDATA"));
        // 应用根决策落日志：受管判定、被拒覆盖值（「忽略并记录」的记录半）都从这里可查
        ShellLog.Append($"[approot] {appRoot.Decision}" +
                        (appRoot.RefusedReason is null ? "" : $"；拒绝启动: {appRoot.RefusedReason}"));
        if (appRoot.RefusedReason is not null)
        {
            MessageBoxW(IntPtr.Zero,
                "启动失败。\n\n" + appRoot.RefusedReason,
                "MaaRM", MB_ICONERROR);
            return;
        }

        // 单实例检查：已有实例运行 → 弹窗询问用户如何处理（见 AcquireSingleInstance）
        if (!AcquireSingleInstance())
            return; // 用户选择保留旧进程，本进程退出

        WinRT.ComWrappersSupport.InitializeComWrappers();
        Application.Start(p =>
        {
            var context = new DispatcherQueueSynchronizationContext(
                DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            new App();
        });
    }

    // 持有互斥体引用直到进程退出（Application.Start 阻塞，Main 返回时进程才结束）
    private static Mutex? s_instanceMutex;

    /// <summary>
    /// 重启等待进程：等旧进程退出后再启动新实例，避开单实例互斥体与 WebView2 用户数据目录的占用竞争。
    /// 返回 true 表示本次进程已完成等待职责，不应继续走正常启动链。
    /// </summary>
    private static bool TryRunRestartWaiter(string[] args)
    {
        if (args.Length < 2 || args[0] != RestartWaiterFlag)
            return false;
        if (!int.TryParse(args[1], out var pid))
            return true;

        try
        {
            using var old = Process.GetProcessById(pid);
            if (!old.WaitForExit(30000))
            {
                ShellLog.Append("[restart] 旧实例 30s 内未退出，取消本次重启");
                MessageBoxW(IntPtr.Zero,
                    "旧实例尚未退出，重启已取消。请手动关闭本程序后再打开。",
                    "MaaMR", MB_ICONERROR);
                return true;
            }
        }
        catch (ArgumentException)
        {
            // 旧进程已退出：直接继续启动
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[restart] 等待旧进程失败: {ex.Message}");
        }

        Thread.Sleep(400); // 给 WebView2 子进程留收尾时间，避开用户数据目录占用
        try
        {
            Process.Start(CreateSelfStartInfo(args.Skip(2), createNoWindow: true));
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[restart] 新实例启动失败: {ex.Message}");
            MessageBoxW(IntPtr.Zero,
                "重启失败，请手动启动 MaaMR。\n\n" + ex.Message,
                "MaaMR", MB_ICONERROR);
        }
        return true;
    }

    /// <summary>构造「启动自己」的参数：apphost 直接用 exe；dotnet 启动时补回 dll。</summary>
    internal static ProcessStartInfo CreateSelfStartInfo(IEnumerable<string> appArgs, bool createNoWindow)
    {
        var processPath = Environment.ProcessPath
            ?? throw new InvalidOperationException("无法解析当前进程路径");
        var psi = new ProcessStartInfo
        {
            FileName = processPath,
            UseShellExecute = false,
            CreateNoWindow = createNoWindow,
            // 保留原工作目录：相对参数（如 --root）在重启后仍按同一基准解析
            WorkingDirectory = Environment.CurrentDirectory,
        };
        if (Path.GetFileNameWithoutExtension(processPath)
            .Equals("dotnet", StringComparison.OrdinalIgnoreCase))
        {
            var cmd = Environment.GetCommandLineArgs();
            if (cmd.Length > 0)
                psi.ArgumentList.Add(cmd[0]);
        }
        foreach (var arg in appArgs)
            psi.ArgumentList.Add(arg);
        return psi;
    }

    /// <summary>
    /// 单实例互斥：持有命名 Mutex 直到进程退出。
    /// 检测到已有实例 → MessageBox 询问：
    ///   「是」= 启动新进程并关闭旧进程；「否」= 取消启动，保留旧进程。
    /// 返回 false 表示本进程应直接退出。
    /// </summary>
    private static bool AcquireSingleInstance()
    {
        Mutex? mutex = null;
        try
        {
            mutex = new Mutex(true, SingleInstanceMutexName, out var createdNew);
            if (createdNew)
            {
                s_instanceMutex = mutex;
                return true;
            }
        }
        catch (Exception)
        {
            // Global 前缀权限不足（异常场景）：降级为会话级命名空间
            try
            {
                mutex = new Mutex(true, "MaaRM_SingleInstance", out var createdNew);
                if (createdNew)
                {
                    s_instanceMutex = mutex;
                    return true;
                }
            }
            catch (Exception)
            {
                // 极端情况互斥体创建失败：放行单开（避免把用户锁死在外）
                s_instanceMutex = mutex;
                return true;
            }
        }

        // 已有实例在运行 → 询问用户
        var choice = MessageBoxW(
            IntPtr.Zero,
            "检测到 MaaRM 已在运行。\n\n" +
            "是(Y)：启动新进程（将自动关闭当前运行的旧进程）\n" +
            "否(N)：取消启动，保留当前运行的进程",
            "MaaRM — 是否启动新进程？",
            MB_YESNO | MB_ICONQUESTION);
        if (choice != IDYES)
            return false; // 用户选择保留旧进程 → 本进程退出

        // 用户选择替换：关闭旧进程（连带子进程，如 sidecar），等其退出后接管互斥体
        KillOtherInstances();
        try
        {
            mutex.WaitOne();
        }
        catch (AbandonedMutexException)
        {
            // 旧进程被强杀 → 互斥体 abandoned，属正常接管
        }
        s_instanceMutex = mutex;
        return true;
    }

    /// <summary>结束除本进程外的所有 MaaRacingMaster.Shell 实例（taskkill /T 连带子进程）。</summary>
    private static void KillOtherInstances()
    {
        var me = Process.GetCurrentProcess().Id;
        foreach (var p in Process.GetProcessesByName("MaaRacingMaster.Shell")
                     .Where(p => p.Id != me).ToList())
        {
            try
            {
                using var k = Process.Start(new ProcessStartInfo
                {
                    FileName = "taskkill.exe",
                    Arguments = $"/PID {p.Id} /T /F",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                });
                k?.WaitForExit(3000);
            }
            catch { /* 尽力而为，失败继续下一个 */ }
            try { p.WaitForExit(3000); } catch { }
        }
    }
}
