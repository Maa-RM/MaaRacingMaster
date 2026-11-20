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
    [property: JsonPropertyName("managed")] bool Managed,
    [property: JsonPropertyName("channel")] string Channel,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("awaiting_stable")] bool AwaitingStable)
{
    public const string ChannelStable = "stable";
    public const string ChannelPreview = "preview";
    public const string SourceCnb = "cnb";
    public const string SourceGithub = "github";

    public const string Unavailable = "unavailable";  // 非受管安装：自动升级不可用（手动升级口径）
    public const string Idle = "idle";
    public const string Available = "available";      // 已发现可安装目标，未下载
    public const string UpToDate = "up_to_date";      // 当前渠道没有更高版本
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
///
/// 语义基线（与计划文档一致）：
/// · 两个下载地址指向同一发行货架，内容与版本应当一致；名字只决定首选，
///   首选不可达回退其余地址，换源重下受「同目标（版本+SHA256）」约束。
/// · 渠道是用户设置，不是包属性：始终显式传给更新器，默认稳定轨；不做降级
///   安装，从测试轨切回后等稳定轨版本追上再升级（数据只升不降）。
/// · 切渠道换的是目标发行，清空在途与就绪；切下载源只换首选地址，
///   在途下载与就绪包保留。
///
/// 两条不变量（各自只有一个出口，避免语义散落在字符串比较里）：
/// ① CheckIntent：一次「检查→下载→提交」的不可变事务上下文，整条异步链只读它；
/// ② Epoch：任何渠道切换开启新世代；所有异步回写（检查/下载/镜像回退/进度）
///    都携带发起时的世代，经 SetState(epoch) 唯一出口校验，迟到回写一律丢弃。
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

    /// <summary>一次检查的不可变事务上下文。检查开始时从当前状态快照出来，
    /// 检查、下载、镜像回退全程只读它——切换设置不会让半程事务混用两套渠道/来源。
    /// 渠道是用户设置，总是显式传入；安装包烧录的渠道在此模型下不被读取。</summary>
    private sealed record CheckIntent(
        long Epoch,
        string StartPhase,
        string Channel,
        string[] Sources)
    {
        public bool IsStale(string phase, long epoch) => epoch != Epoch || phase != StartPhase;

        public UpdateManager CreateManager(string source, IVelopackLocator? locator)
        {
            var options = new UpdateOptions { ExplicitChannel = Channel };
            return locator is null
                ? new UpdateManager(source, options)
                : new UpdateManager(source, options, locator);
        }
    }

    /// <summary>随包来源目录：构造时解析一次，之后只读。
    /// 每行「名字|地址」（cnb|https://… / github|https://…）给下载地址起名；
    /// 所有地址指向同一发行货架，名字只决定尝试顺序（首选在前），不构成不同的
    /// 发行轨道。发布端（assemble.ps1）是命名行的唯一生产者并强制两处齐全；
    /// 运行时无标签行按无名地址排在末位参与回退，空地址行跳过，
    /// 全部无效时诚实报「未配置」。</summary>
    private sealed class SourceCatalog
    {
        private readonly (string? Tag, string Url)[] _entries;

        public SourceCatalog(string? feedSource)
        {
            _entries = (feedSource ?? string.Empty)
                .Split(new[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries)
                .Select(s => s.Trim())
                .Where(s => s.Length > 0 && !s.StartsWith('#'))
                .Select(Parse)
                .Where(e => e.Url.Length > 0)
                .ToArray();
        }

        public bool IsEmpty => _entries.Length == 0;

        /// <summary>按当前偏好排序的尝试顺序：首选地址在前，其余等价地址作回退。
        /// LINQ 排序稳定，同名条目保持配置原序。</summary>
        public string[] For(string preference) => _entries
            .OrderByDescending(e => string.Equals(e.Tag, preference, StringComparison.OrdinalIgnoreCase))
            .Select(e => e.Url)
            .ToArray();

        private static (string? Tag, string Url) Parse(string raw)
        {
            var sep = raw.IndexOf('|');
            return sep > 0
                ? (raw.Substring(0, sep).Trim().ToLowerInvariant(), raw.Substring(sep + 1).Trim())
                : (null, raw);
        }
    }

    private readonly SourceCatalog _catalog;
    private readonly IVelopackLocator? _locator;   // 显式定位器注入（合同探针用）；null = 默认定位器
    private readonly PrepareGateAsync? _prepareGate;
    private readonly HandoffRecoveryAsync? _handoffRecovery;
    private readonly bool _managed;
    private readonly Action<string, string, bool> _savePreferences; // 偏好落盘（channel, source, awaitingStable）
    private readonly object _gate = new();

    private string _channel = UpdateSnapshot.ChannelStable;   // 渠道是用户设置，默认显式稳定轨
    private string _source = UpdateSnapshot.SourceCnb;        // 当前首选下载地址
    private bool _awaitingStable;                             // 从测试轨切回且稳定版尚未追上

    private long _revision;
    private long _epoch;                           // 渠道切换开启新世代：作废此前所有在途异步回写
    private string _phase = UpdateSnapshot.Idle;
    private int _progress;
    private string? _targetVersion;
    private string? _error;
    private UpdateManager? _manager;               // 锁定为检查成功的源（下载/提交绑定同一已验证来源）
    private string? _lockedSource;                 // 与 _manager 配对：镜像回退时跳过已锁源
    private CheckIntent? _activeIntent;            // 当前已验证目标所属的事务上下文
    private UpdateInfo? _pending;
    private CancellationTokenSource? _downloadCts;
    private Task? _downloadTask;

    public UpdateCoordinator(string? feedSource, bool managed,
                             IVelopackLocator? locator = null,
                             PrepareGateAsync? prepareGate = null,
                             HandoffRecoveryAsync? handoffRecovery = null,
                             string? initialChannel = null,
                             string? initialSource = null,
                             bool initialAwaitingStable = false,
                             Action<string, string, bool>? savePreferences = null)
    {
        _catalog = new SourceCatalog(feedSource);
        _managed = managed;
        _locator = locator;
        _prepareGate = prepareGate;
        _handoffRecovery = handoffRecovery;
        _savePreferences = savePreferences ?? ((_, _, _) => { });
        if (initialChannel == UpdateSnapshot.ChannelPreview) _channel = UpdateSnapshot.ChannelPreview;
        if (initialSource == UpdateSnapshot.SourceGithub) _source = UpdateSnapshot.SourceGithub;
        _awaitingStable = initialAwaitingStable && _channel == UpdateSnapshot.ChannelStable;
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
    /// 来源按「首选在前」的顺序尝试：第一个「可达且给出结论」的地址胜出并锁定
    /// （下载/提交绑定同一已验证来源）；首选不可达时回退其余等价地址。全部
    /// 不可达/失败时诚实报错并可重试——不换目标，不把空/坏镜像误报为「已是最新」。
    /// 检查在途期间状态可能被下载/提交推进（网络慢源是常态）：返回时校验发起时
    /// 的相位与世代，不一致即丢弃结果并返回当前快照。</summary>
    public async Task<UpdateSnapshot> CheckAsync()
    {
        CheckIntent intent;
        lock (_gate)
        {
            if (!CanRunCheck())
                return Snapshot(BusyReason());
            var sources = _catalog.For(_source);
            if (sources.Length == 0)
            {
                _targetVersion = null;
                _pending = null;
                _activeIntent = null;
                return SetState(_epoch, UpdateSnapshot.Idle, error: "更新源未配置（或所选源无可用地址）");
            }
            intent = new CheckIntent(_epoch, _phase, _channel, sources);
        }
        string? lastError = null;
        foreach (var source in intent.Sources)
        {
            if (!IsSourceReachable(source))
            {
                lastError = $"{source}（不可达或未就绪）";
                continue;
            }
            try
            {
                var manager = intent.CreateManager(source, _locator);
                var update = await manager.CheckForUpdatesAsync().ConfigureAwait(false);
                bool clearedAwaiting = false;
                UpdateSnapshot result;
                lock (_gate)
                {
                    if (intent.IsStale(_phase, _epoch))
                        return Snapshot(); // 迟到结果或切换前的旧检查丢弃；不重绑源
                    _manager = manager; // 锁定本源：下载/提交绑定同一已验证来源
                    _lockedSource = source;
                    _activeIntent = intent;
                    if (update is null)
                    {
                        _targetVersion = null;
                        _pending = null;
                        return SetState(intent.Epoch, UpdateSnapshot.UpToDate);
                    }
                    _pending = update;
                    _targetVersion = update.TargetFullRelease.Version.ToString();
                    if (_awaitingStable)
                    {
                        _awaitingStable = false; // 稳定轨追上了：等待标记完成使命
                        clearedAwaiting = true;
                    }
                    result = SetState(intent.Epoch, UpdateSnapshot.Available);
                }
                if (clearedAwaiting)
                    _savePreferences(_channel, _source, false); // 锁外落盘：稳定轨已追上
                return result;
            }
            catch (Exception ex)
            {
                lastError = $"{source}（{ex.Message}）";
            }
        }
        lock (_gate)
        {
            if (intent.IsStale(_phase, _epoch))
                return Snapshot(); // 迟到的失败结果或旧世代结果同理不覆盖
            _targetVersion = null;
            _pending = null;
            return SetState(intent.Epoch, UpdateSnapshot.Idle,
                error: $"所有更新源均不可达：{lastError}");
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

    /// <summary>开始下载：仅 available 允许；下载中重复调用复用当前任务（幂等）；
    /// ready 重复调用幂等返回（已就绪无需重下）。启动即回包 downloading 快照——
    /// 下载在后台任务进行，进度与结果由独立状态查询获得（评审回归：等待整段下载
    /// 会让首次下载期间界面拿不到进度、也无法取消）。事务上下文随任务传递：
    /// 镜像回退只用检查时锁定的那套渠道与来源，不重新读取可能已被切换的偏好。</summary>
    public Task<UpdateSnapshot> StartDownloadAsync()
    {
        UpdateInfo? pending;
        CheckIntent intent;
        CancellationTokenSource cts;
        lock (_gate)
        {
            if (_phase == UpdateSnapshot.Downloading && _downloadTask is not null)
                return Task.FromResult(Snapshot()); // 复用当前任务
            if (_phase == UpdateSnapshot.Ready)
                return Task.FromResult(Snapshot()); // 已下载完成：重复调用幂等返回
            if (_phase != UpdateSnapshot.Available)
                return Task.FromResult(Snapshot(BusyReason()));
            if (_pending is null || _activeIntent is null)
                return Task.FromResult(Snapshot("没有可安装目标，请先检查更新"));
            pending = _pending;
            intent = _activeIntent;
            cts = new CancellationTokenSource();
            _downloadCts = cts;
            _revision++;
            _phase = UpdateSnapshot.Downloading;
            _error = null;
            // 任务在锁外创建：DownloadAsync 的同步前段与完成翻转都拿同一把锁，
            // 持锁启动会在同步完成路径上自我死锁。
        }
        _downloadTask = Task.Run(() => DownloadAsync(pending!, intent, cts.Token));
        return Task.FromResult(GetStatus());
    }

    private async Task DownloadAsync(UpdateInfo update, CheckIntent intent, CancellationToken ct)
    {
        string? failure = null;
        try
        {
            var manager = _manager
                ?? throw new InvalidOperationException("未锁定更新源（先检查更新）");
            await DownloadOnManagerAsync(manager, update, intent, ct).ConfigureAwait(false);
            MarkReadyIfDownloading(intent);
            return;
        }
        catch (OperationCanceledException)
        {
            BackToAvailable(intent, "下载已取消");
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
            if (await TryDownloadMirrorAsync(update, intent, ct).ConfigureAwait(false))
            {
                MarkReadyIfDownloading(intent);
                return;
            }
            failure = $"{failure}（镜像回退未找到同目标源）";
        }
        catch (OperationCanceledException)
        {
            BackToAvailable(intent, "下载已取消");
            return;
        }
        catch (Exception ex)
        {
            failure = $"{failure}；镜像回退也失败: {ex.Message}";
        }
        BackToAvailable(intent, $"下载失败: {failure}");
    }

    private async Task DownloadOnManagerAsync(UpdateManager manager, UpdateInfo update,
                                              CheckIntent intent, CancellationToken ct)
    {
        // 进度回调来自库的下载线程：跨线程进入 UI 无关的锁内状态即可（revision 单调）。
        // 世代校验不可省：渠道切换后旧下载任务的迟到进度不得写进新世代。
        await manager.DownloadUpdatesAsync(update, p =>
        {
            lock (_gate)
            {
                if (intent.Epoch == _epoch && _phase == UpdateSnapshot.Downloading)
                {
                    _progress = p;
                    _revision++;
                }
            }
        }, ct).ConfigureAwait(false);
    }

    /// <summary>镜像回退：按序尝试其余地址；候选给出同目标发行才换源下载。
    /// 换源成功即重绑管理器与目标（提交绑定到已验证的镜像）。
    /// 候选源与更新器选项都取自事务上下文，不回读当前偏好。</summary>
    private async Task<bool> TryDownloadMirrorAsync(UpdateInfo update, CheckIntent intent, CancellationToken ct)
    {
        foreach (var source in intent.Sources)
        {
            lock (_gate)
            {
                if (intent.Epoch != _epoch) return false; // 世代已变：停止为旧事务找镜像
            }
            if (source == _lockedSource || !IsSourceReachable(source))
                continue;
            try
            {
                var manager = intent.CreateManager(source, _locator);
                var info = await manager.CheckForUpdatesAsync().ConfigureAwait(false);
                if (info?.TargetFullRelease is null || !SameTarget(update.TargetFullRelease, info.TargetFullRelease))
                    continue;
                await DownloadOnManagerAsync(manager, info, intent, ct).ConfigureAwait(false);
                lock (_gate)
                {
                    if (intent.Epoch != _epoch) return false; // 下载期间被切换：结果作废
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

    private void MarkReadyIfDownloading(CheckIntent intent)
    {
        lock (_gate)
        {
            if (intent.Epoch == _epoch && _phase == UpdateSnapshot.Downloading)
                SetState(intent.Epoch, UpdateSnapshot.Ready, progress: 100);
        }
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
    /// 渠道换的是目标发行：清空在途下载与就绪包、开启新世代、落盘偏好、当场重查；
    /// 从测试轨切回时标记「等待稳定轨追上」——不做降级安装，稳定版版本高于当前
    /// 测试版后自然提示可升级。下载源只是同一货架的地址选择：保留在途下载与
    /// 就绪包，仅换首选顺序，下次检查与镜像回退自然采用。提交临界区
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
        lock (_gate)
        {
            if (_phase is UpdateSnapshot.Preparing or UpdateSnapshot.Applying)
                return Snapshot("升级提交中，不能切换设置");
            if (isChannel)
            {
                changed = value != _channel;
                if (changed)
                {
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
            }
        }
        if (!changed) return GetStatus();
        _savePreferences(_channel, _source, _awaitingStable);
        return isChannel ? await CheckAsync().ConfigureAwait(false) : GetStatus();
    }

    /// <summary>开启新世代并清空一切在途状态。持锁调用；这是唯一绕过 SetState 的状态
    /// 翻转——因为它主动作废旧世代，而不是在旧世代内推进相位。</summary>
    private void ResetForNewEpoch()
    {
        _downloadCts?.Cancel();
        _downloadCts = null;
        _downloadTask = null;
        _manager = null;
        _lockedSource = null;
        _activeIntent = null;
        _pending = null;
        _targetVersion = null;
        _epoch++;
        _phase = UpdateSnapshot.Idle;
        _progress = 0;
        _error = null;
        _revision++;
    }

    /// <summary>提交安装：仅 ready 允许。闸门拒绝/失败 → 状态回 ready 并带原因；
    /// 更新器交接成功（等本进程退出后整树替换）才进入 applying 并触发计划内关窗；
    /// 交接失败（更新器缺失/无法启动）→ 走恢复委托重启后端（闸门通过 = 后端已确认
    /// 退出，不恢复则「旧版可继续使用」是假话），再回 ready 带原因：关窗未排，
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
                    return SetState(_epoch, UpdateSnapshot.Ready, error: reason);
                }
            }
        }
        var manager = _manager;
        var pending = _pending;
        if (manager is null || pending is null)
        {
            lock (_gate)
            {
                return SetState(_epoch, UpdateSnapshot.Ready, error: "未锁定更新源（先检查更新）");
            }
        }
        try
        {
            manager.WaitExitThenApplyUpdates(pending.TargetFullRelease, silent: true, restart: true);
        }
        catch (Exception ex)
        {
            var recovered = false;
            if (_handoffRecovery is not null)
            {
                try
                {
                    recovered = await _handoffRecovery().ConfigureAwait(false);
                }
                catch (Exception recEx)
                {
                    ShellLog.Append($"[update] 交接失败恢复异常: {recEx.Message}");
                }
            }
            ShellLog.Append($"[update] 更新器交接失败: {ex.Message}（后端恢复: {(recovered ? "已重启" : "失败，需重启应用")}）");
            lock (_gate)
            {
                return SetState(_epoch, UpdateSnapshot.Ready,
                    error: recovered
                        ? $"升级程序启动失败: {ex.Message}（旧版本可继续使用，可重试升级）"
                        : $"升级程序启动失败: {ex.Message}（自动恢复失败，请重启应用后再试）");
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
            _pending = null;
            _activeIntent = null;
        }
        _revision++;
        return Snapshot();
    }

    private UpdateSnapshot Snapshot(string? error = null)
    {
        var effError = error ?? _error;
        return new UpdateSnapshot(_phase, _progress, _targetVersion, effError, _revision,
            _managed, _channel, _source, _awaitingStable);
    }
}
