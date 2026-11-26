using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace MaaRacingMaster.Shell;

/// <summary>发布清单中的一个文件：相对路径、字节数、内容 SHA-256（小写十六进制）。</summary>
public sealed record UpdateFileEntry(
    [property: JsonPropertyName("path")] string Path,
    [property: JsonPropertyName("size")] long Size,
    [property: JsonPropertyName("sha256")] string Sha256);

/// <summary>一份直达目标的文件级增量：从 FromVersion 的发行树到本清单目标树。</summary>
public sealed record UpdateDelta(
    [property: JsonPropertyName("from_version")] string FromVersion,
    [property: JsonPropertyName("from_tree_sha256")] string FromTreeSha256,
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("size")] long Size,
    [property: JsonPropertyName("sha256")] string Sha256,
    [property: JsonPropertyName("tree_sha256")] string TreeSha256,
    [property: JsonPropertyName("remove")] IReadOnlyList<string>? Remove,
    [property: JsonPropertyName("ratio")] double Ratio)
{
    public IReadOnlyList<string> RemovedPaths => Remove ?? Array.Empty<string>();
}

/// <summary>完整包：发布资产名、大小、ZIP 摘要、程序树摘要与逐文件清单。</summary>
public sealed record UpdateFull(
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("size")] long Size,
    [property: JsonPropertyName("sha256")] string Sha256,
    [property: JsonPropertyName("tree_sha256")] string TreeSha256,
    [property: JsonPropertyName("files")] IReadOnlyList<UpdateFileEntry> Files);

/// <summary>
/// 发布清单：<c>MaaRacingMaster-&lt;ver&gt;-update.json</c> 的强类型视图。
/// 清单只描述「程序树」这一层（整包 ZIP + 直接增量 + 删除清单），不描述下载地址；
/// 地址由来源端点按 tag 拼接，签名接入前以清单内容 + ZIP 摘要为信任根。
/// </summary>
public sealed record UpdateRelease(
    [property: JsonPropertyName("schema")] int Schema,
    [property: JsonPropertyName("tag")] string Tag,
    [property: JsonPropertyName("version")] string Version,
    [property: JsonPropertyName("channel")] string Channel,
    [property: JsonPropertyName("platform")] string Platform,
    [property: JsonPropertyName("compat")] UpdateCompat Compat,
    [property: JsonPropertyName("full")] UpdateFull Full,
    [property: JsonPropertyName("deltas")] IReadOnlyList<UpdateDelta>? Deltas,
    [property: JsonPropertyName("limits")] UpdateLimits? Limits)
{
    public IReadOnlyList<UpdateDelta> AvailableDeltas => Deltas ?? Array.Empty<UpdateDelta>();
}

public sealed record UpdateCompat(
    [property: JsonPropertyName("layout")] string Layout,
    [property: JsonPropertyName("launcher_api")] int LauncherApi);

public sealed record UpdateLimits(
    [property: JsonPropertyName("max_delta_ratio")] double MaxDeltaRatio);

/// <summary>清单不可用（格式/路径/摘要不合法）。消息面向日志与界面，不携带本机绝对路径。</summary>
public sealed class UpdateProtocolException : Exception
{
    public UpdateProtocolException(string message) : base(message) { }
}

/// <summary>
/// 便携版更新协议：清单解析与校验、路径规范、树摘要、版本比较。
/// 与 <c>scripts/release/make_update_artifacts.py</c> 使用同一套逐字节规则：
/// 相对路径用 <c>/</c> 分隔；树摘要 = SHA-256(按 UTF-8 字节序逐条拼接
/// <c>rel + 0x00 + hex + 0x0A</c>)；<c>__pycache__/</c> 与 <c>*.pyc</c> 不进清单。
/// </summary>
public static class UpdateProtocol
{
    public const int SupportedSchema = 1;
    public const string Platform = "win-x64";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = false,
        Converters = { new UpdateFileEntryConverter() },
    };

    private static readonly Regex Hex64 = new("^[0-9a-f]{64}$", RegexOptions.Compiled);
    private static readonly Regex DrivePrefix = new("^[A-Za-z]:", RegexOptions.Compiled);

    private static readonly HashSet<string> ReservedNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "con", "prn", "aux", "nul",
        "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    };

    /// <summary>UTF-8 字节序比较器：与发布端 Python 的 <c>sorted(key=encode('utf-8'))</c> 一致。</summary>
    public sealed class Utf8PathComparer : IComparer<string>
    {
        public static readonly Utf8PathComparer Instance = new();

        public int Compare(string? x, string? y)
        {
            if (ReferenceEquals(x, y)) return 0;
            if (x is null) return -1;
            if (y is null) return 1;
            var a = Encoding.UTF8.GetBytes(x);
            var b = Encoding.UTF8.GetBytes(y);
            var length = Math.Min(a.Length, b.Length);
            for (var i = 0; i < length; i++)
            {
                if (a[i] != b[i]) return a[i] < b[i] ? -1 : 1;
            }
            return a.Length.CompareTo(b.Length);
        }
    }

    public static UpdateRelease Parse(string json)
    {
        UpdateRelease? release;
        try
        {
            release = JsonSerializer.Deserialize<UpdateRelease>(json, JsonOptions);
        }
        catch (JsonException ex)
        {
            throw new UpdateProtocolException($"更新清单格式不合法: {ex.Message}");
        }
        if (release is null)
            throw new UpdateProtocolException("更新清单为空");

        Validate(release);
        return release;
    }

    private static void Validate(UpdateRelease release)
    {
        if (release.Schema != SupportedSchema)
            throw new UpdateProtocolException($"不支持的清单版本: {release.Schema}");
        if (string.IsNullOrWhiteSpace(release.Tag) || string.IsNullOrWhiteSpace(release.Version))
            throw new UpdateProtocolException("清单缺少 tag 或 version");
        if (!string.Equals(release.Platform, Platform, StringComparison.Ordinal))
            throw new UpdateProtocolException($"平台不匹配: {release.Platform}");
        if (release.Compat is null || string.IsNullOrWhiteSpace(release.Compat.Layout))
            throw new UpdateProtocolException("清单缺少安装布局标识");
        if (release.Full is null)
            throw new UpdateProtocolException("清单缺少完整包信息");

        ValidateAssetName(release.Full.File, "完整包");
        if (release.Full.Size <= 0)
            throw new UpdateProtocolException("完整包大小非法");
        RequireHash(release.Full.Sha256, "完整包");
        RequireHash(release.Full.TreeSha256, "完整包树");
        if (release.Full.Files is null || release.Full.Files.Count == 0)
            throw new UpdateProtocolException("完整包清单为空");

        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in release.Full.Files)
        {
            var problem = ValidatePath(entry.Path);
            if (problem is not null) throw new UpdateProtocolException(problem);
            if (entry.Size < 0) throw new UpdateProtocolException($"文件大小为负: {entry.Path}");
            RequireHash(entry.Sha256, entry.Path);
            if (!seen.Add(entry.Path))
                throw new UpdateProtocolException($"清单内路径重复（含大小写）: {entry.Path}");
        }
        if (!string.Equals(TreeDigest(release.Full.Files), release.Full.TreeSha256,
                StringComparison.OrdinalIgnoreCase))
        {
            throw new UpdateProtocolException("清单的逐文件清单与树摘要不自洽");
        }

        foreach (var delta in release.AvailableDeltas)
        {
            if (string.IsNullOrWhiteSpace(delta.FromVersion))
                throw new UpdateProtocolException("增量缺少基线版本");
            ValidateAssetName(delta.File, "增量");
            if (delta.Size <= 0) throw new UpdateProtocolException($"增量大小非法: {delta.File}");
            RequireHash(delta.Sha256, delta.File);
            RequireHash(delta.FromTreeSha256, "增量基线树");
            RequireHash(delta.TreeSha256, "增量目标树");
            if (!string.Equals(delta.TreeSha256, release.Full.TreeSha256, StringComparison.OrdinalIgnoreCase))
                throw new UpdateProtocolException($"增量目标树与完整包不一致: {delta.File}");
            foreach (var rel in delta.RemovedPaths)
            {
                var problem = ValidatePath(rel);
                if (problem is not null) throw new UpdateProtocolException(problem);
            }
        }
    }

    private static void ValidateAssetName(string name, string role)
    {
        if (string.IsNullOrWhiteSpace(name) || name != Path.GetFileName(name)
            || name.IndexOfAny(new[] { '/', '\\' }) >= 0)
        {
            throw new UpdateProtocolException($"{role}资产名不合法: {name}");
        }
    }

    private static void RequireHash(string value, string role)
    {
        if (string.IsNullOrWhiteSpace(value) || !Hex64.IsMatch(value))
            throw new UpdateProtocolException($"{role}的 SHA-256 不合法");
    }

    /// <summary>统一相对路径分隔符并去掉前导 <c>./</c>。</summary>
    public static string NormalizeRel(string rel)
    {
        var value = rel.Replace('\\', '/');
        while (value.StartsWith("./", StringComparison.Ordinal))
            value = value.Substring(2);
        return value;
    }

    /// <summary>Windows 相对路径校验（与发布端同一套规则）；合法返回 null。</summary>
    public static string? ValidatePath(string rel)
    {
        if (string.IsNullOrEmpty(rel)) return "路径为空";
        if (rel.StartsWith("/", StringComparison.Ordinal) || DrivePrefix.IsMatch(rel))
            return $"绝对路径不允许: {rel}";
        if (rel.Length > 32000) return "路径过长";
        foreach (var part in rel.Split('/'))
        {
            if (part.Length == 0 || part == "." || part == "..")
                return $"非法路径段: {rel}";
            if (part != part.TrimEnd(' ')) return $"路径段以空格结尾: {rel}";
            if (part.EndsWith(".", StringComparison.Ordinal)) return $"路径段以点结尾: {rel}";
            if (part.Length > 255) return $"路径段过长: {rel}";
            foreach (var ch in part)
            {
                if (ch < 32 || ch is '<' or '>' or ':' or '"' or '|' or '?' or '*')
                    return $"路径含非法字符: {rel}";
            }
            var stem = part.Split('.')[0];
            if (ReservedNames.Contains(stem)) return $"使用 Windows 保留名: {rel}";
        }
        return null;
    }

    /// <summary>运行期缓存不属于发行文件清单。</summary>
    public static bool IsRuntimeCache(string rel)
    {
        var lowered = rel.ToLowerInvariant();
        return lowered.Split('/').Contains("__pycache__") || lowered.EndsWith(".pyc", StringComparison.Ordinal);
    }

    /// <summary>规范树摘要：按 UTF-8 字节序拼接 rel\0hex\n 后做 SHA-256。</summary>
    public static string TreeDigest(IEnumerable<UpdateFileEntry> files)
    {
        using var stream = new MemoryStream();
        foreach (var entry in files.OrderBy(e => e.Path, Utf8PathComparer.Instance))
        {
            stream.Write(Encoding.UTF8.GetBytes(entry.Path));
            stream.WriteByte(0);
            stream.Write(Encoding.ASCII.GetBytes(entry.Sha256.ToLowerInvariant()));
            stream.WriteByte((byte)'\n');
        }
        stream.Position = 0;
        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(stream)).ToLowerInvariant();
    }

    private sealed class UpdateFileEntryConverter : JsonConverter<UpdateFileEntry>
    {
        public override UpdateFileEntry Read(ref Utf8JsonReader reader, Type typeToConvert,
                                             JsonSerializerOptions options)
        {
            if (reader.TokenType != JsonTokenType.StartArray)
                throw new JsonException("files 条目必须是 [path,size,sha256] 数组");
            reader.Read();
            var path = reader.GetString() ?? throw new JsonException("files 条目缺少路径");
            reader.Read();
            var size = reader.GetInt64();
            reader.Read();
            var sha = reader.GetString() ?? throw new JsonException("files 条目缺少摘要");
            reader.Read();
            if (reader.TokenType != JsonTokenType.EndArray)
                throw new JsonException("files 条目长度不正确");
            return new UpdateFileEntry(path, size, sha);
        }

        public override void Write(Utf8JsonWriter writer, UpdateFileEntry value,
                                   JsonSerializerOptions options)
        {
            writer.WriteStartArray();
            writer.WriteStringValue(value.Path);
            writer.WriteNumberValue(value.Size);
            writer.WriteStringValue(value.Sha256);
            writer.WriteEndArray();
        }
    }
}

/// <summary>
/// SemVer 子集比较：数字段按数值，正式版高于同号预发布，预发布标识符按
/// 「数字 &lt; 字母、数字按数值、字母按序号」逐段比较。构建元数据（+xxx）不参与排序。
/// 用于渠道候选与「基线是否更旧」判断，不用于展示。
/// </summary>
public readonly struct SemanticVersion : IComparable<SemanticVersion>, IEquatable<SemanticVersion>
{
    private readonly int[] _core;
    private readonly string[] _pre;
    private readonly bool _isRelease;

    private SemanticVersion(int[] core, string[] pre, bool isRelease)
    {
        _core = core;
        _pre = pre;
        _isRelease = isRelease;
    }

    public static SemanticVersion Parse(string value)
    {
        var text = value.Trim();
        if (text.StartsWith("v", StringComparison.OrdinalIgnoreCase)) text = text.Substring(1);
        var plus = text.IndexOf('+');
        if (plus >= 0) text = text.Substring(0, plus);
        var dash = text.IndexOf('-');
        var coreText = dash >= 0 ? text.Substring(0, dash) : text;
        var preText = dash >= 0 ? text.Substring(dash + 1) : string.Empty;
        var parts = coreText.Split('.');
        var core = new int[parts.Length];
        for (var i = 0; i < parts.Length; i++)
        {
            if (!int.TryParse(parts[i], out core[i]))
                throw new FormatException($"版本号不合法: {value}");
        }
        var pre = preText.Length == 0
            ? Array.Empty<string>()
            : preText.Split('.', StringSplitOptions.RemoveEmptyEntries);
        return new SemanticVersion(core, pre, pre.Length == 0);
    }

    public static bool TryParse(string value, out SemanticVersion version)
    {
        try
        {
            version = Parse(value);
            return true;
        }
        catch (FormatException)
        {
            version = default;
            return false;
        }
    }

    public int CompareTo(SemanticVersion other)
    {
        var length = Math.Max(_core?.Length ?? 0, other._core?.Length ?? 0);
        for (var i = 0; i < length; i++)
        {
            var a = _core is not null && i < _core.Length ? _core[i] : 0;
            var b = other._core is not null && i < other._core.Length ? other._core[i] : 0;
            if (a != b) return a < b ? -1 : 1;
        }
        // 正式版高于同号预发布
        if (_isRelease != other._isRelease) return _isRelease ? 1 : -1;
        var preA = _pre ?? Array.Empty<string>();
        var preB = other._pre ?? Array.Empty<string>();
        var count = Math.Min(preA.Length, preB.Length);
        for (var i = 0; i < count; i++)
        {
            var aIsNumber = int.TryParse(preA[i], out var aNumber);
            var bIsNumber = int.TryParse(preB[i], out var bNumber);
            if (aIsNumber && bIsNumber)
            {
                if (aNumber != bNumber) return aNumber < bNumber ? -1 : 1;
                continue;
            }
            if (aIsNumber != bIsNumber) return aIsNumber ? -1 : 1;
            var cmp = string.CompareOrdinal(preA[i], preB[i]);
            if (cmp != 0) return cmp < 0 ? -1 : 1;
        }
        return preA.Length.CompareTo(preB.Length);
    }

    public bool Equals(SemanticVersion other) => CompareTo(other) == 0;
    public override bool Equals(object? obj) => obj is SemanticVersion v && Equals(v);
    public override int GetHashCode() => ToString().GetHashCode(StringComparison.Ordinal);
    public override string ToString() =>
        string.Join('.', _core ?? Array.Empty<int>()) + (_isRelease ? "" : "-" + string.Join('.', _pre));
}
