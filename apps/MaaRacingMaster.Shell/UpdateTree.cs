using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 程序树的扫描与对拍：与发布端 <c>make_update_artifacts.py</c> 同一套规则
/// （缓存排除、路径规范、大小写碰撞、Windows 路径合法性、树摘要）。
/// 扫描结果按路径大小写不敏感去重；符号链接与目录联接一律拒绝（更新只接管普通文件树）。
/// </summary>
public static class UpdateTree
{
    public static string HashFile(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }

    /// <summary>扫描一棵程序树；发现符号链接、非法路径或大小写冲突时抛 <see cref="UpdateProtocolException"/>。</summary>
    public static Dictionary<string, UpdateFileEntry> Scan(string root)
    {
        var fullRoot = Path.GetFullPath(root);
        var files = new Dictionary<string, UpdateFileEntry>(StringComparer.OrdinalIgnoreCase);
        foreach (var path in Directory.EnumerateFiles(fullRoot, "*", SearchOption.AllDirectories))
        {
            var rel = UpdateProtocol.NormalizeRel(Path.GetRelativePath(fullRoot, path));
            if (UpdateProtocol.IsRuntimeCache(rel))
                continue;
            var problem = UpdateProtocol.ValidatePath(rel);
            if (problem is not null)
                throw new UpdateProtocolException(problem);
            var attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
                throw new UpdateProtocolException($"程序树含符号链接或目录联接: {rel}");
            var info = new FileInfo(path);
            files[rel] = new UpdateFileEntry(rel, info.Length, HashFile(path));
        }
        return files;
    }

    /// <summary>树摘要（与清单口径一致）。</summary>
    public static string Digest(IEnumerable<UpdateFileEntry> files) => UpdateProtocol.TreeDigest(files);

    public static string Digest(IReadOnlyDictionary<string, UpdateFileEntry> files) =>
        UpdateProtocol.TreeDigest(files.Values);

    /// <summary>
    /// 与目标清单对拍：文件集合必须完全一致、逐文件摘要相等、树摘要相等。
    /// 返回差异说明（空列表 = 等价）；消息里不带本机绝对路径。
    /// </summary>
    public static List<string> Verify(string root, UpdateFull expected)
    {
        var errors = new List<string>();
        Dictionary<string, UpdateFileEntry> actual;
        try
        {
            actual = Scan(root);
        }
        catch (Exception ex)
        {
            return new List<string> { "扫描暂存树失败: " + ex.Message };
        }

        var target = expected.Files.ToDictionary(f => f.Path, StringComparer.OrdinalIgnoreCase);
        var missing = target.Keys.Where(p => !actual.ContainsKey(p))
            .OrderBy(p => p, UpdateProtocol.Utf8PathComparer.Instance).ToList();
        var extra = actual.Keys.Where(p => !target.ContainsKey(p))
            .OrderBy(p => p, UpdateProtocol.Utf8PathComparer.Instance).ToList();
        if (missing.Count > 0)
            errors.Add("缺少文件: " + string.Join(", ", missing.Take(5))
                       + (missing.Count > 5 ? $" 等 {missing.Count} 个" : ""));
        if (extra.Count > 0)
            errors.Add("多出文件: " + string.Join(", ", extra.Take(5))
                       + (extra.Count > 5 ? $" 等 {extra.Count} 个" : ""));
        var mismatched = target.Keys.Where(p => actual.TryGetValue(p, out var a) && a.Sha256 != target[p].Sha256)
            .OrderBy(p => p, UpdateProtocol.Utf8PathComparer.Instance).ToList();
        if (mismatched.Count > 0)
            errors.Add("内容不一致: " + string.Join(", ", mismatched.Take(5))
                       + (mismatched.Count > 5 ? $" 等 {mismatched.Count} 个" : ""));

        if (errors.Count == 0 && Digest(actual) != expected.TreeSha256)
            errors.Add("树摘要与发布清单不一致");
        return errors;
    }
}
