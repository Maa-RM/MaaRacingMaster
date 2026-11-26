using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace MaaRacingMaster.Shell;

/// <summary>一个发行来源：版本标记地址 + 该来源的 Release 资产根。</summary>
public sealed record UpdateSourceEndpoint(string Name, Uri Marker, Uri Assets);

/// <summary>
/// 随包来源配置的解析：每行 <c>名字|地址</c>，地址是仓库根或本地目录。
/// 地址到具体端点的换算只在这里做一次，客户端与探针共用：
/// · GitHub 仓库 → 原始文件标记（raw.githubusercontent.com）+ <c>/releases/download</c> 资产根；
/// · CNB 仓库 → <c>/-/raw/master/...</c> 标记 + <c>/-/releases/download</c> 资产根；
/// · 本地目录 → <c>latest_release.json</c> + <c>assets/</c>（离线演练）。
/// 首选来源排在前面，其余等价来源按配置顺序回退。
/// </summary>
public static class UpdateSourceCatalog
{
    public static IReadOnlyList<UpdateSourceEndpoint> Parse(string? config, string preference)
    {
        var entries = new List<(string? Tag, string Base)>();
        foreach (var raw in (config ?? string.Empty).Split(new[] { "\r\n", "\n" },
                     StringSplitOptions.RemoveEmptyEntries))
        {
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith('#')) continue;
            var sep = line.IndexOf('|');
            entries.Add(sep > 0
                ? (line.Substring(0, sep).Trim().ToLowerInvariant(), line.Substring(sep + 1).Trim())
                : (null, line));
        }

        return entries
            .Where(e => e.Base.Length > 0)
            .OrderByDescending(e => string.Equals(e.Tag, preference, StringComparison.OrdinalIgnoreCase))
            .Select(e => Build(e.Tag ?? "auto", e.Base))
            .Where(e => e is not null)
            .Select(e => e!)
            .ToArray();
    }

    private static UpdateSourceEndpoint? Build(string name, string raw)
    {
        if (Directory.Exists(raw))
        {
            var root = Path.GetFullPath(raw);
            return new UpdateSourceEndpoint(name,
                new Uri(Path.Combine(root, "latest_release.json")),
                new Uri(Path.Combine(root, "assets")));
        }
        if (!Uri.TryCreate(raw, UriKind.Absolute, out var baseUri)) return null;
        if (baseUri.Scheme == Uri.UriSchemeFile)
        {
            var root = baseUri.LocalPath;
            return new UpdateSourceEndpoint(name,
                new Uri(Path.Combine(root, "latest_release.json")),
                new Uri(Path.Combine(root, "assets")));
        }
        if (baseUri.Scheme != Uri.UriSchemeHttp && baseUri.Scheme != Uri.UriSchemeHttps) return null;

        var trimmed = baseUri.ToString().TrimEnd('/');
        if (baseUri.Host.Equals("github.com", StringComparison.OrdinalIgnoreCase))
        {
            var marker = new Uri(trimmed.Replace("https://github.com/",
                "https://raw.githubusercontent.com/") + "/master/docs/latest_release.json");
            return new UpdateSourceEndpoint(name, marker, new Uri(trimmed + "/releases/download"));
        }
        if (baseUri.Host.Equals("cnb.cool", StringComparison.OrdinalIgnoreCase))
        {
            return new UpdateSourceEndpoint(name,
                new Uri(trimmed + "/-/raw/master/docs/latest_release.json"),
                new Uri(trimmed + "/-/releases/download"));
        }
        // 其他主机按「根下直接放 latest_release.json 与资产」处理（自建源/镜像）。
        return new UpdateSourceEndpoint(name,
            new Uri(trimmed + "/latest_release.json"),
            new Uri(trimmed));
    }

    /// <summary>清单地址：资产根 / tag / MaaRacingMaster-&lt;ver&gt;-update.json（名字由协议固定）。</summary>
    public static Uri ManifestUri(UpdateSourceEndpoint endpoint, string tag, string version)
        => new(endpoint.Assets.ToString().TrimEnd('/') + "/" + Uri.EscapeDataString(tag)
               + "/MaaRacingMaster-" + Uri.EscapeDataString(version) + "-update.json");

    /// <summary>资产地址：资产根 / tag / 文件名。</summary>
    public static Uri AssetUri(Uri assetsBase, string tag, string file)
        => new(assetsBase.ToString().TrimEnd('/') + "/" + Uri.EscapeDataString(tag)
               + "/" + Uri.EscapeDataString(file));
}

/// <summary>版本标记（docs/latest_release.json）的必要字段。</summary>
public sealed record ReleaseMarker(string Tag, string Version);

/// <summary>
/// 来源访问：先取版本标记，再按标记里的 tag 取清单。任何一步失败都抛异常，
/// 由调用方决定换源还是保留旧快照——这里不伪造「已是最新」。
/// </summary>
public sealed class UpdateSourceClient
{
    private readonly IUpdateTransport _transport;

    public UpdateSourceClient(IUpdateTransport transport) => _transport = transport;

    public async Task<(ReleaseMarker Marker, UpdateRelease Release)> FetchAsync(
        UpdateSourceEndpoint endpoint, CancellationToken cancellationToken)
    {
        var markerText = await _transport.GetStringAsync(endpoint.Marker, cancellationToken)
            .ConfigureAwait(false);
        var marker = ParseMarker(markerText, endpoint.Name);

        var manifestUri = UpdateSourceCatalog.ManifestUri(endpoint, marker.Tag, marker.Version);
        var manifestText = await _transport.GetStringAsync(manifestUri, cancellationToken)
            .ConfigureAwait(false);
        var release = UpdateProtocol.Parse(manifestText);
        if (!string.Equals(release.Tag, marker.Tag, StringComparison.Ordinal)
            || !string.Equals(release.Version, marker.Version, StringComparison.Ordinal))
        {
            throw new UpdateProtocolException(
                $"清单与版本标记不一致（标记 {marker.Tag}/{marker.Version}，清单 {release.Tag}/{release.Version}）");
        }
        return (marker, release);
    }

    private static ReleaseMarker ParseMarker(string json, string sourceName)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            var tag = root.TryGetProperty("tag", out var tagElement)
                ? tagElement.GetString()
                : null;
            var version = root.TryGetProperty("version", out var versionElement)
                ? versionElement.GetString()
                : null;
            if (string.IsNullOrWhiteSpace(tag) || string.IsNullOrWhiteSpace(version))
                throw new UpdateProtocolException($"{sourceName} 的版本标记缺少 tag/version");
            return new ReleaseMarker(tag.Trim(), version.Trim());
        }
        catch (JsonException ex)
        {
            throw new UpdateProtocolException($"{sourceName} 的版本标记不是合法 JSON: {ex.Message}");
        }
    }
}
