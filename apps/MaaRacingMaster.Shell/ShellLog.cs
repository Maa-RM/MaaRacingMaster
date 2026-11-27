using System;
using System.IO;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 壳自身诊断日志（GUI 无控制台，stderr 不可见）：落用户数据根 logs/shell.log。
/// 只记启动期决策与生命周期异常（应用根解析、遗留进程清场、WebView2 环境、关停未确认），
/// 业务/会话日志仍由 Python 侧 logger 持有。追加式；写入失败静默（日志不可用不阻断功能）。
/// </summary>
public static class ShellLog
{
    public static void Append(string line)
    {
        try
        {
            // 与 core/paths.py 同口径取 APPDATA 环境变量（已知目录 API 不认重定向）
            var appData = Environment.GetEnvironmentVariable("APPDATA");
            var appRoot = AppRootResolver.Resolve(
                Environment.GetCommandLineArgs(),
                Environment.GetEnvironmentVariable("MaaRM_APP_ROOT"),
                AppContext.BaseDirectory).Root;
            var baseDir = UserDataRoot.Resolve(appRoot, appData);
            if (baseDir is null) return;
            var dir = Path.Combine(baseDir, "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(
                Path.Combine(dir, "shell.log"),
                $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {line}{Environment.NewLine}");
        }
        catch
        {
            // 日志落盘失败：不阻断任何功能
        }
    }
}
