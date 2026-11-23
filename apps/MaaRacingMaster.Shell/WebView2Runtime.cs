using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Web.WebView2.Core;

namespace MaaRacingMaster.Shell;

/// <summary>
/// WebView2 共享运行时：用户数据目录显式指定到用户数据根（升级不触碰前端存储）。
/// 不指定时 WebView2 默认落在 exe 旁（{exe}.exe.WebView2）——受管升级整体替换内容树，
/// 前端存储随升级丢失；显式指定到安装树之外后升级不再触碰。
/// 界面以隐私模式初始化（见 MainWindow）：站点数据不落盘，该目录只承载引擎基础
/// 结构，不再累积缓存（评审裁决：业务偏好住后端 config，浏览器侧无真实持久需求）。
/// 主窗与悬浮窗共用同一环境实例（同享浏览器进程与存储分区）。
/// </summary>
public static class WebView2Runtime
{
    private static readonly SemaphoreSlim Gate = new(1, 1);
    private static CoreWebView2Environment? _environment;

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
            // 浏览器参数经显式 options 传入 WinUI3 加载器。
            // CPU 光栅为默认：GPU 光栅路径上，新进程首访会对首次遇到的绘制组合现场编译
            // 着色器程序（Skia program cache miss → ANGLE 译 D3D），且整块瓦片的编译被
            // EndRasterCHROMIUM::Flush 同步串行等待，造成 100–250ms 帧停顿、切页进场动画
            // 被墙钟时钟快过全程（用户观感「页面瞬间出现」）。光栅改走 Skia 软件路径后
            // 编译为零、首访达再访级顺滑，绘制指令同源、视觉效果不变。
            // 2026-11-23 实测：数据页冷首访 127.8→22.2ms、齿轮弹窗 244.3→27.8ms、
            // 再访节奏无退化。机制与证据详见 shell README「WebView2 浏览器参数」。
            const string baseBrowserArgs = "--disable-gpu-rasterization";
            var probeArgs = Environment.GetEnvironmentVariable("MAARM_APP_WEBVIEW_ARGS");
            var browserArgs = string.IsNullOrWhiteSpace(probeArgs)
                ? baseBrowserArgs
                : $"{baseBrowserArgs} {probeArgs}";
#if DEBUG
            if (debugPort != 0)
            {
                // 调试端口参数与诊断 env 追加共存，不再整体覆盖。
                var debugArgs = $"--remote-debugging-port={debugPort} --remote-debugging-address=127.0.0.1";
                browserArgs = string.IsNullOrWhiteSpace(probeArgs)
                    ? $"{baseBrowserArgs} {debugArgs}"
                    : $"{baseBrowserArgs} {debugArgs} {probeArgs}";
            }
#endif
            CoreWebView2EnvironmentOptions? options = null;
            if (!string.IsNullOrWhiteSpace(browserArgs))
            {
                options = new CoreWebView2EnvironmentOptions { AdditionalBrowserArguments = browserArgs };
            }
            // WinUI3 投影：数据目录走 CreateWithOptionsAsync（CreateAsync 只收选项对象，
            // 选项类不暴露数据目录——固定版本 winmd 核验过成员）
            _environment = await CoreWebView2Environment.CreateWithOptionsAsync(null, udf, options).AsTask().ConfigureAwait(false);
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
}
