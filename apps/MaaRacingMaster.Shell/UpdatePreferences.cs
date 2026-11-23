using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace MaaRacingMaster.Shell;

/// <summary>用户级更新偏好（渠道 / 下载源 / 后台自动检查 / 提醒标记），落在用户数据根
/// 的 config/update-prefs.json，与业务配置同目录、随整树升级保留（受管安装不触碰
/// 用户数据根）。读失败（文件缺失/损坏/字段非法）一律回默认，不让偏好文件
/// 阻断升级能力。</summary>
public static class UpdatePreferences
{
    private sealed class Prefs
    {
        [JsonPropertyName("channel")] public string? Channel { get; set; }
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("from_preview")] public bool FromPreview { get; set; }
        [JsonPropertyName("dismissed_version")] public string? DismissedVersion { get; set; }
        // 可空：老配置没有这个字段 = 未表达，按默认开启处理（新增偏好不改变老用户行为）
        [JsonPropertyName("auto_check_enabled")] public bool? AutoCheckEnabled { get; set; }
    }

    // 设置与提醒标记可能在不同交互序列里先后落盘；同一进程内的读改写串行化，
    // 避免后写的一方把另一方刚写好的字段覆盖回旧值。
    private static readonly object Gate = new();

    /// <summary>返回 (channel, source, fromPreview)。渠道与源都有显式默认值
    /// （stable / cnb），不存在「未表达」状态；fromPreview = 从测试轨切回稳定轨、
    /// 稳定版尚未追上，仅在 channel 为 stable 时有意义。非法值一律按默认处理。</summary>
    public static (string Channel, string Source, bool FromPreview) Load()
    {
        var prefs = Read();
        var channel = prefs?.Channel == UpdateSnapshot.ChannelPreview
            ? UpdateSnapshot.ChannelPreview : UpdateSnapshot.ChannelStable;
        var source = prefs?.Source == UpdateSnapshot.SourceGithub
            ? UpdateSnapshot.SourceGithub : UpdateSnapshot.SourceCnb;
        var fromPreview = prefs?.FromPreview == true && channel == UpdateSnapshot.ChannelStable;
        return (channel, source, fromPreview);
    }

    /// <summary>后台自动检查开关。缺失（老配置）或损坏一律按开启处理——自动检查是
    /// 「发现新版本」的默认来源，静默降级会让用户失去提醒。</summary>
    public static bool LoadAutoCheck() => Read()?.AutoCheckEnabled ?? true;

    /// <summary>被用户标记「本次更新不再提示」的目标版本。按版本记，目标版本
    /// 变化后自然恢复提醒；空值或非法内容一律按未标记处理。</summary>
    public static string? LoadDismissedVersion()
    {
        var version = Read()?.DismissedVersion?.Trim();
        return string.IsNullOrEmpty(version) ? null : version;
    }

    /// <summary>fromPreview 只在稳定轨上有意义：切到测试轨时一律落 false，
    /// 避免旧的等待标记在用户再次进入测试轨后被错误复用。提醒标记与自动检查开关原样保留。</summary>
    public static void Save(string channel, string source, bool fromPreview)
    {
        lock (Gate)
        {
            var existing = Read();
            Write(new Prefs
            {
                Channel = channel,
                Source = source,
                FromPreview = channel == UpdateSnapshot.ChannelStable && fromPreview,
                DismissedVersion = existing?.DismissedVersion,
                AutoCheckEnabled = existing?.AutoCheckEnabled,
            });
        }
    }

    /// <summary>只改自动检查开关，保留渠道 / 下载源 / 等待标记 / 提醒标记。</summary>
    public static void SaveAutoCheck(bool enabled)
    {
        lock (Gate)
        {
            var existing = Read();
            var channel = existing?.Channel == UpdateSnapshot.ChannelPreview
                ? UpdateSnapshot.ChannelPreview : UpdateSnapshot.ChannelStable;
            var source = existing?.Source == UpdateSnapshot.SourceGithub
                ? UpdateSnapshot.SourceGithub : UpdateSnapshot.SourceCnb;
            Write(new Prefs
            {
                Channel = channel,
                Source = source,
                FromPreview = existing?.FromPreview == true && channel == UpdateSnapshot.ChannelStable,
                DismissedVersion = existing?.DismissedVersion,
                AutoCheckEnabled = enabled,
            });
        }
    }

    /// <summary>只改提醒标记，保留渠道 / 下载源 / 等待标记。</summary>
    public static void SaveDismissedVersion(string? version)
    {
        lock (Gate)
        {
            var existing = Read();
            var channel = existing?.Channel == UpdateSnapshot.ChannelPreview
                ? UpdateSnapshot.ChannelPreview : UpdateSnapshot.ChannelStable;
            var source = existing?.Source == UpdateSnapshot.SourceGithub
                ? UpdateSnapshot.SourceGithub : UpdateSnapshot.SourceCnb;
            Write(new Prefs
            {
                Channel = channel,
                Source = source,
                FromPreview = existing?.FromPreview == true && channel == UpdateSnapshot.ChannelStable,
                DismissedVersion = NormalizeVersion(version),
                AutoCheckEnabled = existing?.AutoCheckEnabled,
            });
        }
    }

    private static string? NormalizeVersion(string? version)
    {
        var trimmed = version?.Trim();
        return string.IsNullOrEmpty(trimmed) ? null : trimmed;
    }

    private static Prefs? Read()
    {
        var path = ConfigPath();
        if (path is null) return null;
        try
        {
            return File.Exists(path)
                ? JsonSerializer.Deserialize<Prefs>(File.ReadAllText(path))
                : null;
        }
        catch
        {
            return null;
        }
    }

    private static void Write(Prefs prefs)
    {
        var path = ConfigPath();
        if (path is null) return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, JsonSerializer.Serialize(prefs));
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
