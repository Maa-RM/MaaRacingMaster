using System;
using System.IO;
using System.Linq;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;
using Velopack;
using Velopack.Locators;

namespace MaaRacingMaster.Shell;

/// <summary>宿主更新协调器的对外快照（响应形状，snake_case 与业务侧一致）。
/// revision 单调递增：只在状态实质变化时 +1，前端据此丢弃过期轮询响应。</summary>
public sealed record UpdateSnapshot(
    [property: JsonPropertyName("phase")] string Phase,
    [property: JsonPropertyName("progress")] int Progress,
    [property: JsonPropertyName("target_version")] string? TargetVersion,
    [property: JsonPropertyName("error")] string? Error,
    [property: JsonPropertyName("revision")] long Revision,
    [property: JsonPropertyName("managed")] bool Managed)
{
    public const string Unavailable = "unavailable";  // 非受管安装：自动升级不可用（手动升级口径）
    public const string Idle = "idle";
    public const string Available = "available";      // 已发现可安装目标，未下载
    public const string UpToDate = "up_to_date";      // 已是目标版本
    public const string Downloading = "downloading";
    public const string Ready = "ready";              // 已下载完成，可提交
    public const string Preparing = "preparing";      // 冻结后端 + 退出确认中
    public const string Applying = "applying";        // 已交给更新器：等本进程退出后替换
}

/// <summary>
/// 宿主更新协调器：应用级 SDK 适配的唯一宿主。
/// 限制为固定来源、完整目标版本、下载任务与安装意图——不建通用插件更新服务，
/// 前端只发固定意图，不传任何来源/路径/目标版本组合。
///
/// 状态机：unavailable（非受管/降级）→ idle →(check)→ available|up_to_date
/// →(download)→ ready →(apply)→ preparing → applying。
/// 提交闸门：apply 必须先经 prepareGate（后端冻结 + 有界退出确认），
/// 闸门拒绝或异常时状态回 ready 并带原因，解冻由闸门实现负责；全部确认后才
/// 交给更新器（WaitExitThenApplyUpdates：等本进程退出后整树替换并重启）。
/// 来源未配置时诚实返回不可用，不伪造检查结果。
/// </summary>
public sealed class UpdateCoordinator
{
    /// <summary>提交前准备闸门：true = 后端已冻结且已确认退出（可交更新器）；
    /// false/异常 = 不能提交（实现内部负责解冻）。返回原因经 out 参数带给快照。</summary>
    public delegate Task<string?> PrepareGateAsync();

    /// <summary>提交进入 applying 相位（等本进程退出后整树替换）时触发一次：
    /// 壳订阅它做「计划内关闭」（窗口收尾 → Update.exe 检测退出 → 替换 → 重启）。</summary>
    public event Action? ApplyingStarted;

    private readonly string[] _sources;            // 按序尝试的固定来源（主源 + 镜像；同目标发行集）
    private readonly IVelopackLocator? _locator;   // 显式定位器注入（合同探针用）；null = 默认定位器
    private readonly PrepareGateAsync? _prepareGate;
    private readonly bool _managed;
    private readonly object _gate = new();

    private long _revision;
    private string _phase = UpdateSnapshot.Idle;
    private int _progress;
    private string? _targetVersion;
    private string? _error;
    private UpdateManager? _manager;               // 锁定为检查成功的源（下载/提交绑定同一已验证来源）
    private string? _lockedSource;                 // 与 _manager 配对：镜像回退时跳过已锁源
    private UpdateInfo? _pending;
    private CancellationTokenSource? _downloadCts;
    private Task? _downloadTask;

    public UpdateCoordinator(string? feedSource, bool managed,
                             IVelopackLocator? locator = null,
                             PrepareGateAsync? prepareGate = null)
    {
        // 多源支持：随包配置每行一个来源（主源 + 镜像），按序尝试；全部失败不换目标。
        _sources = (feedSource ?? string.Empty)
            .Split(new[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries)
            .Select(s => s.Trim())
            .Where(s => s.Length > 0 && !s.StartsWith('#'))
            .ToArray();
        _managed = managed;
        _locator = locator;
        _prepareGate = prepareGate;
        if (!managed)
        {
            _phase = UpdateSnapshot.Unavailable;
            _error = "非受管安装，自动升级不可用（请使用手动升级入口）";
        }
    }

    public UpdateSnapshot GetStatus()
    {
        lock (_gate)
        {
            return Snapshot();
        }
    }

    /// <summary>检查更新：仅在 idle/available/up_to_date/ready 允许（刷新目标）。
    /// 多源按序尝试：第一个「可达且给出结论」的源胜出并锁定（下载/提交绑定同一已验证
    /// 来源）；全部不可达/失败时诚实报错并可重试——不换目标，不把空/坏镜像误报为
    /// 「已是最新」（来源故障不得换目标，也不得误报已可安装）。</summary>
    public async Task<UpdateSnapshot> CheckAsync()
    {
        lock (_gate)
        {
            if (!CanRunCheck())
                return Snapshot(BusyReason());
            if (_sources.Length == 0)
            {
                _targetVersion = null;
                _pending = null;
                return SetState(UpdateSnapshot.Idle, error: "更新源未配置");
            }
        }
        string? lastError = null;
        for (var i = 0; i < _sources.Length; i++)
        {
            var source = _sources[i];
            if (!IsSourceReachable(source))
            {
                lastError = $"{source}（不可达或未就绪）";
                continue;
            }
            try
            {
                var manager = GetManagerFor(source);
                var update = await manager.CheckForUpdatesAsync().ConfigureAwait(false);
                lock (_gate)
                {
                    _manager = manager; // 锁定本源：下载/提交绑定同一已验证来源
                    _lockedSource = source;
                    if (update is null)
                    {
                        _targetVersion = null;
                        _pending = null;
                        return SetState(UpdateSnapshot.UpToDate);
                    }
                    _pending = update;
                    _targetVersion = update.TargetFullRelease.Version.ToString();
                    return SetState(UpdateSnapshot.Available);
                }
            }
            catch (Exception ex)
            {
                lastError = $"{source}（{ex.Message}）";
            }
        }
        lock (_gate)
        {
            _targetVersion = null;
            _pending = null;
            return SetState(UpdateSnapshot.Idle, error: $"所有更新源均不可达：{lastError}");
        }
    }

    /// <summary>源可达性预检：本地目录必须存在且非空（空目录 = 镜像未就绪——
    /// 更新库对空目录返回「无更新」，直接信任会把故障误报为已是最新）；网络源
    /// 的可达性由检查调用的异常区分（网络错误抛出，正常无更新返回 null）。</summary>
    private static bool IsSourceReachable(string source)
    {
        if (Directory.Exists(source))
            return Directory.EnumerateFileSystemEntries(source).Any();
        if (source.StartsWith("http", StringComparison.OrdinalIgnoreCase))
            return true;
        return false;
    }

    private UpdateManager GetManagerFor(string source)
    {
        return _locator is null
            ? new UpdateManager(source)
            : new UpdateManager(source, new UpdateOptions(), _locator);
    }

    /// <summary>开始下载：仅 available 允许；下载中重复调用复用当前任务（幂等）；
    /// ready 重复调用幂等返回（已就绪无需重下）。启动即回包 downloading 快照——
    /// 下载在后台任务进行，进度与结果由独立状态查询获得（评审回归：等待整段下载
    /// 会让首次下载期间界面拿不到进度、也无法取消）。</summary>
    public Task<UpdateSnapshot> StartDownloadAsync()
    {
        UpdateInfo? pending;
        lock (_gate)
        {
            if (_phase == UpdateSnapshot.Downloading && _downloadTask is not null)
                return Task.FromResult(Snapshot()); // 复用当前任务
            if (_phase == UpdateSnapshot.Ready)
                return Task.FromResult(Snapshot()); // 已下载完成：重复调用幂等返回
            if (_phase != UpdateSnapshot.Available)
                return Task.FromResult(Snapshot(BusyReason()));
            if (_pending is null)
                return Task.FromResult(Snapshot("没有可安装目标，请先检查更新"));
            pending = _pending;
            _downloadCts = new CancellationTokenSource();
            _revision++;
            _phase = UpdateSnapshot.Downloading;
            _error = null;
            // 任务在锁外创建：DownloadAsync 的同步前段与完成翻转都拿同一把锁，
            // 持锁启动会在同步完成路径上自我死锁。
        }
        var token = _downloadCts!.Token;
        _downloadTask = Task.Run(() => DownloadAsync(pending!, token));
        return Task.FromResult(GetStatus());
    }

    private async Task DownloadAsync(UpdateInfo update, CancellationToken ct)
    {
        string? failure = null;
        try
        {
            var manager = _manager
                ?? throw new InvalidOperationException("未锁定更新源（先检查更新）");
            await DownloadOnManagerAsync(manager, update, ct).ConfigureAwait(false);
            MarkReadyIfDownloading();
            return;
        }
        catch (OperationCanceledException)
        {
            BackToAvailable("下载已取消");
            return;
        }
        catch (Exception ex)
        {
            failure = ex.Message;
        }
        // 镜像回退（评审回归：索引可达不等于包可达）：锁定源下载失败时按序尝试其余源，
        // 仅当镜像给出「同目标」发行（版本与内容指纹一致）才换源重试；同版本不同内容
        // 视为不同发行，不换。全部失败回可重试的 available，目标不换。
        try
        {
            if (await TryDownloadMirrorAsync(update, ct).ConfigureAwait(false))
            {
                MarkReadyIfDownloading();
                return;
            }
            failure = $"{failure}（镜像回退未找到同目标源）";
        }
        catch (OperationCanceledException)
        {
            BackToAvailable("下载已取消");
            return;
        }
        catch (Exception ex)
        {
            failure = $"{failure}；镜像回退也失败: {ex.Message}";
        }
        BackToAvailable($"下载失败: {failure}");
    }

    private async Task DownloadOnManagerAsync(UpdateManager manager, UpdateInfo update, CancellationToken ct)
    {
        // 进度回调来自库的下载线程：跨线程进入 UI 无关的锁内状态即可（revision 单调）
        await manager.DownloadUpdatesAsync(update, p =>
        {
            lock (_gate)
            {
                if (_phase == UpdateSnapshot.Downloading)
                {
                    _progress = p;
                    _revision++;
                }
            }
        }, ct).ConfigureAwait(false);
    }

    /// <summary>镜像回退：按序尝试其余源；候选给出同目标发行才换源下载。
    /// 换源成功即重绑管理器与目标（提交绑定到已验证的镜像）。</summary>
    private async Task<bool> TryDownloadMirrorAsync(UpdateInfo update, CancellationToken ct)
    {
        foreach (var source in _sources)
        {
            if (source == _lockedSource || !IsSourceReachable(source))
                continue;
            try
            {
                var manager = GetManagerFor(source);
                var info = await manager.CheckForUpdatesAsync().ConfigureAwait(false);
                if (info?.TargetFullRelease is null || !SameTarget(update.TargetFullRelease, info.TargetFullRelease))
                    continue;
                await DownloadOnManagerAsync(manager, info, ct).ConfigureAwait(false);
                lock (_gate)
                {
                    _manager = manager;
                    _lockedSource = source;
                    _pending = info;
                }
                return true;
            }
            catch (OperationCanceledException) { throw; }
            catch { /* 该镜像不可用或下载失败：继续下一候选 */ }
        }
        return false;
    }

    /// <summary>「同目标」判定：版本与内容指纹（SHA256）一致——回退只换源，不换发行。</summary>
    private static bool SameTarget(VelopackAsset a, VelopackAsset b)
    {
        return a.Version == b.Version
            && string.Equals(a.SHA256, b.SHA256, StringComparison.OrdinalIgnoreCase);
    }

    private void MarkReadyIfDownloading()
    {
        lock (_gate)
        {
            if (_phase == UpdateSnapshot.Downloading) // 取消竞态下不再翻转
                SetState(UpdateSnapshot.Ready, progress: 100);
        }
    }

    private void BackToAvailable(string error)
    {
        lock (_gate)
        {
            SetState(UpdateSnapshot.Available, error: error);
        }
    }

    /// <summary>取消下载：仅 downloading 允许；保留可继续使用的旧版。</summary>
    public UpdateSnapshot CancelDownload()
    {
        lock (_gate)
        {
            if (_phase != UpdateSnapshot.Downloading)
                return Snapshot(BusyReason());
            _downloadCts?.Cancel();
            return Snapshot();
        }
    }

    /// <summary>提交安装：仅 ready 允许。闸门拒绝/失败 → 状态回 ready 并带原因；
    /// 更新器交接成功（等本进程退出后整树替换）才进入 applying 并触发计划内关窗；
    /// 交接失败（更新器缺失/无法启动）→ 回 ready 带原因：关窗未排、旧版继续可用，
    /// 前端保留重启升级入口（评审回归：先排关窗再交接会把启动失败变成关窗既成事实）。</summary>
    public async Task<UpdateSnapshot> ApplyAsync()
    {
        lock (_gate)
        {
            if (_phase != UpdateSnapshot.Ready || _pending is null)
                return Snapshot(BusyReason());
            _revision++;
            _phase = UpdateSnapshot.Preparing;
            _error = null;
        }
        if (_prepareGate is not null)
        {
            string? reason;
            try
            {
                reason = await _prepareGate().ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                reason = $"升级准备失败: {ex.Message}";
            }
            if (reason is not null)
            {
                lock (_gate)
                {
                    return SetState(UpdateSnapshot.Ready, error: reason);
                }
            }
        }
        var manager = _manager;
        var pending = _pending;
        if (manager is null || pending is null)
        {
            lock (_gate)
            {
                return SetState(UpdateSnapshot.Ready, error: "未锁定更新源（先检查更新）");
            }
        }
        try
        {
            manager.WaitExitThenApplyUpdates(pending.TargetFullRelease, silent: true, restart: true);
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[update] 更新器交接失败: {ex.Message}（保留旧版，可重试）");
            lock (_gate)
            {
                return SetState(UpdateSnapshot.Ready,
                    error: $"升级程序启动失败: {ex.Message}（旧版本仍可继续使用，可重试）");
            }
        }
        lock (_gate)
        {
            _revision++;
            _phase = UpdateSnapshot.Applying;
        }
        ApplyingStarted?.Invoke(); // 交接成功后才安排计划内关窗
        ShellLog.Append($"[update] 提交已排队：目标 {pending.TargetFullRelease.Version}，等本进程退出后整树替换");
        return GetStatus();
    }

    // ---------- 内部 ----------

    private bool CanRunCheck()
    {
        return _phase is UpdateSnapshot.Idle or UpdateSnapshot.Available
            or UpdateSnapshot.UpToDate or UpdateSnapshot.Ready;
    }

    private string BusyReason() => _phase switch
    {
        UpdateSnapshot.Unavailable => "非受管安装，自动升级不可用",
        UpdateSnapshot.Downloading => "下载进行中",
        UpdateSnapshot.Preparing => "升级准备中",
        UpdateSnapshot.Applying => "已提交等待退出替换",
        _ => $"当前状态 {_phase} 不允许该操作",
    };

    /// <summary>持锁状态下统一的状态翻转出口：revision 只在实质变化时递增。
    /// 目标版本不在此处隐式改写——清空目标由调用方显式置 null（UpToDate/Idle/检查失败）。</summary>
    private UpdateSnapshot SetState(string phase, int? progress = null, string? error = null)
    {
        _phase = phase;
        if (progress.HasValue) _progress = progress.Value;
        _error = error;
        if (phase == UpdateSnapshot.Idle) { _progress = 0; _pending = null; }
        _revision++;
        return Snapshot();
    }

    private UpdateSnapshot Snapshot(string? error = null)
    {
        var effError = error ?? _error;
        return new UpdateSnapshot(_phase, _progress, _targetVersion, effError, _revision, _managed);
    }
}
