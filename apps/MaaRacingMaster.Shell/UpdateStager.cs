using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace MaaRacingMaster.Shell;

/// <summary>暂存完成的结果：模式（delta/full）、下载字节数与过程中的说明。</summary>
public sealed record StagedUpdate(UpdateRelease Release, string StageRoot, string Mode,
                                  long DownloadBytes, IReadOnlyList<string> Notes)
{
    public const string ModeDelta = "delta";
    public const string ModeFull = "full";
}

/// <summary>
/// 下载与暂存：把一个已解析、已校验的发布清单落成「完整的候选程序树」。
/// 顺序固定为：对拍本机发行文件（决定增量是否可用）→ 选直接增量或完整包 →
/// 下载并核对资产摘要 → 解包成完整树 → 逐文件对拍目标树。任何一步不满足
/// 都退回完整包；完整包仍不满足则失败，绝不把半成品交给安装交接。
///
/// 程序目录只应放发行文件（玩家数据在 %APPDATA%）：目录里多出来的内容不阻断更新、
/// 也不带入新版本，只在说明里列出；旧目录在替换成功前一直保留，需要时仍可取回。
/// 本类不碰运行中的安装树：所有写入都落在暂存目录。
/// </summary>
public sealed class UpdateStager
{
    private readonly IUpdateTransport _transport;
    private readonly string _cacheDir;

    public UpdateStager(IUpdateTransport transport, string cacheDir)
    {
        _transport = transport;
        _cacheDir = cacheDir;
    }

    public async Task<StagedUpdate> StageAsync(UpdateRelease release, Uri assetsBase,
                                               string currentVersion, string contentRoot,
                                               string stageRoot, IProgress<int>? progress,
                                               CancellationToken cancellationToken)
    {
        var target = release.Full.Files.ToDictionary(f => f.Path, StringComparer.OrdinalIgnoreCase);
        var current = UpdateTree.Scan(contentRoot);
        var delta = release.AvailableDeltas.FirstOrDefault(
            item => SameVersion(item.FromVersion, currentVersion));

        var notes = new List<string>();
        // 本版基线 = 目标文件 + 本次发行移除的文件；其余才是「发行包之外」的内容。
        var baselinePaths = new HashSet<string>(target.Keys, StringComparer.OrdinalIgnoreCase);
        foreach (var removed in delta?.RemovedPaths ?? Array.Empty<string>())
            baselinePaths.Add(removed);
        var extras = current.Keys
            .Where(path => !baselinePaths.Contains(path))
            .OrderBy(path => path, UpdateProtocol.Utf8PathComparer.Instance)
            .ToList();
        if (extras.Count > 0)
        {
            notes.Add($"安装目录里有 {extras.Count} 个发行包之外的文件，未带入新版本: "
                      + string.Join("、", extras.Take(5)) + (extras.Count > 5 ? " 等" : ""));
        }

        // 增量基线只对发行文件求树摘要：多出来的文件不参与比较，
        // 免得一个无关文件把本可增量的更新推回整包。
        var managed = current
            .Where(pair => baselinePaths.Contains(pair.Key))
            .ToDictionary(pair => pair.Key, pair => pair.Value, StringComparer.OrdinalIgnoreCase);
        if (delta is not null)
        {
            if (!string.Equals(UpdateTree.Digest(managed), delta.FromTreeSha256,
                    StringComparison.OrdinalIgnoreCase))
            {
                notes.Add("本机发行文件与增量基线不一致，改用完整包");
            }
            else
            {
                try
                {
                    await StageDeltaAsync(release, delta, assetsBase, contentRoot, stageRoot,
                        progress, cancellationToken).ConfigureAwait(false);
                    VerifyStage(release, stageRoot);
                    return new StagedUpdate(release, stageRoot, StagedUpdate.ModeDelta,
                        delta.Size, notes);
                }
                catch (OperationCanceledException)
                {
                    throw;
                }
                catch (Exception ex)
                {
                    notes.Add($"直接增量不可用，改用完整包: {ex.Message}");
                }
            }
        }
        else
        {
            notes.Add("没有匹配本机版本的直接增量，使用完整包");
        }

        await StageFullAsync(release, assetsBase, stageRoot, progress, cancellationToken)
            .ConfigureAwait(false);
        VerifyStage(release, stageRoot);
        return new StagedUpdate(release, stageRoot, StagedUpdate.ModeFull,
            release.Full.Size, notes);
    }

    private static bool SameVersion(string left, string right)
    {
        if (SemanticVersion.TryParse(left, out var a) && SemanticVersion.TryParse(right, out var b))
            return a.Equals(b);
        return string.Equals(left.Trim(), right.Trim(), StringComparison.OrdinalIgnoreCase);
    }

    private async Task StageFullAsync(UpdateRelease release, Uri assetsBase, string stageRoot,
                                      IProgress<int>? progress, CancellationToken ct)
    {
        ResetStage(stageRoot);
        var zipPath = await DownloadVerifiedAsync(
            UpdateSourceCatalog.AssetUri(assetsBase, release.Tag, release.Full.File),
            release.Full.File, release.Full.Size, release.Full.Sha256, progress, ct)
            .ConfigureAwait(false);
        ExtractZip(zipPath, stageRoot, ct);
        progress?.Report(100);
    }

    private async Task StageDeltaAsync(UpdateRelease release, UpdateDelta delta, Uri assetsBase,
                                       string contentRoot, string stageRoot,
                                       IProgress<int>? progress, CancellationToken ct)
    {
        ResetStage(stageRoot);
        var zipPath = await DownloadVerifiedAsync(
            UpdateSourceCatalog.AssetUri(assetsBase, release.Tag, delta.File),
            delta.File, delta.Size, delta.Sha256, progress, ct).ConfigureAwait(false);

        using var archive = ZipFile.OpenRead(zipPath);
        var payload = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in archive.Entries)
        {
            if (string.IsNullOrEmpty(entry.Name)) continue;
            var rel = UpdateProtocol.NormalizeRel(entry.FullName);
            var problem = UpdateProtocol.ValidatePath(rel);
            if (problem is not null)
                throw new UpdateProtocolException($"增量包路径不合法: {problem}");
            payload.Add(rel);
        }
        var target = release.Full.Files.ToDictionary(f => f.Path, StringComparer.OrdinalIgnoreCase);
        foreach (var rel in payload)
        {
            if (!target.ContainsKey(rel))
                throw new UpdateProtocolException($"增量包含目标清单之外的文件: {rel}");
        }

        // 未变化文件从本机树取（随后统一对拍哈希）：基线缺件即视为增量不可用。
        foreach (var entry in release.Full.Files)
        {
            if (payload.Contains(entry.Path)) continue;
            var source = Path.Combine(contentRoot, entry.Path.Replace('/', Path.DirectorySeparatorChar));
            if (!File.Exists(source))
                throw new UpdateProtocolException($"本机缺少增量依赖的基线文件: {entry.Path}");
            var destination = Path.Combine(stageRoot, entry.Path.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            File.Copy(source, destination, overwrite: true);
        }

        // 解包变化文件（同名先删，避免残留占位）。
        foreach (var entry in archive.Entries)
        {
            if (string.IsNullOrEmpty(entry.Name)) continue;
            var rel = UpdateProtocol.NormalizeRel(entry.FullName);
            var destination = SafeTarget(stageRoot, rel);
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            if (File.Exists(destination)) File.Delete(destination);
            entry.ExtractToFile(destination, overwrite: false);
            ct.ThrowIfCancellationRequested();
        }

        foreach (var rel in delta.RemovedPaths)
        {
            var destination = SafeTarget(stageRoot, rel);
            if (File.Exists(destination)) File.Delete(destination);
        }
        progress?.Report(100);
    }

    private async Task<string> DownloadVerifiedAsync(Uri relativeOrAbsolute, string fileName,
                                                     long expectedSize, string expectedSha256,
                                                     IProgress<int>? progress, CancellationToken ct)
    {
        if (!relativeOrAbsolute.IsAbsoluteUri)
            throw new UpdateProtocolException("清单资产地址不是绝对地址");
        Directory.CreateDirectory(_cacheDir);
        var destination = Path.Combine(_cacheDir, fileName + ".part");
        if (File.Exists(destination)) File.Delete(destination);

        var meter = progress is null
            ? null
            : new ByteProgress(loaded => progress.Report(
                expectedSize <= 0 ? 0 : (int)Math.Clamp(loaded * 90 / expectedSize, 0, 90)));
        await _transport.DownloadAsync(relativeOrAbsolute, destination, meter, ct)
            .ConfigureAwait(false);

        var info = new FileInfo(destination);
        if (info.Length != expectedSize)
            throw new UpdateProtocolException(
                $"资产大小不符（期望 {expectedSize}，实际 {info.Length}）: {fileName}");
        if (!string.Equals(UpdateTree.HashFile(destination), expectedSha256,
                StringComparison.OrdinalIgnoreCase))
        {
            throw new UpdateProtocolException($"资产摘要不符: {fileName}");
        }
        var finalPath = Path.Combine(_cacheDir, fileName);
        if (File.Exists(finalPath)) File.Delete(finalPath);
        File.Move(destination, finalPath);
        return finalPath;
    }

    private static void ExtractZip(string zipPath, string stageRoot, CancellationToken ct)
    {
        using var archive = ZipFile.OpenRead(zipPath);
        foreach (var entry in archive.Entries)
        {
            if (string.IsNullOrEmpty(entry.Name)) continue;
            var rel = UpdateProtocol.NormalizeRel(entry.FullName);
            var problem = UpdateProtocol.ValidatePath(rel);
            if (problem is not null)
                throw new UpdateProtocolException($"整包路径不合法: {problem}");
            var destination = SafeTarget(stageRoot, rel);
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            if (File.Exists(destination)) File.Delete(destination);
            entry.ExtractToFile(destination, overwrite: false);
            ct.ThrowIfCancellationRequested();
        }
    }

    private static string SafeTarget(string root, string rel)
    {
        var fullRoot = Path.GetFullPath(root);
        var target = Path.GetFullPath(Path.Combine(fullRoot,
            rel.Replace('/', Path.DirectorySeparatorChar)));
        var relative = Path.GetRelativePath(fullRoot, target);
        if (relative.StartsWith("..", StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new UpdateProtocolException($"路径越界，拒绝写入: {rel}");
        return target;
    }

    private static void ResetStage(string stageRoot)
    {
        if (string.IsNullOrWhiteSpace(stageRoot))
            throw new UpdateProtocolException("暂存目录未配置");
        var full = Path.GetFullPath(stageRoot);
        if (Path.GetPathRoot(full)!.TrimEnd('\\', '/').Length == 0
            || full.Length <= Path.GetPathRoot(full)!.Length + 2)
        {
            throw new UpdateProtocolException("暂存目录过于靠上，拒绝清理");
        }
        if (Directory.Exists(full)) Directory.Delete(full, recursive: true);
        Directory.CreateDirectory(full);
    }

    private static void VerifyStage(UpdateRelease release, string stageRoot)
    {
        var errors = UpdateTree.Verify(stageRoot, release.Full);
        if (errors.Count > 0)
            throw new UpdateProtocolException("候选程序树与发布清单不一致: " + string.Join("；", errors));
    }

    /// <summary>把累计字节同步换算成百分比；不用 Progress&lt;T&gt;，避免回调被投递到
    /// 非预期的同步上下文后在下载结束后才到达。</summary>
    private sealed class ByteProgress : IProgress<long>
    {
        private readonly Action<long> _report;
        public ByteProgress(Action<long> report) => _report = report;
        public void Report(long value) => _report(value);
    }
}
