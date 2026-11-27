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
            CheckCompatGate();
            CheckStartupReportShape();

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
            var marker = await client.FetchMarkerAsync(endpoints[0], "stable", CancellationToken.None);
            Program.Check("版本标记按渠道读取（无 channels 的旧标记对任何渠道生效）",
                marker is not null && marker.Version == "1.2.0", $"marker={marker?.Version}");
            var release = await client.FetchManifestAsync(endpoints[0], marker!, CancellationToken.None);
            Program.Check("版本标记与清单读取", release.Version == "1.2.0"
                && release.Full.Files.Count == newEntries.Count,
                $"version={release.Version} files={release.Full.Files.Count}");

            await CheckChannelMarkers(work);

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

            // 正式发行 ZIP 的真实形状：根目录放稳定启动器，程序文件在 program\ 下。
            // 清单以 program\ 为根，暂存只取子树——启动器本身不参与替换。
            var prefixedRoot = Path.Combine(work, "prefixed");
            Materialize(prefixedRoot,
                newFiles.ToDictionary(kv => "program/" + kv.Key, kv => kv.Value));
            Directory.CreateDirectory(prefixedRoot);
            File.WriteAllBytes(Path.Combine(prefixedRoot, "MaaRacingMaster.exe"),
                Encoding.UTF8.GetBytes("launcher"));
            var prefixedTag = "v1.3.0";
            var prefixedAssets = Path.Combine(feed, "assets", prefixedTag);
            Directory.CreateDirectory(prefixedAssets);
            var prefixedZip = Path.Combine(prefixedAssets, "MaaRacingMaster-1.3.0-win-x64.zip");
            var prefixedPaths = newFiles.Keys.Select(path => "program/" + path)
                .Append("MaaRacingMaster.exe");
            ZipTree(prefixedRoot, prefixedPaths, prefixedZip);
            var prefixedManifest = new
            {
                schema = 1,
                tag = prefixedTag,
                version = "1.3.0",
                channel = "stable",
                platform = "win-x64",
                compat = new { layout = "program-v1", launcher_api = UpdateProtocol.SupportedLauncherApi },
                full = new
                {
                    file = Path.GetFileName(prefixedZip),
                    size = new FileInfo(prefixedZip).Length,
                    sha256 = UpdateTree.HashFile(prefixedZip),
                    tree_sha256 = UpdateProtocol.TreeDigest(newEntries.Values),
                    files = newEntries.Values
                        .OrderBy(e => e.Path, UpdateProtocol.Utf8PathComparer.Instance)
                        .Select(e => new object[] { e.Path, e.Size, e.Sha256 })
                        .ToList(),
                },
                deltas = Array.Empty<object>(),
                limits = new { max_delta_ratio = 0.30 },
            };
            var prefixedManifestPath = Path.Combine(prefixedAssets, "MaaRacingMaster-1.3.0-update.json");
            File.WriteAllText(prefixedManifestPath,
                JsonSerializer.Serialize(prefixedManifest, ManifestJson));
            var prefixedRelease = UpdateProtocol.Parse(
                File.ReadAllText(prefixedManifestPath));
            var prefixedStage = Path.Combine(work, "stage-prefixed");
            var prefixedStaged = await new UpdateStager(new FileUpdateTransport(),
                    Path.Combine(work, "cache-prefixed"))
                .StageAsync(prefixedRelease, new Uri(Path.Combine(feed, "assets")), "9.9.9",
                    cleanOldRoot, prefixedStage, null, CancellationToken.None);
            Program.Check("正式包只暂存 program 子树，启动器不进暂存",
                prefixedStaged.Mode == StagedUpdate.ModeFull
                && UpdateTree.Verify(prefixedStage, prefixedRelease.Full).Count == 0
                && !File.Exists(Path.Combine(prefixedStage, "MaaRacingMaster.exe")),
                $"mode={prefixedStaged.Mode} notes={string.Join("；", prefixedStaged.Notes)}");

            // 恢复被故意破坏的增量与清单，再验证「额外文件不阻断增量」。
            // ZIP 重写后字节摘要会变（时间戳），必须按新清单重解析再交给暂存器。
            ZipTree(newRoot, payload, deltaZip);
            WriteManifest(feed, tag, oldEntries, newEntries, removed, fullZip, deltaZip);
            release = UpdateProtocol.Parse(File.ReadAllText(manifestPath));

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

            // 协调器接入：同一份本地来源，覆盖检查 → 下载暂存 → 提交交接。
            var installRoot = Path.Combine(work, "install");
            Directory.CreateDirectory(Path.Combine(installRoot, "program"));
            CopyTree(oldRoot, Path.Combine(installRoot, "program"));
            var fakeLauncher = Path.Combine(installRoot, "MaaRacingMaster.exe");
            File.Copy(Environment.ProcessPath!, fakeLauncher, overwrite: true);

            var applyingFired = false;
            var coordinator = new UpdateCoordinator(
                feedSource: "probe|" + feed,
                installRoot: installRoot,
                currentVersion: "1.1.0",
                initialSource: "probe",
                savePreferences: (_, _, _) => { },
                saveDismissedVersion: _ => { },
                saveAutoCheck: _ => { });
            coordinator.ApplyingStarted += () => applyingFired = true;

            var checkedSnapshot = await coordinator.CheckAsync();
            Program.Check("协调器便携检查 → available",
                checkedSnapshot.Phase == UpdateSnapshot.Available
                && checkedSnapshot.TargetVersion == "1.2.0"
                && checkedSnapshot.Managed,
                $"phase={checkedSnapshot.Phase} target={checkedSnapshot.TargetVersion} managed={checkedSnapshot.Managed}");

            await coordinator.StartDownloadAsync(checkedSnapshot.Revision);
            var ready = await WaitForPhaseAsync(coordinator, UpdateSnapshot.Ready,
                TimeSpan.FromSeconds(30));
            Program.Check("协调器下载 → ready 且暂存树等价",
                ready.Phase == UpdateSnapshot.Ready
                && UpdateTree.Verify(UpdateLauncher.StagingPath(installRoot), release.Full).Count == 0,
                $"phase={ready.Phase} error={ready.Error}");

            // 安装前的本地差异：程序目录里的玩家文件必须被识别；首版没有已装清单，
            // 只能报额外文件（BaselineKnown=false），不能假装识别了被改过的官方文件。
            File.WriteAllText(Path.Combine(installRoot, "program", "user-note.txt"), "mine");
            // 没有基线时，「本版移除的官方文件」与「玩家文件」无法区分，两个都会报成额外文件——
            // 这正是把已装清单留到下一次更新的理由。
            var inspect = await coordinator.InspectLocalChangesAsync();
            Program.Check("安装前识别额外文件（无基线时含发行包移除的文件）",
                inspect is not null && inspect.ExtraFiles == 2 && inspect.ModifiedFiles == 0
                && !inspect.BaselineKnown && inspect.Sample.Contains("user-note.txt"),
                inspect is null
                    ? "null"
                    : $"extra={inspect.ExtraFiles} modified={inspect.ModifiedFiles} baseline={inspect.BaselineKnown}");

            // 有已装清单时能识别「被改过的官方文件」：写入上一版基线并改一个官方文件。
            var installedPath = UpdateLauncher.InstalledManifestPath(installRoot);
            Directory.CreateDirectory(Path.GetDirectoryName(installedPath)!);
            var oldRelease = new UpdateRelease(1, "v1.1.0", "1.1.0", "stable", "win-x64",
                new UpdateCompat(UpdateProtocol.SupportedLayout, UpdateProtocol.SupportedLauncherApi),
                new UpdateFull("MaaRacingMaster-1.1.0-win-x64.zip", 1, new string('a', 64),
                    UpdateProtocol.TreeDigest(oldEntries.Values), oldEntries.Values.ToList()),
                Array.Empty<UpdateDelta>(), null);
            File.WriteAllText(installedPath, UpdateProtocol.Serialize(oldRelease));
            File.WriteAllText(
                Path.Combine(installRoot, "program", "runtime", "python", "python.exe"),
                "locally changed");
            var inspectBaseline = await coordinator.InspectLocalChangesAsync();
            Program.Check("有基线时识别被改过的官方文件",
                inspectBaseline is not null && inspectBaseline.BaselineKnown
                && inspectBaseline.ModifiedFiles == 1
                && inspectBaseline.Sample.Contains("runtime/python/python.exe"),
                inspectBaseline is null
                    ? "null"
                    : $"modified={inspectBaseline.ModifiedFiles} baseline={inspectBaseline.BaselineKnown}");

            // 备份落到「用户数据目录」而不是安装目录：只在本进程内改 APPDATA 指向临时目录。
            var appData = Path.Combine(work, "appdata");
            Environment.SetEnvironmentVariable("APPDATA", appData);

            var applied = await coordinator.ApplyAsync(backupExtras: true);
            var txPath = UpdateLauncher.TransactionPath(installRoot);
            Program.Check("协调器提交 → applying 且事务文件就绪",
                applied.Phase == UpdateSnapshot.Applying
                && applyingFired
                && File.Exists(txPath)
                && File.ReadAllText(txPath).Contains("target=1.2.0", StringComparison.Ordinal),
                $"phase={applied.Phase} error={applied.Error} fired={applyingFired}");
            Program.Check("选择备份后本地文件复制到用户数据目录",
                File.Exists(Path.Combine(appData, "MaaRacingMaster", "data", "_update_backup",
                    "1.1.0", "user-note.txt")),
                Path.Combine(appData, "MaaRacingMaster", "data", "_update_backup", "1.1.0"));
            Program.Check("交接写入启动汇报与清单副本",
                File.Exists(UpdateLauncher.PendingReportPath(installRoot))
                && File.Exists(UpdateLauncher.PendingManifestPath(installRoot)),
                UpdateLauncher.PendingReportPath(installRoot));
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

    private static void CheckStartupReportShape()
    {
        // 前端按 snake_case 读取；默认序列化输出 PascalCase 会让回滚被当成成功显示
        // （vundefined），这里锁住线上实际写出的字段名。
        var json = JsonSerializer.Serialize(new UpdateLauncher.StartupUpdateReport(
            "installed", "1.2.0", "delta", 1024, "probe", ""));
        Program.Check("启动汇报字段名为 snake_case",
            json.Contains("\"outcome\"", StringComparison.Ordinal)
            && json.Contains("\"version\"", StringComparison.Ordinal)
            && json.Contains("\"bytes\"", StringComparison.Ordinal),
            json);
    }

    private static void CheckCompatGate()
    {
        var hash = new string('a', 64);
        var baseRelease = new UpdateRelease(1, "v1.0.0", "1.0.0", "stable", "win-x64",
            new UpdateCompat("program-v1", UpdateProtocol.SupportedLauncherApi),
            new UpdateFull("f.zip", 1, hash, hash, Array.Empty<UpdateFileEntry>()),
            Array.Empty<UpdateDelta>(), null);
        Program.Check("兼容闸门：同接口放行", UpdateProtocol.IncompatibleReason(baseRelease) is null, "");

        var newerApi = baseRelease with
        {
            Compat = new UpdateCompat("program-v1", UpdateProtocol.SupportedLauncherApi + 1),
        };
        Program.Check("兼容闸门：要求更高的启动器接口 → 拒绝自动安装",
            UpdateProtocol.IncompatibleReason(newerApi) is not null,
            UpdateProtocol.IncompatibleReason(newerApi) ?? "");

        var otherLayout = baseRelease with { Compat = new UpdateCompat("flat-v0", 1) };
        Program.Check("兼容闸门：不同安装布局 → 拒绝自动安装",
            UpdateProtocol.IncompatibleReason(otherLayout) is not null,
            UpdateProtocol.IncompatibleReason(otherLayout) ?? "");
    }

    /// <summary>
    /// 用真实发布的 ZIP 与清单跑一遍暂存：发行包根目录带启动器、程序文件在 program\ 下，
    /// 这条回归锁住「正式包能通过验树」——发布流程里的 Python 自验不经过客户端代码。
    /// </summary>
    public static void StageRealPackage(string[] args)
    {
        var zip = Option(args, "--zip");
        var manifestPath = Option(args, "--manifest");
        var outDir = Option(args, "--out");
        if (zip is null || manifestPath is null || outDir is null)
        {
            Program.Check("stage 参数齐全", false, "需要 --zip / --manifest / --out");
            return;
        }

        var work = Path.Combine(Path.GetTempPath(),
            "maarm-stage-" + Guid.NewGuid().ToString("N")[..8]);
        try
        {
            var release = UpdateProtocol.Parse(File.ReadAllText(manifestPath));
            var incompatible = UpdateProtocol.IncompatibleReason(release);
            Program.Check("正式清单兼容当前客户端", incompatible is null, incompatible ?? "");

            var assets = Path.Combine(work, "assets", release.Tag);
            Directory.CreateDirectory(assets);
            File.Copy(zip, Path.Combine(assets, Path.GetFileName(zip)), overwrite: true);
            var contentRoot = Path.Combine(work, "current");
            Directory.CreateDirectory(contentRoot);
            if (Directory.Exists(outDir)) Directory.Delete(outDir, recursive: true);

            var stager = new UpdateStager(new FileUpdateTransport(), Path.Combine(work, "cache"));
            var staged = stager.StageAsync(release, new Uri(Path.Combine(work, "assets")),
                    "0.0.0-probe", contentRoot, outDir, null, CancellationToken.None)
                .GetAwaiter().GetResult();
            var problems = UpdateTree.Verify(outDir, release.Full);
            Program.Check("正式 ZIP 可暂存为程序树",
                staged.Mode == StagedUpdate.ModeFull && problems.Count == 0,
                string.Join("；", problems.Take(5)));
            Program.Check("暂存不含包根启动器",
                !File.Exists(Path.Combine(outDir, "MaaRacingMaster.exe")),
                "暂存根出现启动器");
        }
        catch (Exception ex)
        {
            Program.Check("正式 ZIP 暂存", false, ex.Message);
        }
        finally
        {
            try { Directory.Delete(work, recursive: true); } catch { /* 临时目录清理失败不影响结论 */ }
        }
    }

    private static string? Option(string[] args, string name)
    {
        var index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    private static async Task CheckChannelMarkers(string work)
    {
        var root = Path.Combine(work, "markers");
        Directory.CreateDirectory(root);
        var markerPath = Path.Combine(root, "latest_release.json");
        File.WriteAllText(markerPath, JsonSerializer.Serialize(new
        {
            tag = "v2.0.0-dev.1",
            version = "2.0.0-dev.1",
            channels = new
            {
                stable = new { tag = "v1.0.0", version = "1.0.0" },
                preview = new { tag = "v2.0.0-dev.1", version = "2.0.0-dev.1" },
            },
        }, ManifestJson));

        var endpoint = new UpdateSourceEndpoint("probe", new Uri(markerPath),
            new Uri(Path.Combine(root, "assets")));
        var client = new UpdateSourceClient(new FileUpdateTransport());
        var stable = await client.FetchMarkerAsync(endpoint, "stable", CancellationToken.None);
        var preview = await client.FetchMarkerAsync(endpoint, "preview", CancellationToken.None);
        var missing = await client.FetchMarkerAsync(endpoint, "nightly", CancellationToken.None);
        Program.Check("分渠道标记：各渠道取自己的版本，未声明渠道不回退顶层",
            stable?.Version == "1.0.0" && preview?.Version == "2.0.0-dev.1" && missing is null,
            $"stable={stable?.Version} preview={preview?.Version} nightly={missing?.Version}");
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
            compat = new { layout = "program-v1", launcher_api = UpdateProtocol.SupportedLauncherApi },
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

    private static async Task<UpdateSnapshot> WaitForPhaseAsync(
        UpdateCoordinator coordinator, string phase, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            var snapshot = coordinator.GetStatus();
            if (snapshot.Phase == phase || snapshot.Phase == UpdateSnapshot.Available)
                return snapshot;
            await Task.Delay(200);
        }
        return coordinator.GetStatus();
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
