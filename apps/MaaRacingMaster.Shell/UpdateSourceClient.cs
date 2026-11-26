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

/// <summary>一个渠道的版本标记（tag 与版本号；清单会再核对一次两者一致）。</summary>
public sealed record ReleaseMarker(string Tag, string Version);

/// <summary>
/// 版本标记文件（docs/latest_release.json）：顶层是「最新发布」，<c>channels</c> 是按渠道
/// 分发的入口。缺少 <c>channels</c> 的旧标记对任何渠道都生效（离线演练与历史源）；
/// 一旦写了 <c>channels</c>，就只认其中的渠道——缺该渠道即本源本渠道无更新，不回退顶层。
/// </summary>
public sealed record ReleaseMarkers(string? Tag, string? Version,
    IReadOnlyDictionary<string, ReleaseMarker>? Channels)
{
    public ReleaseMarker? ForChannel(string channel)
    {
        if (Channels is not null)
            return Channels.TryGetValue(channel, out var marker) ? marker : null;
        return Tag is null || Version is null ? null : new ReleaseMarker(Tag, Version);
    }
}

/// <summary>
/// 来源访问：先取版本标记，再按标记里的 tag 取清单。任何一步失败都抛异常，
/// 由调用方决定换源还是保留旧快照——这里不伪造「已是最新」。
/// </summary>
public sealed class UpdateSourceClient
{
    private readonly IUpdateTransport _transport;

    public UpdateSourceClient(IUpdateTransport transport) => _transport = transport;

    /// <summary>读本源该渠道的标记；本源没有这个渠道时返回 null（不是错误）。</summary>
    public async Task<ReleaseMarker?> FetchMarkerAsync(UpdateSourceEndpoint endpoint, string channel,
                                                       CancellationToken cancellationToken)
    {
        var markerText = await _transport.GetStringAsync(endpoint.Marker, cancellationToken)
            .ConfigureAwait(false);
        return ParseMarkers(markerText, endpoint.Name).ForChannel(channel);
    }

    public Task<UpdateRelease> FetchManifestAsync(UpdateSourceEndpoint endpoint, ReleaseMarker marker,
                                                  CancellationToken cancellationToken)
        => FetchManifestAtAsync(endpoint, marker.Tag, marker.Version, cancellationToken);

    /// <summary>
    /// 按已选定的 tag/版本取清单。镜像回退必须走这条：重新读备用源的「最新标记」会在
    /// 备用源已发下一版时错过仍可下载的旧目标（版本与摘要都对不上 → 白白放弃回退）。
    /// </summary>
    public async Task<UpdateRelease> FetchManifestAtAsync(UpdateSourceEndpoint endpoint, string tag,
                                                          string version,
                                                          CancellationToken cancellationToken)
    {
        var manifestUri = UpdateSourceCatalog.ManifestUri(endpoint, tag, version);
        var manifestText = await _transport.GetStringAsync(manifestUri, cancellationToken)
            .ConfigureAwait(false);
        var release = UpdateProtocol.Parse(manifestText);
        if (!string.Equals(release.Tag, tag, StringComparison.Ordinal)
            || !string.Equals(release.Version, version, StringComparison.Ordinal))
        {
            throw new UpdateProtocolException(
                $"清单与版本标记不一致（标记 {tag}/{version}，清单 {release.Tag}/{release.Version}）");
        }
        return release;
    }

    private static ReleaseMarkers ParseMarkers(string json, string sourceName)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                throw new UpdateProtocolException($"{sourceName} 的版本标记不是对象");

            var tag = ReadString(root, "tag");
            var version = ReadString(root, "version");
            Dictionary<string, ReleaseMarker>? channels = null;
            if (root.TryGetProperty("channels", out var channelsElement)
                && channelsElement.ValueKind == JsonValueKind.Object)
            {
                channels = new Dictionary<string, ReleaseMarker>(StringComparer.Ordinal);
                foreach (var property in channelsElement.EnumerateObject())
                {
                    if (property.Value.ValueKind != JsonValueKind.Object) continue;
                    var entryTag = ReadString(property.Value, "tag");
                    var entryVersion = ReadString(property.Value, "version");
                    if (entryTag is null || entryVersion is null)
                    {
                        throw new UpdateProtocolException(
                            $"{sourceName} 的 {property.Name} 渠道标记缺少 tag/version");
                    }
                    channels[property.Name] = new ReleaseMarker(entryTag, entryVersion);
                }
            }
            if (channels is null && (tag is null || version is null))
                throw new UpdateProtocolException($"{sourceName} 的版本标记缺少 tag/version");
            return new ReleaseMarkers(tag, version, channels);
        }
        catch (JsonException ex)
        {
            throw new UpdateProtocolException($"{sourceName} 的版本标记不是合法 JSON: {ex.Message}");
        }
    }

    private static string? ReadString(JsonElement element, string name)
        => element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()?.Trim()
            : null;
}
