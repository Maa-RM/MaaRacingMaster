using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace MaaRacingMaster.Shell;

/// <summary>用户级更新偏好（渠道 / 下载源），落在用户数据根的 config/update-prefs.json，
/// 与业务配置同目录、随整树升级保留（受管安装不触碰用户数据根）。
/// 读失败（文件缺失/损坏/字段非法）一律回默认，不让偏好文件阻断升级能力。</summary>
public static class UpdatePreferences
{
    private sealed class Prefs
    {
        [JsonPropertyName("channel")] public string? Channel { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("from_preview")] public bool FromPreview { get; set; }
    }

    /// <summary>返回 (channel, source, fromPreview)。渠道与源都有显式默认值
    /// （stable / cnb），不存在「未表达」状态；fromPreview = 从测试轨切回稳定轨、
    /// 稳定版尚未追上，仅在 channel 为 stable 时有意义。非法值一律按默认处理。</summary>
    public static (string Channel, string Source, bool FromPreview) Load()
    {
        var path = ConfigPath();
        if (path is null) return (UpdateSnapshot.ChannelStable, UpdateSnapshot.SourceCnb, false);
        try
        {
            if (!File.Exists(path))
                return (UpdateSnapshot.ChannelStable, UpdateSnapshot.SourceCnb, false);
            var prefs = JsonSerializer.Deserialize<Prefs>(File.ReadAllText(path));
            var channel = prefs?.Channel == UpdateSnapshot.ChannelPreview
                ? UpdateSnapshot.ChannelPreview : UpdateSnapshot.ChannelStable;
            var source = prefs?.Source == UpdateSnapshot.SourceGithub
                ? UpdateSnapshot.SourceGithub : UpdateSnapshot.SourceCnb;
            var fromPreview = prefs?.FromPreview == true && channel == UpdateSnapshot.ChannelStable;
            return (channel, source, fromPreview);
        }
        catch
        {
            return (UpdateSnapshot.ChannelStable, UpdateSnapshot.SourceCnb, false);
        }
    }

    /// <summary>fromPreview 只在稳定轨上有意义：切到测试轨时一律落 false，
    /// 避免旧的等待标记在用户再次进入测试轨后被错误复用。</summary>
    public static void Save(string channel, string source, bool fromPreview)
    {
        var path = ConfigPath();
        if (path is null) return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var json = JsonSerializer.Serialize(new Prefs
            {
                Channel = channel,
                Source = source,
                FromPreview = channel == UpdateSnapshot.ChannelStable && fromPreview,
            });
            File.WriteAllText(path, json);
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[update-prefs] 落盘失败: {ex.Message}");
        }
    }

    private static string? ConfigPath()
    {
        var appData = Environment.GetEnvironmentVariable("APPDATA");
        var appRoot = AppRootResolver.Resolve(
            Environment.GetCommandLineArgs(),
            Environment.GetEnvironmentVariable("MaaRM_APP_ROOT"),
            AppContext.BaseDirectory,
            appData).Root;
        var baseDir = UserDataRoot.Resolve(appRoot, appData);
        return baseDir is null ? null : Path.Combine(baseDir, "config", "update-prefs.json");
    }
}
