using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Web.WebView2.Core;

namespace MaaRacingMaster.Shell;

/// <summary>
/// WebView2 共享运行时：用户数据目录显式指定到用户数据根（升级适配 P1 门禁）。
/// 不指定时 WebView2 默认落在 exe 旁（{exe}.exe.WebView2）——受管升级整体替换内容树，
/// 前端存储随升级丢失；显式指定到安装树之外后升级不再触碰。
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
            var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
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
            Directory.CreateDirectory(udf);
            // WinUI3 投影：数据目录走 CreateWithOptionsAsync（CreateAsync 只收选项对象，
            // 选项类不暴露数据目录——固定版本 winmd 核验过成员）
            _environment = await CoreWebView2Environment.CreateWithOptionsAsync(null, udf, null).AsTask().ConfigureAwait(false);
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
