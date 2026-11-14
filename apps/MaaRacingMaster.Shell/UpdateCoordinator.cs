using System;
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
/// 宿主更新协调器（升级适配 P2）：应用级 SDK 适配的唯一宿主。
/// 限制为固定来源、完整目标版本、下载任务与安装意图——不建通用插件更新服务，
/// 前端不传任何来源/路径/目标版本组合（P3 界面只发固定意图）。
///
/// 状态机：unavailable（非受管/降级）→ idle →(check)→ available|up_to_date
/// →(download)→ ready →(apply)→ preparing → applying。
/// 提交闸门（P2 契约）：apply 必须先经 prepareGate（后端冻结 + 有界退出确认），
/// 闸门拒绝或异常时状态回 ready 并带原因，解冻由闸门实现负责；全部确认后才
/// 交给更新器（WaitExitThenApplyUpdates：等本进程退出后整树替换并重启）。
/// 来源未配置（P4 接发布索引前）诚实返回不可用，不伪造检查结果。
/// </summary>
public sealed class UpdateCoordinator
{
    /// <summary>提交前准备闸门：true = 后端已冻结且已确认退出（可交更新器）；
    /// false/异常 = 不能提交（实现内部负责解冻）。返回原因经 out 参数带给快照。</summary>
    public delegate Task<string?> PrepareGateAsync();

    private readonly string? _feedSource;
    private readonly IVelopackLocator? _locator;   // 显式定位器注入（合同探针用）；null = 默认定位器
    private readonly PrepareGateAsync? _prepareGate;
    private readonly bool _managed;
    private readonly object _gate = new();

    private long _revision;
    private string _phase = UpdateSnapshot.Idle;
    private int _progress;
    private string? _targetVersion;
    private string? _error;
    private UpdateManager? _manager;
    private UpdateInfo? _pending;
    private CancellationTokenSource? _downloadCts;
    private Task? _downloadTask;

    public UpdateCoordinator(string? feedSource, bool managed,
                             IVelopackLocator? locator = null,
                             PrepareGateAsync? prepareGate = null)
    {
        _feedSource = string.IsNullOrWhiteSpace(feedSource) ? null : feedSource;
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

    /// <summary>检查更新：仅在 idle/available/up_to_date/ready 允许（刷新目标）。</summary>
    public async Task<UpdateSnapshot> CheckAsync()
    {
        lock (_gate)
        {
            if (!CanRunCheck())
                return Snapshot(BusyReason());
        }
        try
        {
            var manager = GetManager();
            if (manager is null)
            {
                lock (_gate) { _targetVersion = null; _pending = null; }
                return SetState(UpdateSnapshot.Idle, error: "更新源未配置");
            }
            var update = await manager.CheckForUpdatesAsync().ConfigureAwait(false);
            lock (_gate)
            {
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
            lock (_gate)
            {
                _targetVersion = null;
                _pending = null;
                return SetState(UpdateSnapshot.Idle, error: $"检查更新失败: {ex.Message}");
            }
        }
    }

    /// <summary>开始下载：仅 available/ready 允许；下载中重复调用复用当前任务（幂等）。</summary>
    public async Task<UpdateSnapshot> StartDownloadAsync()
    {
        Task? running;
        lock (_gate)
        {
            if (_phase == UpdateSnapshot.Downloading && _downloadTask is not null)
                running = _downloadTask; // 复用当前任务
            else if (_phase is UpdateSnapshot.Available or UpdateSnapshot.Ready)
            {
                if (_pending is null)
                    return Snapshot("没有可安装目标，请先检查更新");
                _downloadCts = new CancellationTokenSource();
                _downloadTask = DownloadAsync(_pending, _downloadCts.Token);
                _revision++;
                _phase = UpdateSnapshot.Downloading;
                _error = null;
                running = _downloadTask;
            }
            else
                return Snapshot(BusyReason());
        }
        await running.ConfigureAwait(false);
        return GetStatus();
    }

    private async Task DownloadAsync(UpdateInfo update, CancellationToken ct)
    {
        try
        {
            var manager = GetManager()
                ?? throw new InvalidOperationException("更新源未配置");
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
            lock (_gate)
            {
                if (_phase == UpdateSnapshot.Downloading) // 取消竞态下不再翻转
                    SetState(UpdateSnapshot.Ready, progress: 100);
            }
        }
        catch (OperationCanceledException)
        {
            lock (_gate)
            {
                SetState(UpdateSnapshot.Available, error: "下载已取消");
            }
        }
        catch (Exception ex)
        {
            lock (_gate)
            {
                SetState(UpdateSnapshot.Available, error: $"下载失败: {ex.Message}");
            }
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
    /// 全部确认 → applying（等本进程退出后由更新器整树替换并重启）。</summary>
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
        lock (_gate)
        {
            _revision++;
            _phase = UpdateSnapshot.Applying;
        }
        var manager = GetManager();
        if (manager is null)
        {
            lock (_gate)
            {
                return SetState(UpdateSnapshot.Ready, error: "更新源未配置，无法提交");
            }
        }
        manager.WaitExitThenApplyUpdates(_pending.TargetFullRelease, silent: true, restart: true);
        ShellLog.Append($"[update] 提交已排队：目标 {_pending.TargetFullRelease.Version}，等本进程退出后整树替换");
        return GetStatus();
    }

    // ---------- 内部 ----------

    private UpdateManager? GetManager()
    {
        if (_feedSource is null)
            return null;
        if (_manager is not null)
            return _manager;
        _manager = _locator is null
            ? new UpdateManager(_feedSource)
            : new UpdateManager(_feedSource, new UpdateOptions(), _locator);
        return _manager;
    }

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
