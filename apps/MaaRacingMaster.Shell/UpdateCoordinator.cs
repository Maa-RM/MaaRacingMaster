using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;

namespace MaaRacingMaster.Shell;

/// <summary>宿主更新协调器的对外快照（响应形状，snake_case 与业务侧一致）。
/// revision 单调递增：只在状态实质变化时 +1，前端据此丢弃过期轮询响应。</summary>
public sealed record UpdateSnapshot(
    [property: JsonPropertyName("phase")] string Phase,
    [property: JsonPropertyName("progress")] int Progress,
    [property: JsonPropertyName("target_version")] string? TargetVersion,
    [property: JsonPropertyName("error")] string? Error,
    [property: JsonPropertyName("revision")] long Revision,
    [property: JsonPropertyName("managed")] bool Managed,
    [property: JsonPropertyName("channel")] string Channel,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("awaiting_stable")] bool AwaitingStable,
    [property: JsonPropertyName("dismissed_version")] string? DismissedVersion,
    [property: JsonPropertyName("auto_check")] bool AutoCheck)
{
    public const string ChannelStable = "stable";
    public const string ChannelPreview = "preview";
    public const string SourceCnb = "cnb";
    public const string SourceGithub = "github";

    public const string Unavailable = "unavailable";  // 当前运行方式不支持自动升级（手动下载口径）
    public const string Idle = "idle";
    public const string Available = "available";      // 已发现可安装目标，未下载
    public const string UpToDate = "up_to_date";      // 当前渠道没有更高版本
    public const string Downloading = "downloading";
    public const string Ready = "ready";              // 已下载完成，可提交
    public const string Preparing = "preparing";      // 冻结后端 + 退出确认中
    public const string Applying = "applying";        // 已交给更新器：等本进程退出后替换
}

/// <summary>检查发起方：手动检查允许在 ready（已下载）时刷新目标；后台自动检查
/// 只在 idle/available/up_to_date 允许——不得撤掉玩家已下载好的升级入口。</summary>
public enum UpdateCheckTrigger
{
    Manual,
    Auto,
}

/// <summary>
/// 宿主更新协调器：应用级 SDK 适配的唯一宿主。
/// 限制为固定来源、完整目标版本、下载任务与安装意图——不建通用插件更新服务，
/// 前端只发固定意图，不传任何来源/路径/目标版本组合。
///
/// 状态机：unavailable（当前运行方式不支持自动升级）→ idle →(check)→ available|up_to_date
/// →(download)→ ready →(apply)→ preparing → applying。
/// 提交闸门：apply 必须先经 prepareGate（后端冻结 + 有界退出确认），
/// 闸门拒绝或异常时状态回 ready 并带原因，解冻由闸门实现负责；全部确认后才
/// 交给更新器（WaitExitThenApplyUpdates：等本进程退出后整树替换并重启）。
/// 来源未配置时诚实返回不可用，不伪造检查结果。
///
/// 语义基线（与计划文档一致）：
/// · 两个下载地址指向同一发行货架，内容与版本应当一致；名字只决定首选，
///   首选不可达回退其余地址，换源重下受「同目标（版本+SHA256）」约束。
/// · 渠道是用户设置，不是包属性：始终显式传给更新器，默认稳定轨；不做降级
///   安装，从测试轨切回后等稳定轨版本追上再升级（数据只升不降）。
/// · 切渠道换的是目标发行，清空在途与就绪；切下载源只换首选地址，
///   在途下载与就绪包保留，但修订号前进（前端据此丢弃旧点击）。
///
/// 三条不变量（各自只有一个出口，避免语义散落在字符串比较里）：
/// ① CheckIntent：一次「检查→下载→提交」的不可变事务上下文，整条异步链只读它；
/// ② Epoch：任何渠道切换开启新世代；所有异步回写（检查/下载/镜像回退/进度）
///    都携带发起时的世代，经 SetState(epoch) 唯一出口校验，迟到回写一律丢弃；
/// ③ CheckRun：任意时刻最多一个检查事务在跑（_checkGate），同世代的手动/自动
///    请求合并在途结果；切渠道取消旧索引并等它真正退出后，才放行新渠道检查。
/// </summary>
public sealed class UpdateCoordinator
{
    /// <summary>提交前准备闸门：true = 后端已冻结且已确认退出（可交更新器）；
    /// false/异常 = 不能提交（实现内部负责解冻）。返回原因经 out 参数带给快照。</summary>
    public delegate Task<string?> PrepareGateAsync();

    /// <summary>交接失败恢复委托：闸门通过 = 后端已确认退出；更新器交接失败后由实现方
    /// 重启后端，返回 true = 旧版本已恢复可用（错误提示据此措辞，不谎称可继续使用）。</summary>
    public delegate Task<bool> HandoffRecoveryAsync();

    /// <summary>提交进入 applying 相位（等本进程退出后整树替换）时触发一次：
    /// 壳订阅它做「计划内关闭」（窗口收尾 → Update.exe 检测退出 → 替换 → 重启）。</summary>
    public event Action? ApplyingStarted;

    /// <summary>自动检查提交了成功结果（available / up_to_date）：壳据此向页面推送快照，
    /// 让关于标签的提醒点在用户没打开关于页时也能亮起。参数是提交后的有效快照。</summary>
    public event Action<UpdateSnapshot>? AutoCheckCommitted;

    /// <summary>自动检查开关变化：进程内调度器据此重算等待（关闭即停等，重新启用以
    /// 「本进程最近一次检查收尾」为锚点重排；本进程还没检查过则立即安排）。</summary>
    public event Action? AutoCheckSettingChanged;

    /// <summary>一次检查的不可变事务上下文。检查开始时从当前状态快照出来，
    /// 检查、下载、镜像回退全程只读它——切换设置不会让半程事务混用两套渠道/来源。
    /// 渠道是用户设置，总是显式传入；安装包烧录的渠道在此模型下不被读取。</summary>
    private sealed record CheckIntent(
        long Epoch,
        string StartPhase,
        string Channel,
        UpdateSourceEndpoint[] Endpoints)
    {
        public bool IsStale(string phase, long epoch) => epoch != Epoch || phase != StartPhase;
    }

    /// <summary>一次检查事务的运行态：完成信号 + 取消源 + 发起方。
    /// 完成信号在锁外 TrySetResult，避免把续体调度进状态锁。</summary>
    private sealed record CheckRun(
        CheckIntent Intent,
        UpdateCheckTrigger Trigger,
        CancellationTokenSource Cts,
        TaskCompletionSource<UpdateSnapshot> Completion);

    private readonly string? _feedSource;          // 随包来源配置原文（按命名端点解析）
    private readonly bool _portable;               // 便携安装：清单协议 + 启动器事务（唯一受支持形态）
    private readonly string? _installRoot;         // 便携安装根（program\ 的父目录）
    private readonly string _currentVersion;       // 本次运行的版本（渠道比较用）
    private readonly bool _snapshotPortable;       // 快照 managed = 自动升级可用（便携安装）
    private readonly PrepareGateAsync? _prepareGate;
    private readonly HandoffRecoveryAsync? _handoffRecovery;
    private readonly Action<string, string, bool> _savePreferences; // 偏好落盘（channel, source, awaitingStable）
    private readonly Action<string?> _saveDismissedVersion;         // 「本次更新不再提示」落盘
    private readonly Action<bool> _saveAutoCheck;                   // 后台自动检查开关落盘
    private readonly TimeProvider _time;                            // 调度/修订计时（探针可注入）
    private readonly TimeSpan _indexTimeout;                        // 单镜像索引预算（含小写重试）
    private readonly object _gate = new();
    private readonly SemaphoreSlim _checkGate = new(1, 1);          // 检查事务串行化（旧退出→新放行）

    private string _channel = UpdateSnapshot.ChannelStable;   // 渠道是用户设置，默认显式稳定轨
    private string _source = UpdateSnapshot.SourceCnb;        // 当前首选下载地址
    private bool _awaitingStable;                             // 从测试轨切回且稳定版尚未追上
    private string? _dismissedVersion;                        // 已标记「本次更新不再提示」的目标版本
    private bool _autoCheckEnabled;                           // 后台自动检查开关（随 prefernces 持久化）

    private long _revision;
    private long _epoch;                           // 渠道切换开启新世代：作废此前所有在途异步回写
    private string _phase = UpdateSnapshot.Idle;
    private int _progress;
    private string? _targetVersion;
    private string? _error;
    private CheckIntent? _activeIntent;            // 当前已验证目标所属的事务上下文
    private UpdateRelease? _release;               // 已验证的发布清单
    private UpdateSourceEndpoint? _endpoint;       // 便携模式：锁定的来源端点
    private StagedUpdate? _staged;                 // 便携模式：已暂存完成的候选树
    private CancellationTokenSource? _downloadCts;
    private Task? _downloadTask;
    private int _drainCount;                       // 切渠道后旧下载的收尾（未完成前不许开新下载，可叠加）

    private Task<UpdateSnapshot>? _checkTask;      // 在途检查（同世代请求合并）
    private long _checkTaskEpoch = -1;
    private CancellationTokenSource? _checkCts;    // 当前检查的取消源（切渠道/关窗/开始下载）
    private DateTimeOffset? _lastCheckCompletedUtc; // 本进程最近一次检查真正收尾（不含忙态拒绝）
    private DateTimeOffset? _autoAnchorUtc;         // 自动检查计时起点（收尾 或 忙态跳过）

    public UpdateCoordinator(string? feedSource,
                             PrepareGateAsync? prepareGate = null,
                             HandoffRecoveryAsync? handoffRecovery = null,
                             string? initialChannel = null,
                             string? initialSource = null,
                             bool initialAwaitingStable = false,
                             string? initialDismissedVersion = null,
                             Action<string, string, bool>? savePreferences = null,
                             Action<string?>? saveDismissedVersion = null,
                             bool initialAutoCheck = true,
                             Action<bool>? saveAutoCheck = null,
                             TimeProvider? timeProvider = null,
                             TimeSpan? indexTimeout = null,
                             string? installRoot = null,
                             string? currentVersion = null)
    {
        _feedSource = feedSource;
        _portable = !string.IsNullOrWhiteSpace(installRoot) && Directory.Exists(installRoot);
        _installRoot = _portable ? installRoot : null;
        _currentVersion = string.IsNullOrWhiteSpace(currentVersion) ? "0.0.0" : currentVersion;
        _snapshotPortable = _portable;
        _prepareGate = prepareGate;
        _handoffRecovery = handoffRecovery;
        _savePreferences = savePreferences ?? ((_, _, _) => { });
        _saveDismissedVersion = saveDismissedVersion ?? (_ => { });
        _saveAutoCheck = saveAutoCheck ?? (_ => { });
        _autoCheckEnabled = initialAutoCheck;
        _time = timeProvider ?? TimeProvider.System;
        _indexTimeout = indexTimeout ?? TimeSpan.FromSeconds(60);
        if (initialChannel == UpdateSnapshot.ChannelPreview) _channel = UpdateSnapshot.ChannelPreview;
        if (initialSource == UpdateSnapshot.SourceGithub) _source = UpdateSnapshot.SourceGithub;
        _awaitingStable = initialAwaitingStable && _channel == UpdateSnapshot.ChannelStable;
        _dismissedVersion = string.IsNullOrWhiteSpace(initialDismissedVersion)
            ? null : initialDismissedVersion.Trim();
        if (!_portable)
        {
            _phase = UpdateSnapshot.Unavailable;
            _error = "当前运行方式不支持自动升级（请使用手动下载入口）";
        }
    }

    public UpdateSnapshot GetStatus()
    {
        lock (_gate)
        {
            return Snapshot();
        }
    }

    /// <summary>本进程最近一次检查收尾（成功或失败）；调度器重新启用自动检查时以它为锚点，
    /// 为空表示本进程还没检查过 → 重新启用后立即安排。</summary>
    public DateTimeOffset? LastCheckCompletedUtc
    {
        get { lock (_gate) return _lastCheckCompletedUtc; }
    }

    /// <summary>距下一次自动检查的等待时长：null = 当前不该检查（关闭自动检查 / 当前运行方式不支持），
    /// Zero = 立即。计时锚点取最近一次检查收尾与最近一次忙态跳过中较晚者。</summary>
    public TimeSpan? NextAutoCheckDelay(DateTimeOffset now, TimeSpan interval)
    {
        lock (_gate)
        {
            if (!_portable || !_autoCheckEnabled) return null;
            var anchor = _autoAnchorUtc;
            if (anchor is null) return TimeSpan.Zero;
            var delay = anchor.Value + interval - now;
            return delay > TimeSpan.Zero ? delay : TimeSpan.Zero;
        }
    }

    /// <summary>取消在途检查（窗口关闭 / 开始下载 / 调度器停机）：索引请求随令牌中止，
    /// 不再把一次检查挂到网络超时；状态不变，由调用方决定后续相位。</summary>
    public void CancelPendingCheck()
    {
        lock (_gate)
        {
            _checkCts?.Cancel();
        }
    }

    /// <summary>检查更新：手动检查仅在 idle/available/up_to_date/ready 允许（刷新目标），
    /// 自动检查不允许 ready（不撤掉已下载的升级入口）。同世代已有在途检查时直接合并且
    /// 复用其结果——手动点击不会给同一轮再发一次索引请求。
    /// 来源按「首选在前」的顺序尝试：第一个「可达且给出结论」的地址胜出并锁定
    /// （下载/提交绑定同一已验证来源）；首选不可达时回退其余等价地址。全部
    /// 不可达/失败时诚实报错并可重试——不换目标，不把空/坏镜像误报为「已是最新」。
    /// 检查在途期间状态可能被下载/提交推进（网络慢源是常态）：返回时校验发起时
    /// 的相位与世代，不一致即丢弃结果并返回当前快照。</summary>
    public Task<UpdateSnapshot> CheckAsync(UpdateCheckTrigger trigger = UpdateCheckTrigger.Manual)
    {
        Task<UpdateSnapshot>? shared = null;
        CheckRun? run = null;
        lock (_gate)
        {
            if (_checkTask is { IsCompleted: false } inFlight && _checkTaskEpoch == _epoch)
            {
                shared = inFlight; // 同世代在途：手动/自动共用这一轮
            }
            else if (!CanRunCheck(trigger))
            {
                if (trigger == UpdateCheckTrigger.Auto) _autoAnchorUtc = _time.GetUtcNow();
                return Task.FromResult(Snapshot(BusyReason()));
            }
            else
            {
                var endpoints = UpdateSourceCatalog.Parse(_feedSource, _source).ToArray();
                if (endpoints.Length == 0)
                {
                    _targetVersion = null;
                    _activeIntent = null;
                    var empty = SetState(_epoch, UpdateSnapshot.Idle, error: "更新源未配置（或所选源无可用地址）");
                    NoteCheckSettled(_time.GetUtcNow());
                    return Task.FromResult(empty);
                }
                var intent = new CheckIntent(_epoch, _phase, _channel, endpoints);
                var cts = new CancellationTokenSource();
                var completion = new TaskCompletionSource<UpdateSnapshot>(
                    TaskCreationOptions.RunContinuationsAsynchronously);
                _checkCts = cts;
                _checkTask = completion.Task;
                _checkTaskEpoch = _epoch;
                run = new CheckRun(intent, trigger, cts, completion);
            }
        }
        if (shared is not null) return shared;
        _ = RunCheckAsync(run!); // 锁外启动：RunCheckAsync 的同步前段会拿状态锁
        return run!.Completion.Task;
    }

    /// <summary>开始下载：仅 available 允许；下载中重复调用复用当前任务（幂等）；
    /// ready 重复调用幂等返回（已就绪无需重下）。启动即回包 downloading 快照——
    /// 下载在后台任务进行，进度与结果由独立状态查询获得（评审回归：等待整段下载
    /// 会让首次下载期间界面拿不到进度、也无法取消）。事务上下文随任务传递：
    /// 镜像回退只用检查时锁定的那套渠道与来源，不重新读取可能已被切换的偏好。
    /// expectedRevision 是玩家看到目标时的修订号：界面快照已过期（后台检查换掉了目标）
    /// 就拒绝，让玩家对新目标重新确认，而不是替旧界面下载新版本。</summary>
    public Task<UpdateSnapshot> StartDownloadAsync(long? expectedRevision = null)
    {
        UpdateRelease? release = null;
        UpdateSourceEndpoint? endpoint = null;
        CheckIntent intent;
        CancellationTokenSource cts;
        lock (_gate)
        {
            if (expectedRevision is long seen && seen != _revision)
                return Task.FromResult(Snapshot("界面信息已更新，请确认当前目标后重新操作"));
            // 旧下载还没收尾：它和下一次下载写同一个暂存目录与缓存，必须先等它退出。
            if (_drainCount > 0)
                return Task.FromResult(Snapshot("正在切换更新渠道，请稍候"));
            if (_phase == UpdateSnapshot.Downloading && _downloadTask is not null)
                return Task.FromResult(Snapshot()); // 复用当前任务
            if (_phase == UpdateSnapshot.Ready)
                return Task.FromResult(Snapshot()); // 已下载完成：重复调用幂等返回
            if (_phase != UpdateSnapshot.Available)
                return Task.FromResult(Snapshot(BusyReason()));
            if (_activeIntent is null)
                return Task.FromResult(Snapshot("没有可安装目标，请先检查更新"));
            release = _release;
            endpoint = _endpoint;
            if (release is null || endpoint is null)
                return Task.FromResult(Snapshot("没有可安装目标，请先检查更新"));
            _checkCts?.Cancel(); // 下载事务接管：在途检查的迟到结果没有回写资格
            intent = _activeIntent;
            cts = new CancellationTokenSource();
            _downloadCts = cts;
            _revision++;
            _phase = UpdateSnapshot.Downloading;
            _error = null;
            // 任务在锁外创建：下载的同步前段与完成翻转都拿同一把锁，
            // 持锁启动会在同步完成路径上自我死锁。
        }
        _downloadTask = Task.Run(() => DownloadPortableAsync(release!, endpoint!, intent, cts.Token));
        return Task.FromResult(GetStatus());
    }

    /// <summary>
    /// 便携安装的下载：把清单落成完整候选树（增量优先、失败回退整包，细节在
    /// <see cref="UpdateStager"/>）。失败时按事务上下文里的其它来源重建同一目标
    /// （版本与整包摘要都一致才换源），全部失败回可重试的 available。
    /// </summary>
    private async Task DownloadPortableAsync(UpdateRelease release, UpdateSourceEndpoint endpoint,
                                             CheckIntent intent, CancellationToken ct)
    {
        try
        {
            var staged = await StagePortableAsync(release, endpoint, intent, ct).ConfigureAwait(false);
            lock (_gate)
            {
                if (intent.Epoch == _epoch && _phase == UpdateSnapshot.Downloading)
                {
                    _staged = staged;
                    SetState(intent.Epoch, UpdateSnapshot.Ready, progress: 100);
                }
            }
        }
        catch (OperationCanceledException)
        {
            BackToAvailable(intent, "下载已取消");
        }
        catch (Exception ex)
        {
            BackToAvailable(intent, "下载失败: " + ex.Message);
        }
    }

    private async Task<StagedUpdate> StagePortableAsync(UpdateRelease release,
                                                        UpdateSourceEndpoint endpoint,
                                                        CheckIntent intent, CancellationToken ct)
    {
        var installRoot = _installRoot ?? throw new InvalidOperationException("缺少安装根");
        var contentRoot = Path.Combine(installRoot, "program");
        var stageRoot = UpdateLauncher.StagingPath(installRoot);
        var cacheDir = Path.Combine(UpdateLauncher.UpdateDir(installRoot), "cache");
        var stager = new UpdateStager(TransportForAssets(endpoint.Assets), cacheDir);
        var progress = new SyncProgress(value =>
        {
            lock (_gate)
            {
                if (intent.Epoch == _epoch && _phase == UpdateSnapshot.Downloading)
                {
                    _progress = value;
                    _revision++;
                }
            }
        });

        Exception? firstFailure = null;
        try
        {
            return await stager
                .StageAsync(release, endpoint.Assets, _currentVersion, contentRoot, stageRoot,
                            progress, ct)
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            firstFailure = ex;
        }

        // 镜像回退：只换地址，不换发行——按已选定的 tag/版本取清单，版本与整包摘要
        // 都一致才采用另一来源的资产。不得重读备用源的「最新标记」：它一旦发了下一版，
        // 就会错过仍可下载的旧目标。
        foreach (var candidate in intent.Endpoints)
        {
            if (candidate.Assets.Equals(endpoint.Assets) || !EndpointReachable(candidate))
                continue;
            try
            {
                var client = new UpdateSourceClient(TransportForEndpoint(candidate));
                var mirror = await client
                    .FetchManifestAtAsync(candidate, release.Tag, release.Version, ct)
                    .ConfigureAwait(false);
                if (!string.Equals(mirror.Version, release.Version, StringComparison.Ordinal)
                    || !string.Equals(mirror.Full.Sha256, release.Full.Sha256,
                        StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                var mirrorStager = new UpdateStager(TransportForAssets(candidate.Assets), cacheDir);
                return await mirrorStager
                    .StageAsync(mirror, candidate.Assets, _currentVersion, contentRoot, stageRoot,
                                progress, ct)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch
            {
                // 该镜像不可用：继续下一候选
            }
        }
        throw new InvalidOperationException(
            $"{firstFailure?.Message}（镜像回退未找到同目标源）");
    }

    /// <summary>同步进度回调（不投递到同步上下文，避免下载结束后才到达）。</summary>
    private sealed class SyncProgress : IProgress<int>
    {
        private readonly Action<int> _report;
        public SyncProgress(Action<int> report) => _report = report;
        public void Report(int value) => _report(value);
    }

    /// <summary>等一个旧任务完全退出；它的异常与终态由该任务自己写回的快照承担。</summary>
    private static async Task DrainAsync(Task? task)
    {
        if (task is null || task.IsCompleted) return;
        try { await task.ConfigureAwait(false); }
        catch { /* 旧任务失败不阻断新渠道，状态已在它自己的返回里落定 */ }
    }

    private void BackToAvailable(CheckIntent intent, string error)
    {
        lock (_gate)
        {
            // 渠道切换会开启新世代：旧下载任务取消后的这次回写必须丢弃，
            // 否则它会把 idle 改成 available，让切换后的新检查被相位守卫误判为迟到。
            if (intent.Epoch == _epoch)
                SetState(intent.Epoch, UpdateSnapshot.Available, error: error);
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

    /// <summary>切换渠道/下载源（用户在设置浮窗选）。非法值直接拒绝、状态不变。
    /// 渠道换的是目标发行：清空在途下载与就绪包、开启新世代、落盘偏好，取消旧渠道
    /// 索引并等旧检查事务真正退出后再查新渠道（不让两轮请求并发，也不让旧结果落在
    /// 新渠道上）；从测试轨切回时标记「等待稳定轨追上」——不做降级安装，稳定版版本
    /// 高于当前测试版后自然提示可升级。下载源只是同一货架的地址选择：保留在途下载
    /// 与就绪包，仅换首选顺序并前进修订号，下次检查与镜像回退自然采用。提交临界区
    /// （preparing/applying）不允许切换：那时窗口已在关闭链上。</summary>
    public Task<UpdateSnapshot> SetChannelAsync(string? channel) =>
        SwitchSettingAsync(value: channel == UpdateSnapshot.ChannelPreview
                ? UpdateSnapshot.ChannelPreview
                : channel == UpdateSnapshot.ChannelStable ? UpdateSnapshot.ChannelStable : null,
            isChannel: true);

    public Task<UpdateSnapshot> SetSourceAsync(string? source) =>
        SwitchSettingAsync(value: source == UpdateSnapshot.SourceGithub
                ? UpdateSnapshot.SourceGithub
                : source == UpdateSnapshot.SourceCnb ? UpdateSnapshot.SourceCnb : null,
            isChannel: false);

    private async Task<UpdateSnapshot> SwitchSettingAsync(string? value, bool isChannel)
    {
        if (value is null)
        {
            lock (_gate) return Snapshot($"非法{(isChannel ? "渠道" : "下载源")}值");
        }
        bool changed;
        long epoch;
        Task<UpdateSnapshot>? draining = null;
        Task? drainingDownload = null;
        var countingDrain = false;
        lock (_gate)
        {
            if (_phase is UpdateSnapshot.Preparing or UpdateSnapshot.Applying)
                return Snapshot("升级提交中，不能切换设置");
            if (isChannel)
            {
                changed = value != _channel;
                if (changed)
                {
                    draining = _checkTask; // ResetForNewEpoch 会清空在途引用，先留一份等它退出
                    drainingDownload = _downloadTask;
                    // 计数与重置同锁：重置一把状态放成 Idle，下载闸门就必须已经生效，
                    // 否则这里释放锁到下一行加计数之间存在并发窗口。
                    _drainCount++;
                    countingDrain = true;
                    _awaitingStable = value == UpdateSnapshot.ChannelStable
                        && _channel == UpdateSnapshot.ChannelPreview;
                    _channel = value;
                    ResetForNewEpoch();
                }
            }
            else
            {
                changed = value != _source;
                _source = value;
                if (changed) _revision++; // 首选地址变化 = 可观察快照变化（旧点击据此失效）
            }
            epoch = _epoch;
        }
        if (!changed) return GetStatus();
        // 先等旧下载真正退出，再做偏好落盘与后续检查：等待闸门已随重置一起生效。
        if (countingDrain)
        {
            try
            {
                await DrainAsync(drainingDownload).ConfigureAwait(false);
            }
            finally
            {
                lock (_gate) { _drainCount--; }
            }
        }
        _savePreferences(_channel, _source, _awaitingStable);
        if (!isChannel) return GetStatus();
        if (draining is { IsCompleted: false })
        {
            try { await draining.ConfigureAwait(false); }
            catch { /* 旧事务的失败/取消由它自己的返回快照承担 */ }
        }
        lock (_gate)
        {
            // 排水期间可能又切了一次渠道（续体不得为旧意图启动检查）：谁最后改谁负责。
            if (_epoch != epoch || _channel != value) return Snapshot();
        }
        return await CheckAsync().ConfigureAwait(false);
    }

    /// <summary>更新提醒：记住「本次更新不再提示」的目标版本。只影响关于标签上的
    /// 提醒点，不改变更新本身的可下载状态；目标版本变化后自然恢复提醒。
    /// 没有可安装目标时拒绝，避免写入空标记。expectedRevision 同下载：界面快照
    /// 过期就拒绝，不把「旧界面看到的版本」标成已忽略。</summary>
    public UpdateSnapshot SetDismissed(bool dismissed, long? expectedRevision = null)
    {
        string? value;
        lock (_gate)
        {
            if (expectedRevision is long seen && seen != _revision)
                return Snapshot("界面信息已更新，请确认当前目标后重新操作");
            if (dismissed && string.IsNullOrEmpty(_targetVersion))
                return Snapshot("没有可忽略的更新目标");
            value = dismissed ? _targetVersion : null;
            if (value == _dismissedVersion) return Snapshot(); // 幂等：不产生无意义修订
            _dismissedVersion = value;
            _revision++;
        }
        _saveDismissedVersion(value); // 锁外落盘：文件 IO 不占状态锁
        return GetStatus();
    }

    /// <summary>后台自动检查开关：只改偏好与快照，不触发立即检查（调度器按新开关重算；
    /// 重新启用时以本进程最近一次检查收尾为锚点——还没检查过或锚点已过期就立即安排，
    /// 否则等剩余时间）。关闭后手动检查与切渠道不受影响。</summary>
    public UpdateSnapshot SetAutoCheck(bool enabled)
    {
        UpdateSnapshot snapshot;
        lock (_gate)
        {
            if (_autoCheckEnabled == enabled) return Snapshot(); // 幂等：不产生无意义修订
            _autoCheckEnabled = enabled;
            _autoAnchorUtc = enabled ? _lastCheckCompletedUtc : _autoAnchorUtc;
            _revision++;
            snapshot = Snapshot();
        }
        _saveAutoCheck(enabled); // 锁外落盘
        try { AutoCheckSettingChanged?.Invoke(); }
        catch (Exception ex) { ShellLog.Append($"[update] 自动检查开关回调异常: {ex.Message}"); }
        return snapshot;
    }

    /// <summary>开启新世代并清空一切在途状态。持锁调用；这是唯一绕过 SetState 的状态
    /// 翻转——因为它主动作废旧世代，而不是在旧世代内推进相位。
    /// 同时在途检查被取消（旧渠道的索引请求不再等待），其迟到回写因世代不符被丢弃。
    /// 非便携运行（源码 / 解压即用 ZIP）没有自动升级能力：重置后保持 Unavailable 与其
    /// 原因文案，不让切渠道/切源把一个诚实的「不支持」刷成 Idle+「更新源未配置」。</summary>
    private void ResetForNewEpoch()
    {
        _downloadCts?.Cancel();
        _downloadCts = null;
        _downloadTask = null;
        _activeIntent = null;
        _release = null;
        _endpoint = null;
        _staged = null;
        _targetVersion = null;
        _checkCts?.Cancel();
        _checkCts = null;
        _checkTask = null;
        _checkTaskEpoch = -1;
        _epoch++;
        _phase = _portable ? UpdateSnapshot.Idle : UpdateSnapshot.Unavailable;
        _progress = 0;
        if (_portable) _error = null;
        _revision++;
    }

    /// <summary>提交安装：仅 ready 允许。闸门拒绝/失败 → 状态回 ready 并带原因；
    /// 更新器交接成功（等本进程退出后整树替换）才进入 applying 并触发计划内关窗；
    /// 交接失败（更新器缺失/无法启动）→ 走恢复委托重启后端（闸门通过 = 后端已确认
    /// 退出，不恢复则「旧版可继续使用」是假话），再回 ready 带原因：关窗未排，
    /// 前端保留重启升级入口（评审回归：先排关窗再交接会把启动失败变成关窗既成事实）。</summary>
    public async Task<UpdateSnapshot> ApplyAsync(bool backupExtras = false)
    {
        lock (_gate)
        {
            if (_phase != UpdateSnapshot.Ready)
                return Snapshot(BusyReason());
            _checkCts?.Cancel(); // ready 允许手动复查：提交时在途结果同样失效
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
                    return SetState(_epoch, UpdateSnapshot.Ready, error: reason);
                }
            }
        }
        return await ApplyPortableAsync(backupExtras).ConfigureAwait(false);
    }

    /// <summary>
    /// 便携安装的提交：事务文件与替换都由根启动器执行。交接成功才进入 applying 并
    /// 安排计划内关窗；交接失败（暂存缺失/启动器不可用）走恢复委托把后端拉起来，
    /// 状态回 ready 带原因——关窗未排，玩家仍可重试。
    /// </summary>
    private async Task<UpdateSnapshot> ApplyPortableAsync(bool backupExtras)
    {
        var targetVersion = _release?.Version;
        if (_staged is null || _release is null || string.IsNullOrEmpty(targetVersion))
        {
            lock (_gate)
            {
                return SetState(_epoch, UpdateSnapshot.Ready,
                    error: "没有已下载的更新，请重新检查后再试");
            }
        }
        if (backupExtras)
        {
            try
            {
                BackupLocalChanges(_release);
            }
            catch (Exception ex)
            {
                // prepareGate 已让后端退出：只回 Ready 会把玩家留在「旧窗口能用、业务不可用」
                // 的状态，必须复用交接失败的恢复流程把后端拉回来。
                var recovered = false;
                if (_handoffRecovery is not null)
                {
                    try
                    {
                        recovered = await _handoffRecovery().ConfigureAwait(false);
                    }
                    catch (Exception recEx)
                    {
                        ShellLog.Append($"[update] 备份失败后的后端恢复异常: {recEx.Message}");
                    }
                }
                ShellLog.Append($"[update] 备份本地文件失败: {ex.Message}（后端恢复: {(recovered ? "已重启" : "失败，需重启应用")}）");
                lock (_gate)
                {
                    return SetState(_epoch, UpdateSnapshot.Ready,
                        error: recovered
                            ? $"备份本地文件失败，已取消安装: {ex.Message}（旧版本可继续使用，可重试）"
                            : $"备份本地文件失败，已取消安装: {ex.Message}（后端恢复失败，请重启应用）");
                }
            }
        }
        var source = _endpoint?.Name ?? _source;
        if (!UpdateLauncher.TryStartApply(targetVersion, Environment.ProcessId,
                _staged.Mode, _staged.DownloadBytes, source, _release.Notes ?? "",
                UpdateProtocol.Serialize(_release), out var error, _installRoot))
        {
            var recovered = false;
            if (_handoffRecovery is not null)
            {
                try
                {
                    recovered = await _handoffRecovery().ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    ShellLog.Append($"[update] 交接失败恢复异常: {ex.Message}");
                }
            }
            ShellLog.Append($"[update] 安装事务启动失败: {error}（后端恢复: {(recovered ? "已重启" : "失败，需重启应用")}）");
            lock (_gate)
            {
                return SetState(_epoch, UpdateSnapshot.Ready,
                    error: recovered
                        ? $"升级程序启动失败: {error}（旧版本可继续使用，可重试升级）"
                        : $"升级程序启动失败: {error}（自动恢复失败，请重启应用后再试）");
            }
        }
        lock (_gate)
        {
            _revision++;
            _phase = UpdateSnapshot.Applying;
        }
        ApplyingStarted?.Invoke(); // 交接成功后才安排计划内关窗
        ShellLog.Append($"[update] 提交已交接：目标 {targetVersion}，等本进程退出后替换程序目录");
        return GetStatus();
    }

    /// <summary>
    /// 安装前的本地差异：程序目录里多出来的文件，以及相对「已装清单」被玩家改过的
    /// 官方文件。首版安装没有基线，只能判出额外文件（BaselineKnown=false）。
    /// </summary>
    public sealed record LocalChangeReport(
        [property: JsonPropertyName("extra_files")] int ExtraFiles,
        [property: JsonPropertyName("extra_bytes")] long ExtraBytes,
        [property: JsonPropertyName("modified_files")] int ModifiedFiles,
        [property: JsonPropertyName("baseline_known")] bool BaselineKnown,
        [property: JsonPropertyName("sample")] IReadOnlyList<string> Sample,
        [property: JsonPropertyName("backup_path")] string BackupPath);

    /// <summary>扫描本地树（约一次全量哈希）；没有可安装目标时返回 null。</summary>
    public Task<LocalChangeReport?> InspectLocalChangesAsync() => Task.Run(InspectLocalChanges);

    private LocalChangeReport? InspectLocalChanges()
    {
        var release = _release;
        if (!_portable || release is null || _installRoot is null) return null;
        var contentRoot = Path.Combine(_installRoot, "program");
        if (!Directory.Exists(contentRoot)) return null;

        var target = release.Full.Files.Select(f => f.Path)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var disk = UpdateTree.Scan(contentRoot);
        var extras = disk.Keys.Where(path => !target.Contains(path))
            .OrderBy(path => path, UpdateProtocol.Utf8PathComparer.Instance).ToList();
        var (modified, baseline) = ModifiedAgainstInstalled(target, disk);
        long extraBytes = extras.Sum(path => disk[path].Size);
        var sample = extras.Concat(modified)
            .OrderBy(path => path, UpdateProtocol.Utf8PathComparer.Instance)
            .Take(5).ToList();
        return new LocalChangeReport(extras.Count, extraBytes, modified.Count,
            baseline is not null, sample, BackupRoot());
    }

    private (List<string> Modified, UpdateFull? Baseline) ModifiedAgainstInstalled(
        HashSet<string> target, IReadOnlyDictionary<string, UpdateFileEntry> disk)
    {
        var baseline = LoadInstalledManifest();
        var modified = new List<string>();
        if (baseline is null) return (modified, null);
        var installed = baseline.Files.ToDictionary(f => f.Path, StringComparer.OrdinalIgnoreCase);
        foreach (var path in target)
        {
            if (!installed.TryGetValue(path, out var expected)) continue;
            if (!disk.TryGetValue(path, out var actual)) continue;
            if (!string.Equals(actual.Sha256, expected.Sha256, StringComparison.OrdinalIgnoreCase))
                modified.Add(path);
        }
        modified.Sort(UpdateProtocol.Utf8PathComparer.Instance);
        return (modified, baseline);
    }

    private UpdateFull? LoadInstalledManifest()
    {
        if (_installRoot is null) return null;
        var path = UpdateLauncher.InstalledManifestPath(_installRoot);
        if (!File.Exists(path)) return null;
        try
        {
            return UpdateProtocol.Parse(File.ReadAllText(path)).Full;
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[update] 已装清单不可读，只能按额外文件判断差异: {ex.Message}");
            return null;
        }
    }

    private string BackupRoot()
    {
        var appData = Environment.GetEnvironmentVariable("APPDATA");
        if (!string.IsNullOrWhiteSpace(appData))
            return Path.Combine(appData, "MaaRacingMaster", "data", "_update_backup", _currentVersion);
        return Path.Combine(UpdateLauncher.UpdateDir(_installRoot!), "backup-files", _currentVersion);
    }

    /// <summary>把额外文件与被改过的官方文件按原相对路径复制到备份目录；失败即中止安装。</summary>
    private void BackupLocalChanges(UpdateRelease release)
    {
        if (_installRoot is null) return;
        var contentRoot = Path.Combine(_installRoot, "program");
        var target = release.Full.Files.Select(f => f.Path)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var disk = UpdateTree.Scan(contentRoot);
        var extras = disk.Keys.Where(path => !target.Contains(path)).ToList();
        var (modified, baseline) = ModifiedAgainstInstalled(target, disk);
        // 没有基线就分不清「被改过的官方文件」：既然玩家选了备份，就复制整个程序
        // 目录，别让他以为备份过的东西一定在里面。
        var toCopy = baseline is null
            ? disk.Keys.ToList()
            : extras.Concat(modified).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        var backupRoot = BackupRoot();
        var count = 0;
        foreach (var rel in toCopy)
        {
            var sourcePath = Path.Combine(contentRoot, rel.Replace('/', Path.DirectorySeparatorChar));
            var destination = Path.Combine(backupRoot, rel.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            File.Copy(sourcePath, destination, overwrite: true);
            count++;
        }
        ShellLog.Append($"[update] 已备份本地文件 {count} 个到 {backupRoot}"
            + (baseline is null ? "（无基线：整目录复制）" : ""));
    }

    // ---------- 内部 ----------

    /// <summary>检查事务执行体：持检查槽（任意时刻一轮），完成后释放并结算计时锚点。
    /// 取消（切渠道/关窗）不改状态——新世代或关窗路径自己决定后续相位。</summary>
    private async Task<UpdateSnapshot> RunCheckAsync(CheckRun run)
    {
        UpdateSnapshot result = GetStatus();
        try
        {
            await _checkGate.WaitAsync(run.Cts.Token).ConfigureAwait(false);
            try
            {
                result = await DoPortableCheckAsync(run.Intent, run.Trigger, run.Cts.Token)
                    .ConfigureAwait(false);
            }
            finally
            {
                _checkGate.Release();
            }
        }
        catch (OperationCanceledException)
        {
            result = GetStatus();
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[update] 检查异常: {ex.Message}");
            result = GetStatus();
        }
        finally
        {
            lock (_gate)
            {
                if (ReferenceEquals(_checkTask, run.Completion.Task))
                {
                    _checkTask = null;
                    _checkTaskEpoch = -1;
                }
                if (ReferenceEquals(_checkCts, run.Cts)) _checkCts = null;
                NoteCheckSettled(_time.GetUtcNow());
            }
            run.Cts.Dispose();
            run.Completion.TrySetResult(result);
        }
        if (run.Trigger == UpdateCheckTrigger.Auto
            && result.Phase is UpdateSnapshot.Available or UpdateSnapshot.UpToDate)
        {
            try { AutoCheckCommitted?.Invoke(result); }
            catch (Exception ex) { ShellLog.Append($"[update] 自动检查推送回调异常: {ex.Message}"); }
        }
        return result;
    }

    /// <summary>
    /// 便携安装的检查：按渠道取「版本标记 → 发布清单」，逐个来源找比本机更高的版本。
    /// 某个来源的标记不高于本机不等于整体已最新——必须继续看下一个来源（首选源滞后时
    /// 不能挡住另一源的新版本）。清单与来源在锁内锁定，下载/提交只读同一事务上下文。
    /// </summary>
    private async Task<UpdateSnapshot> DoPortableCheckAsync(CheckIntent intent,
                                                            UpdateCheckTrigger trigger,
                                                            CancellationToken ct)
    {
        string? lastError = null;
        string? needsManual = null;
        var anyResponded = false;
        var sawUnavailableNewer = false;   // 有源声称有新版但取不到可用清单：不能报「已是最新」
        UpdateSourceEndpoint? bestEndpoint = null;
        UpdateRelease? bestRelease = null;
        SemanticVersion? bestVersion = null;

        foreach (var endpoint in intent.Endpoints)
        {
            if (ct.IsCancellationRequested) break;
            if (!EndpointReachable(endpoint))
            {
                lastError = $"{endpoint.Name}（不可达或未就绪）";
                continue;
            }
            var markerAhead = false;
            try
            {
                // 每源预算覆盖「标记 + 清单」两次请求，也覆盖正文读取：HttpClient 的超时
                // 在流式读下只到响应头，镜像回头后卡住正文会拖死整轮回退。
                using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
                budget.CancelAfter(_indexTimeout);
                var client = new UpdateSourceClient(TransportForEndpoint(endpoint));
                var marker = await client.FetchMarkerAsync(endpoint, intent.Channel, budget.Token)
                    .ConfigureAwait(false);
                if (marker is null)
                {
                    lastError = $"{endpoint.Name}（没有 {intent.Channel} 渠道标记）";
                    continue;
                }
                if (!SemanticVersion.TryParse(marker.Version, out var markerVersion)
                    || !SemanticVersion.TryParse(_currentVersion, out var current))
                {
                    lastError = $"{endpoint.Name}（版本号不合法）";
                    continue;
                }
                markerAhead = markerVersion.CompareTo(current) > 0;
                if (!markerAhead)
                {
                    // 本源确认没有更高版本；「标记说有新版但清单取不到」不算这一类。
                    anyResponded = true;
                    continue;
                }

                var release = await client.FetchManifestAsync(endpoint, marker, budget.Token)
                    .ConfigureAwait(false);
                if (!string.Equals(release.Channel, intent.Channel, StringComparison.Ordinal))
                {
                    sawUnavailableNewer = true;
                    lastError = $"{endpoint.Name}（渠道不符：{release.Channel}）";
                    continue;
                }
                var incompatible = UpdateProtocol.IncompatibleReason(release);
                if (incompatible is not null)
                {
                    sawUnavailableNewer = true;
                    needsManual = $"{release.Version}：{incompatible}";
                    lastError = $"{endpoint.Name}（{incompatible}）";
                    continue;
                }
                if (!SemanticVersion.TryParse(release.Version, out var target))
                {
                    sawUnavailableNewer = true;
                    lastError = $"{endpoint.Name}（版本号不合法）";
                    continue;
                }
                // 到这里本源确实有更高且可用的版本：全源都没找到时才允许判「已是最新」。
                anyResponded = true;
                if (bestVersion is null || target.CompareTo(bestVersion.Value) > 0)
                {
                    bestEndpoint = endpoint;
                    bestRelease = release;
                    bestVersion = target;
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (OperationCanceledException)
            {
                if (markerAhead) sawUnavailableNewer = true;
                lastError = $"{endpoint.Name}（读取超时）";
            }
            catch (Exception ex)
            {
                if (markerAhead) sawUnavailableNewer = true;
                lastError = $"{endpoint.Name}（{ex.Message}）";
            }
        }
        if (ct.IsCancellationRequested) throw new OperationCanceledException(ct);

        if (bestRelease is not null && bestEndpoint is not null)
        {
            bool clearedAwaiting = false;
            UpdateSnapshot result;
            lock (_gate)
            {
                if (intent.IsStale(_phase, _epoch))
                    return Snapshot();
                _endpoint = bestEndpoint;
                _activeIntent = intent;
                _release = bestRelease;
                _targetVersion = bestRelease.Version;
                if (_awaitingStable)
                {
                    _awaitingStable = false;
                    clearedAwaiting = true;
                }
                result = SetState(intent.Epoch, UpdateSnapshot.Available);
            }
            if (clearedAwaiting)
                _savePreferences(_channel, _source, false);
            return result;
        }

        // 至少一个来源正常回应且都不高于本机：这才叫「已是最新」。全源失败仍按原语义
        // 区分自动/手动检查（自动保留旧状态，手动诚实报错）。
        if (anyResponded && needsManual is null && !sawUnavailableNewer)
        {
            lock (_gate)
            {
                if (intent.IsStale(_phase, _epoch))
                    return Snapshot();
                _release = null;
                _targetVersion = null;
                return SetState(intent.Epoch, UpdateSnapshot.UpToDate);
            }
        }

        // 有更新的版本但当前启动器装不了：不能报「已是最新」，也不能给出一个点了会失败的
        // 安装按钮——只提示手动下载。
        if (needsManual is not null)
        {
            if (trigger == UpdateCheckTrigger.Auto)
            {
                ShellLog.Append($"[update] 新版本需要手动安装，保留当前状态: {needsManual}");
                return GetStatus();
            }
            lock (_gate)
            {
                if (intent.IsStale(_phase, _epoch))
                    return Snapshot();
                _targetVersion = null;
                _release = null;
                return SetState(intent.Epoch, UpdateSnapshot.Idle,
                    error: $"新版本无法自动安装：{needsManual}");
            }
        }

        if (trigger == UpdateCheckTrigger.Auto)
        {
            ShellLog.Append($"[update] 自动检查全源失败，保留当前状态: {lastError}");
            return GetStatus();
        }
        lock (_gate)
        {
            if (intent.IsStale(_phase, _epoch))
                return Snapshot();
            _targetVersion = null;
            _release = null;
            return SetState(intent.Epoch, UpdateSnapshot.Idle,
                error: sawUnavailableNewer
                    ? $"更新源暂时不可用，请稍后重试：{lastError}"
                    : $"所有更新源均不可达：{lastError}");
        }
    }

    /// <summary>本地目录源要确认标记文件在位；网络源交给请求异常区分。</summary>
    private static bool EndpointReachable(UpdateSourceEndpoint endpoint)
    {
        if (endpoint.Marker.IsFile)
            return File.Exists(endpoint.Marker.LocalPath);
        return true;
    }

    private static readonly IUpdateTransport LocalTransport = new FileUpdateTransport();

    /// <summary>来源端点对应的传输：file:// 用于本地演练，其余走 HTTP。</summary>
    private static IUpdateTransport TransportForEndpoint(UpdateSourceEndpoint endpoint)
        => endpoint.Marker.IsFile ? LocalTransport : new HttpUpdateTransport(TimeSpan.FromMinutes(5));

    private static IUpdateTransport TransportForAssets(Uri assets)
        => assets.IsFile ? LocalTransport : new HttpUpdateTransport(TimeSpan.FromMinutes(30));

    /// <summary>检查结算：记录本进程最近一次检查收尾，并推进自动检查计时锚点。
    /// 手动检查、自动检查与全源失败同样顺延下一个周期（方案：从本轮结束起等一个间隔）。
    /// 忙态跳过走 CheckAsync 的拒绝分支，只推进锚点、不记「已完成」。</summary>
    private void NoteCheckSettled(DateTimeOffset now)
    {
        _autoAnchorUtc = now;
        _lastCheckCompletedUtc = now;
    }

    private bool CanRunCheck(UpdateCheckTrigger trigger)
    {
        return _phase switch
        {
            UpdateSnapshot.Idle or UpdateSnapshot.Available or UpdateSnapshot.UpToDate => true,
            UpdateSnapshot.Ready => trigger == UpdateCheckTrigger.Manual,
            _ => false,
        };
    }

    private string BusyReason() => _phase switch
    {
        UpdateSnapshot.Unavailable => "当前运行方式不支持自动升级",
        UpdateSnapshot.Downloading => "下载进行中",
        UpdateSnapshot.Ready => "已下载完成，请重启升级",
        UpdateSnapshot.Preparing => "升级准备中",
        UpdateSnapshot.Applying => "已提交等待退出替换",
        _ => $"当前状态 {_phase} 不允许该操作",
    };

    /// <summary>持锁状态下唯一的状态翻转出口：revision 只在实质变化时递增；
    /// 世代不符即丢弃迟到回写（epoch 由发起方在事务开始时捕获）。
    /// 目标版本不在此处隐式改写——清空目标由调用方显式置 null。</summary>
    private UpdateSnapshot SetState(long epoch, string phase, int? progress = null, string? error = null)
    {
        if (epoch != _epoch) return Snapshot();
        _phase = phase;
        if (progress.HasValue) _progress = progress.Value;
        _error = error;
        if (phase == UpdateSnapshot.Idle)
        {
            _progress = 0;
            _activeIntent = null;
            _release = null;
            _endpoint = null;
            _staged = null;
        }
        _revision++;
        return Snapshot();
    }

    private UpdateSnapshot Snapshot(string? error = null)
    {
        var effError = error ?? _error;
        return new UpdateSnapshot(_phase, _progress, _targetVersion, effError, _revision,
            _snapshotPortable, _channel, _source, _awaitingStable, _dismissedVersion, _autoCheckEnabled);
    }
}
