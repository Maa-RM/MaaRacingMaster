using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Web.WebView2.Core;

namespace MaaRacingMaster.Shell;

/// <summary>
/// WebView2 共享运行时：用户数据目录显式指定到用户数据根（升级不触碰前端存储）。
/// 不指定时 WebView2 默认落在 exe 旁（{exe}.exe.WebView2）——受管升级整体替换内容树，
/// 前端存储随升级丢失；显式指定到安装树之外后升级不再触碰。
/// 界面使用常规资料分区（非隐私模式）：InPrivate 分区只有内存版 GPU 缓存，着色器程序
/// 无法跨启动复用（见 MainWindow 初始化注释与 shell README「WebView2 渲染与编译缓存」）。
/// 主窗与悬浮窗共用同一环境实例（同享浏览器进程与存储分区）。
/// </summary>
public static class WebView2Runtime
{
    private static readonly SemaphoreSlim Gate = new(1, 1);
    private static CoreWebView2Environment? _environment;

    /// <summary>本次实际使用的用户数据目录（供预热标记等旁路读写）；创建失败为 null。</summary>
    public static string? UserDataFolder { get; private set; }

    /// <summary>
    /// 取共享环境；首次调用创建（数据目录 = 用户数据根\webview2）。创建失败返回 null，
    /// 调用方以默认位置回落初始化并记 shell.log——源码 / 旧 ZIP 模式的兜底；
    /// 受管模式 %APPDATA% 缺失已在启动前拒绝，不会落到这里。
    /// </summary>
    public static async Task<CoreWebView2Environment?> GetOrCreateAsync()
    {
        if (_environment is not null) return _environment;
        await Gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_environment is not null) return _environment;
            // 与 core/paths.py 同口径取 APPDATA 环境变量（已知目录 API 不认重定向）
            var appData = Environment.GetEnvironmentVariable("APPDATA");
            var appRoot = AppRootResolver.Resolve(
                Environment.GetCommandLineArgs(),
                Environment.GetEnvironmentVariable("MaaRM_APP_ROOT"),
                AppContext.BaseDirectory,
                appData).Root;
            var userData = UserDataRoot.Resolve(appRoot, appData);
            if (userData is null)
            {
                ShellLog.Append("[webview2] 用户数据根不可解析，WebView2 回落默认位置（exe 旁）");
                return null;
            }
            var udf = Path.Combine(userData, "webview2");
#if DEBUG
            // 显式参数随启动命令跨过 UAC。本机诊断使用独立目录，避免复用常规 WebView2 进程。
            const string debugPortPrefix = "--gui-debug-port=";
            var debugPortArgument = Array.Find(Environment.GetCommandLineArgs(),
                argument => argument.StartsWith(debugPortPrefix, StringComparison.Ordinal));
            var debugPort = 0;
            if (debugPortArgument is not null &&
                int.TryParse(debugPortArgument[debugPortPrefix.Length..], out debugPort) &&
                debugPort is > 0 and <= 65535)
            {
                udf = Path.Combine(userData, "webview2-gui-debug");
            }
            else
            {
                debugPort = 0;
            }
#endif
            Directory.CreateDirectory(udf);
            PruneCrashDumps(udf);
            // 浏览器参数：默认不内置任何渲染开关——GPU 光栅 + 磁盘着色器缓存已是顺畅路径
            // （首访编译一次、之后跨启动复用，机理与实测见 shell README「WebView2 渲染与编译缓存」）。
            // 仅诊断 env 与 Debug 调试端口追加参数，互不覆盖。
            var browserArgs = new List<string>();
            var probeArgs = Environment.GetEnvironmentVariable("MAARM_APP_WEBVIEW_ARGS");
            if (!string.IsNullOrWhiteSpace(probeArgs)) browserArgs.Add(probeArgs);
#if DEBUG
            if (debugPort != 0)
            {
                // 调试端口参数与诊断 env 追加共存，不再整体覆盖。
                browserArgs.Add($"--remote-debugging-port={debugPort} --remote-debugging-address=127.0.0.1");
            }
#endif
            CoreWebView2EnvironmentOptions? options = browserArgs.Count == 0
                ? null
                : new CoreWebView2EnvironmentOptions { AdditionalBrowserArguments = string.Join(" ", browserArgs) };
            // WinUI3 投影：数据目录走 CreateWithOptionsAsync（CreateAsync 只收选项对象，
            // 选项类不暴露数据目录——固定版本 winmd 核验过成员）
            _environment = await CoreWebView2Environment.CreateWithOptionsAsync(null, udf, options).AsTask().ConfigureAwait(false);
            UserDataFolder = udf;
            ShellLog.Append($"[webview2] 用户数据目录: {udf}");
            return _environment;
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[webview2] 自定义用户数据目录创建失败，回落默认位置: {ex.Message}");
            return null;
        }
        finally
        {
            Gate.Release();
        }
    }

    // 崩溃转储保留策略：Crashpad 默认不清理 reports，单份转储可达数十 MB，崩溃几次就能
    // 在用户数据目录里堆出上百 MB。启动时只留最近 3 份且不超过 7 天，只删
    // Crashpad\reports 下的 .dmp——着色器缓存与资料分区不在此列（它们是可复用 / 再生的
    // 资产）。清理失败不阻断启动。
    private const int CrashDumpKeepCount = 3;
    private static readonly TimeSpan CrashDumpMaxAge = TimeSpan.FromDays(7);

    private static void PruneCrashDumps(string udf)
    {
        // WebView2 会把浏览器状态放进用户数据目录下的 EBWebView 子目录；旧版运行时也有
        // 直接落在根下的情形，两处都扫一遍。
        foreach (var reports in new[]
        {
            Path.Combine(udf, "EBWebView", "Crashpad", "reports"),
            Path.Combine(udf, "Crashpad", "reports"),
        })
        {
            try
            {
                if (!Directory.Exists(reports)) continue;
                var dumps = new DirectoryInfo(reports).GetFiles("*.dmp", SearchOption.TopDirectoryOnly);
                if (dumps.Length == 0) continue;
                var cutoff = DateTime.UtcNow - CrashDumpMaxAge;
                var newest = dumps.OrderByDescending(f => f.LastWriteTimeUtc).ToArray();
                for (var i = 0; i < newest.Length; i++)
                {
                    if (i < CrashDumpKeepCount && newest[i].LastWriteTimeUtc >= cutoff) continue;
                    try
                    {
                        newest[i].Delete();
                    }
                    catch (IOException)
                    {
                        // 其它实例占用中：留到下次启动再清
                    }
                    catch (UnauthorizedAccessException)
                    {
                        // 同上
                    }
                }
            }
            catch (Exception ex)
            {
                ShellLog.Append($"[webview2] 崩溃转储清理失败: {ex.Message}");
            }
        }
    }
}
