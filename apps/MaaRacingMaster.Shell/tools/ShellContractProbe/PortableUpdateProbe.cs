using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MaaRacingMaster.Shell;

namespace MaaRacingMaster.Shell.ContractProbe;

/// <summary>
/// 便携版更新协议合同：用本地目录源驱动正式实现，覆盖
/// 清单校验、来源解析、增量选择、增量失败回退整包、额外文件拒绝与版本排序。
/// 夹具完全在临时目录内构造，不访问网络，也不触碰真实安装树。
/// </summary>
internal static class PortableUpdateProbe
{
    private static readonly JsonSerializerOptions ManifestJson = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = null,
    };

    public static async Task RunAsync(string[] args)
    {
        _ = args;
        var work = Path.Combine(Path.GetTempPath(),
            "maarm-portable-probe-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(work);
        try
        {
            CheckVersionOrdering();
            CheckTreeDigestVector();

            var oldFiles = new Dictionary<string, byte[]>
            {
                ["app/MaaRacingMaster.Shell.exe"] = Encoding.UTF8.GetBytes("shell-old"),
                ["runtime/python/python.exe"] = Encoding.UTF8.GetBytes("python"),
                ["maaracing_master/__init__.py"] = Encoding.UTF8.GetBytes("v1"),
                ["assets/icon.ico"] = Encoding.UTF8.GetBytes("icon"),
                ["remove-me.txt"] = Encoding.UTF8.GetBytes("bye"),
            };
            var newFiles = new Dictionary<string, byte[]>
            {
                ["app/MaaRacingMaster.Shell.exe"] = Encoding.UTF8.GetBytes("shell-new"),
                ["app/New.dll"] = Encoding.UTF8.GetBytes("new"),
                ["runtime/python/python.exe"] = Encoding.UTF8.GetBytes("python"),
                ["maaracing_master/__init__.py"] = Encoding.UTF8.GetBytes("v2"),
                ["assets/icon.ico"] = Encoding.UTF8.GetBytes("icon"),
            };

            var oldRoot = Path.Combine(work, "old");
            var newRoot = Path.Combine(work, "new");
            Materialize(oldRoot, oldFiles);
            Materialize(newRoot, newFiles);

            var oldEntries = Entries(oldFiles);
            var newEntries = Entries(newFiles);
            var payload = newEntries.Values
                .Where(e => !oldEntries.TryGetValue(e.Path, out var old) || old.Sha256 != e.Sha256)
                .Select(e => e.Path)
                .ToList();
            var removed = oldEntries.Keys.Where(path => !newEntries.ContainsKey(path)).ToList();

            var feed = Path.Combine(work, "feed");
            var tag = "v1.2.0";
            var assets = Path.Combine(feed, "assets", tag);
            Directory.CreateDirectory(assets);

            var fullZip = Path.Combine(assets, "MaaRacingMaster-1.2.0-win-x64.zip");
            ZipTree(newRoot, newEntries.Keys, fullZip);
            var deltaZip = Path.Combine(assets, "MaaRacingMaster-1.1.0-to-1.2.0-delta.zip");
            ZipTree(newRoot, payload, deltaZip);

            WriteManifest(feed, tag, oldEntries, newEntries, removed, fullZip, deltaZip);
            File.WriteAllText(Path.Combine(feed, "latest_release.json"),
                JsonSerializer.Serialize(new { tag, version = "1.2.0" }, ManifestJson));

            var endpoints = UpdateSourceCatalog.Parse("probe|" + feed, "probe");
            Program.Check("来源解析（本地目录）", endpoints.Count == 1
                && endpoints[0].Assets.LocalPath.EndsWith(Path.Combine("assets"), StringComparison.OrdinalIgnoreCase),
                $"count={endpoints.Count}");

            var transport = new FileUpdateTransport();
            var client = new UpdateSourceClient(transport);
            var (marker, release) = await client.FetchAsync(endpoints[0], CancellationToken.None);
            Program.Check("版本标记与清单读取", marker.Version == "1.2.0" && release.Version == "1.2.0"
                && release.Full.Files.Count == newEntries.Count,
                $"marker={marker.Version} version={release.Version} files={release.Full.Files.Count}");

            var manifestPath = Path.Combine(assets, "MaaRacingMaster-1.2.0-update.json");
            var broken = File.ReadAllText(manifestPath).Replace("\"app/New.dll\"", "\"../New.dll\"");
            var rejected = false;
            try { UpdateProtocol.Parse(broken); }
            catch (UpdateProtocolException) { rejected = true; }
            Program.Check("清单拒绝越界路径", rejected, "坏路径未被拒绝");

            var stager = new UpdateStager(transport, Path.Combine(work, "cache"));
            var stageA = Path.Combine(work, "stage-a");
            var staged = await stager.StageAsync(release, endpoints[0].Assets, "1.1.0",
                oldRoot, stageA, null, CancellationToken.None);
            Program.Check("基线匹配 → 走直接增量", staged.Mode == StagedUpdate.ModeDelta,
                $"mode={staged.Mode} notes={string.Join("；", staged.Notes)}");
            Program.Check("增量暂存后与目标树逐文件相等",
                UpdateTree.Verify(stageA, release.Full).Count == 0,
                string.Join("；", UpdateTree.Verify(stageA, release.Full)));

            File.WriteAllBytes(deltaZip, new byte[] { 1, 2, 3, 4 });
            var stageB = Path.Combine(work, "stage-b");
            var fallback = await stager.StageAsync(release, endpoints[0].Assets, "1.1.0",
                oldRoot, stageB, null, CancellationToken.None);
            Program.Check("增量损坏 → 回退完整包并完成暂存",
                fallback.Mode == StagedUpdate.ModeFull
                && fallback.Notes.Any(note => note.Contains("完整包", StringComparison.Ordinal))
                && UpdateTree.Verify(stageB, release.Full).Count == 0,
                $"mode={fallback.Mode} notes={string.Join("；", fallback.Notes)}");

            // 没有匹配增量时无法区分「旧发行移除的文件」与玩家文件：用一份不含被移除文件的
            // 干净旧树验证整包路径；带移除文件的旧树由增量用例覆盖（remove 列表允许删除）。
            var cleanOldRoot = Path.Combine(work, "old-clean");
            Materialize(cleanOldRoot,
                oldFiles.Where(kv => kv.Key != "remove-me.txt").ToDictionary(kv => kv.Key, kv => kv.Value));
            var stageC = Path.Combine(work, "stage-c");
            var fullOnly = await stager.StageAsync(release, endpoints[0].Assets, "1.0.0",
                cleanOldRoot, stageC, null, CancellationToken.None);
            Program.Check("无匹配基线 → 直接使用完整包", fullOnly.Mode == StagedUpdate.ModeFull
                && UpdateTree.Verify(stageC, release.Full).Count == 0,
                $"mode={fullOnly.Mode}");

            // 恢复被故意破坏的增量与清单，再验证「额外文件不阻断增量」。
            ZipTree(newRoot, payload, deltaZip);
            WriteManifest(feed, tag, oldEntries, newEntries, removed, fullZip, deltaZip);

            File.WriteAllText(Path.Combine(oldRoot, "user-note.txt"), "user data");
            var stageD = Path.Combine(work, "stage-d");
            var withExtra = await stager.StageAsync(release, endpoints[0].Assets, "1.1.0",
                oldRoot, stageD, null, CancellationToken.None);
            Program.Check("额外文件不阻断更新，也不带入新树",
                withExtra.Mode == StagedUpdate.ModeDelta
                && !File.Exists(Path.Combine(stageD, "user-note.txt"))
                && withExtra.Notes.Any(note => note.Contains("发行包之外", StringComparison.Ordinal))
                && UpdateTree.Verify(stageD, release.Full).Count == 0,
                $"mode={withExtra.Mode} notes={string.Join("；", withExtra.Notes)}");
            File.Delete(Path.Combine(oldRoot, "user-note.txt"));

            var modifiedRoot = Path.Combine(work, "old-modified");
            CopyTree(oldRoot, modifiedRoot);
            File.WriteAllText(
                Path.Combine(modifiedRoot, "runtime", "python", "python.exe"), "locally changed");
            var stageE = Path.Combine(work, "stage-e");
            var modified = await stager.StageAsync(release, endpoints[0].Assets, "1.1.0",
                modifiedRoot, stageE, null, CancellationToken.None);
            Program.Check("发行文件被改动 → 弃用增量、整包重建",
                modified.Mode == StagedUpdate.ModeFull
                && modified.Notes.Any(note => note.Contains("基线不一致", StringComparison.Ordinal))
                && UpdateTree.Verify(stageE, release.Full).Count == 0,
                $"mode={modified.Mode} notes={string.Join("；", modified.Notes)}");
        }
        finally
        {
            try { Directory.Delete(work, recursive: true); } catch { /* 临时目录清理失败不影响结论 */ }
        }
    }

    private static void CheckVersionOrdering()
    {
        Program.Check("版本排序：0.32.2-dev.2 < 0.32.3-dev.1",
            SemanticVersion.Parse("0.32.2-dev.2").CompareTo(SemanticVersion.Parse("0.32.3-dev.1")) < 0, "");
        Program.Check("版本排序：dev.9 < dev.10",
            SemanticVersion.Parse("0.32.3-dev.9").CompareTo(SemanticVersion.Parse("0.32.3-dev.10")) < 0, "");
        Program.Check("版本排序：正式版高于同号预发布",
            SemanticVersion.Parse("0.32.3").CompareTo(SemanticVersion.Parse("0.32.3-dev.1")) > 0, "");
    }

    private static void CheckTreeDigestVector()
    {
        var entries = new List<UpdateFileEntry>
        {
            new("a.txt", 6, Sha(Encoding.UTF8.GetBytes("hello\n"))),
            new("lib/b.bin", 3, Sha(new byte[] { 0, 1, 2 })),
        };
        Program.Check("树摘要跨语言向量",
            UpdateProtocol.TreeDigest(entries) ==
            "bd9f746716f48e74ae8c7f97dc10566de195d3e92daae999dfd722b19a3de05f",
            UpdateProtocol.TreeDigest(entries));
    }

    private static void WriteManifest(string feed, string tag,
                                      IReadOnlyDictionary<string, UpdateFileEntry> oldEntries,
                                      IReadOnlyDictionary<string, UpdateFileEntry> newEntries,
                                      IReadOnlyList<string> removed,
                                      string fullZip, string deltaZip)
    {
        var fullEntries = newEntries.Values
            .OrderBy(e => e.Path, UpdateProtocol.Utf8PathComparer.Instance)
            .Select(e => new object[] { e.Path, e.Size, e.Sha256 })
            .ToList();
        var manifest = new
        {
            schema = 1,
            tag,
            version = "1.2.0",
            channel = "stable",
            platform = "win-x64",
            compat = new { layout = "probe-v1", launcher_api = 1 },
            full = new
            {
                file = Path.GetFileName(fullZip),
                size = new FileInfo(fullZip).Length,
                sha256 = UpdateTree.HashFile(fullZip),
                tree_sha256 = UpdateProtocol.TreeDigest(newEntries.Values),
                files = fullEntries,
            },
            deltas = new[]
            {
                new
                {
                    from_version = "1.1.0",
                    from_tree_sha256 = UpdateProtocol.TreeDigest(oldEntries.Values),
                    file = Path.GetFileName(deltaZip),
                    size = new FileInfo(deltaZip).Length,
                    sha256 = UpdateTree.HashFile(deltaZip),
                    tree_sha256 = UpdateProtocol.TreeDigest(newEntries.Values),
                    remove = removed,
                    ratio = 0.01,
                },
            },
            limits = new { max_delta_ratio = 0.30 },
        };
        var assets = Path.Combine(feed, "assets", tag);
        File.WriteAllText(Path.Combine(assets, "MaaRacingMaster-1.2.0-update.json"),
            JsonSerializer.Serialize(manifest, ManifestJson));
    }

    private static Dictionary<string, UpdateFileEntry> Entries(Dictionary<string, byte[]> files)
        => files.ToDictionary(kv => kv.Key,
            kv => new UpdateFileEntry(kv.Key, kv.Value.LongLength, Sha(kv.Value)));

    private static void Materialize(string root, Dictionary<string, byte[]> files)
    {
        foreach (var (rel, data) in files)
        {
            var path = Path.Combine(root, rel.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllBytes(path, data);
        }
    }

    private static void ZipTree(string root, IEnumerable<string> paths, string zipPath)
    {
        if (File.Exists(zipPath)) File.Delete(zipPath);
        using var archive = ZipFile.Open(zipPath, ZipArchiveMode.Create);
        foreach (var rel in paths.OrderBy(p => p, UpdateProtocol.Utf8PathComparer.Instance))
        {
            var entry = archive.CreateEntry(rel, CompressionLevel.Optimal);
            using var output = entry.Open();
            using var input = File.OpenRead(
                Path.Combine(root, rel.Replace('/', Path.DirectorySeparatorChar)));
            input.CopyTo(output);
        }
    }

    private static void CopyTree(string source, string destination)
    {
        foreach (var file in Directory.EnumerateFiles(source, "*", SearchOption.AllDirectories))
        {
            var target = Path.Combine(destination, Path.GetRelativePath(source, file));
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.Copy(file, target, overwrite: true);
        }
    }

    private static string Sha(byte[] data)
        => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
}
