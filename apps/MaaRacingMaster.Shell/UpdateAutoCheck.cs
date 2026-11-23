using System;
using System.Threading;
using System.Threading.Tasks;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 进程内后台自动检查调度：首轮立即发起（不额外延迟，窗口与业务后端照常启动），
/// 之后从「最近一次检查收尾」起算一个间隔。它只决定「什么时候问协调器」——
/// 开关、受管安装、相位准入与结果提交都由协调器判断；到期遇到忙态由协调器
/// 记录锚点，本轮跳过、下个周期再试。
///
/// 等待一律用 <see cref="TimeProvider"/> + 带令牌的 <see cref="Task.Delay(TimeSpan, TimeProvider, CancellationToken)"/>：
/// 关窗 / 关掉开关 / 重新启用都能立即打断等待，不靠轮询，也不占 UI 线程。
/// 重新启用开关以「本进程最近一次检查收尾」为锚点（本进程还没检查过 → 立即安排）。
/// </summary>
internal sealed class UpdateAutoCheck : IDisposable
{
    private readonly UpdateCoordinator _coordinator;
    private readonly TimeProvider _time;
    private readonly TimeSpan _interval;
    private readonly object _gate = new();

    private CancellationTokenSource? _stop;
    private CancellationTokenSource? _wake; // 当前等待的唤醒源（开关变化时打断）

    public UpdateAutoCheck(UpdateCoordinator coordinator, TimeProvider? timeProvider = null,
                           TimeSpan? interval = null)
    {
        _coordinator = coordinator;
        _time = timeProvider ?? TimeProvider.System;
        _interval = interval ?? TimeSpan.FromHours(1);
        _coordinator.AutoCheckSettingChanged += Wake;
    }

    /// <summary>启动调度循环（幂等）。窗口初始化后调用。</summary>
    public void Start()
    {
        lock (_gate)
        {
            if (_stop is not null) return;
            _stop = new CancellationTokenSource();
            _ = LoopAsync(_stop.Token);
        }
    }

    /// <summary>停止调度：取消等待，并取消在途检查的索引请求（关窗不放 I/O）。
    /// 幂等；停止期间新到的检查请求由协调器自身的相位/令牌语义处理。</summary>
    public void Stop()
    {
        CancellationTokenSource? stop;
        lock (_gate)
        {
            stop = _stop;
            _stop = null;
            _wake?.Cancel();
        }
        stop?.Cancel();
        stop?.Dispose();
        _coordinator.CancelPendingCheck();
    }

    /// <summary>开关变化即打断当前等待：关闭 → 下次循环读到停用；重新启用 → 按新锚点重算。</summary>
    private void Wake()
    {
        lock (_gate) _wake?.Cancel();
    }

    private async Task LoopAsync(CancellationToken stop)
    {
        while (!stop.IsCancellationRequested)
        {
            var delay = _coordinator.NextAutoCheckDelay(_time.GetUtcNow(), _interval);
            if (delay is null)
            {
                // 停用中：等开关变化的唤醒信号，不做轮询
                await DelayOrWakeAsync(Timeout.InfiniteTimeSpan, stop).ConfigureAwait(false);
                if (stop.IsCancellationRequested) return;
                continue;
            }

            if (delay.Value > TimeSpan.Zero
                && await DelayOrWakeAsync(delay.Value, stop).ConfigureAwait(false))
            {
                continue; // 等待期间被唤醒（开关变化）：重算到期时间，不盲目检查
            }
            if (stop.IsCancellationRequested) return;

            // 等待期间手动检查/切渠道可能把锚点推后：只有仍到期才发起。
            if (_coordinator.NextAutoCheckDelay(_time.GetUtcNow(), _interval) is not TimeSpan due
                || due > TimeSpan.Zero)
            {
                continue;
            }

            try
            {
                await _coordinator.CheckAsync(UpdateCheckTrigger.Auto).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                // 检查失败不影响下一轮：协调器自己记录锚点（全源失败也顺延一个周期）
                ShellLog.Append($"[update] 自动检查异常: {ex.Message}");
            }
        }
    }

    /// <summary>等待指定时长；返回 true = 被开关变化唤醒（而非计时到点）。
    /// 停止令牌触发时抛 OperationCanceledException 由循环退出承担——
    /// 这里统一转成 false，让循环继续走到 IsCancellationRequested 判断。</summary>
    private async Task<bool> DelayOrWakeAsync(TimeSpan delay, CancellationToken stop)
    {
        CancellationTokenSource wake;
        lock (_gate) { wake = new CancellationTokenSource(); _wake = wake; }
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(stop, wake.Token);
        try
        {
            await Task.Delay(delay, _time, linked.Token).ConfigureAwait(false);
            return false;
        }
        catch (OperationCanceledException) when (!stop.IsCancellationRequested)
        {
            return true;
        }
        catch (OperationCanceledException)
        {
            return false;
        }
        finally
        {
            lock (_gate) { if (ReferenceEquals(_wake, wake)) _wake = null; }
            wake.Dispose();
        }
    }

    public void Dispose()
    {
        _coordinator.AutoCheckSettingChanged -= Wake;
        Stop();
    }
}
