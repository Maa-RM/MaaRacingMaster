using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace MaaRacingMaster.Shell;

/// <summary>backend 进程非零退出 / 连接断开。</summary>
public sealed class BackendDisconnectedException : Exception
{
    public BackendDisconnectedException(int? exitCode)
        : base($"backend disconnected (exit code: {exitCode?.ToString() ?? "unknown"})")
    {
    }
}

/// <summary>RPC 层返回 ok=false。</summary>
public sealed class RpcErrorException : Exception
{
    public RpcErrorException(string message) : base(message) { }
}

/// <summary>侧车关停结果：ExitCode=退出码（未知为 null）；ConfirmedExit=关停流程结束时
/// 已确认进程真正退出。更新协调（P2 起）以 ConfirmedExit 判断能否进入提交，不允许把
/// 「流程走完」误当「进程已退」。</summary>
public sealed record SidecarShutdownResult(int? ExitCode, bool ConfirmedExit);

/// <summary>
/// PythonSidecar —— 进程生命周期 + JSONL transport（契约测试 11/11 验证通过后复用）。
/// 唯一 stdout reader + pending map + timeout + stderr drain + grace shutdown。
/// </summary>
public sealed class PythonSidecar : IDisposable
{
    private readonly Process _process;
    private readonly StreamWriter _stdin;
    private readonly StreamReader _stdout;
    private readonly string? _workDir;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly ConcurrentDictionary<ulong, TaskCompletionSource<JsonElement>> _pending = new();
    private ulong _nextId;
    private volatile bool _backendDown;
    private volatile bool _closing;
    private bool _deathReported; // 同一进程世代最多一条死亡事件（随实例创建归零）
    private bool _disposed;

    /// <summary>sidecar 主动推送的事件（type=event）：事件名 + 数据负载（可为 Undefined）。
    /// 由 shell 侧订阅并按需响应，如轮次开始/结束、运行结束自动退出（auto_exit）。</summary>
    public event Action<string, JsonElement>? SidecarEvent;

    /// <summary>后端进程退出（读到 stdout 末尾且非本壳主动关闭）。退出码可能未知。</summary>
    public event Action<int?>? BackendExited;

    /// <summary>便捷构造：脚本直跑（契约测试用）。</summary>
    public PythonSidecar(string pythonExe, string scriptPath)
        : this(pythonExe, $"-u \"{scriptPath}\"", null)
    {
    }

    /// <summary>完整构造：fileName + arguments + workingDirectory（正式 shell 用 -m 模块启动）。</summary>
    public PythonSidecar(string fileName, string arguments, string? workingDirectory)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            Arguments = arguments,
            WorkingDirectory = workingDirectory ?? string.Empty,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        _workDir = workingDirectory;
        // 强制 Python UTF-8 模式：本 shell 带 requireAdministrator，UAC 提权后
        // 父进程的 PYTHONUTF8 环境变量不继承，Python stdout 会落回 GBK/cp936，
        // 与 C# 的 UTF-8 解码不一致 → 中文乱码（\uFFFD）。
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        _process = Process.Start(psi)
            ?? throw new InvalidOperationException("failed to start sidecar process");
        _stdin = _process.StandardInput;
        _stdout = _process.StandardOutput;

        _ = Task.Run(ReaderLoopAsync); // 唯一 stdout reader
        _ = Task.Run(DrainStderrAsync); // stderr 持续 drain，防 pipe 填满
    }

    private async Task ReaderLoopAsync()
    {
        try
        {
            string? line;
            while ((line = await _stdout.ReadLineAsync()) is not null)
            {
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    var root = doc.RootElement;
                    if (root.TryGetProperty("type", out var t) && t.GetString() == "response")
                    {
                        var id = root.GetProperty("id").GetUInt64();
                        if (_pending.TryRemove(id, out var tcs))
                        {
                            bool ok = root.GetProperty("ok").GetBoolean();
                            if (ok)
                            {
                                // Clone()：JsonElement 深拷贝，脱离 JsonDocument 生命周期
                                tcs.SetResult(root.Clone());
                            }
                            else
                            {
                                var err = root.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String
                                    ? e.GetString() ?? "rpc error"
                                    : "rpc error";
                                tcs.SetException(new RpcErrorException(err));
                            }
                        }
                        // 未知 id 的 response：忽略（可能已超时移除）
                    }
                    // type=event：sidecar 主动推送事件（作为 response 分支的 else），上抛给 shell 订阅方处理
                    else
                    {
                        var evtName = root.TryGetProperty("event", out var ev) &&
                                      ev.ValueKind == JsonValueKind.String
                            ? ev.GetString()
                            : null;
                        if (evtName is not null)
                        {
                            // 数据负载必须深拷贝：解析文档在循环末释放，跨线程访问即抛已释放异常
                            JsonElement payload = default;
                            if (root.TryGetProperty("data", out var d) && d.ValueKind != JsonValueKind.Null)
                                payload = d.Clone();
                            try { SidecarEvent?.Invoke(evtName, payload); }
                            catch (Exception evEx) { Console.Error.WriteLine($"[sidecar] event handler error: {evEx.Message}"); }
                        }
                    }
                }
                catch (JsonException ex)
                {
                    // malformed stdout：记录并继续，不 crash
                    Console.Error.WriteLine($"[sidecar] malformed stdout line ignored: {ex.Message}");
                }
            }
        }
        catch (Exception ex) when (ex is ObjectDisposedException or IOException)
        {
            // 主动 Dispose / 关窗竞态：流被本壳关闭，不当作后端异常
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[sidecar] stdout reader error: {ex.Message}");
        }
        finally
        {
            // EOF = Python 进程退出 → 所有 pending 立即 disconnected
            FailAllPending(new BackendDisconnectedException(SafeExitCode()));
            _backendDown = true;

            // 只有非主动关闭的 EOF 才是后端死亡：正常关窗/主动关停不报
            if (!_closing && !_deathReported)
            {
                _deathReported = true;
                var exitCode = WaitForExitCode();
                try { BackendExited?.Invoke(exitCode); }
                catch (Exception ex) { Console.Error.WriteLine($"[sidecar] backend exited handler error: {ex.Message}"); }
            }
        }
    }

    /// <summary>读退出码；进程已释放/不可访问时返回 null（退出码只作诊断，不参与判定）。</summary>
    private int? SafeExitCode()
    {
        try { return _process.HasExited ? _process.ExitCode : null; }
        catch { return null; }
    }

    /// <summary>EOF 后短暂等进程真正退出，尽量给死亡通知带上退出码。</summary>
    private int? WaitForExitCode()
    {
        try
        {
            if (_process.WaitForExit(500)) return _process.ExitCode;
            return SafeExitCode();
        }
        catch
        {
            return null;
        }
    }

    private async Task DrainStderrAsync()
    {
        // GUI 无控制台：sidecar stderr 落盘便于排查。固定落用户数据目录
        // （%APPDATA%/MaaRacingMaster/logs，开发/发行一致、不受日志开关控制——框架诊断日志）；
        // APPDATA 不可用时回退工作目录，保证跨机器可移植。
        // 体积有界：超过上限轮转一份备份后重开，长跑不再无界追加。
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        var baseDir = string.IsNullOrEmpty(appData)
            ? (_workDir ?? Environment.CurrentDirectory)
            : Path.Combine(appData, "MaaRacingMaster");
        var logPath = Path.Combine(baseDir, "logs", "sidecar_stderr.log");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
            RotateIfOversize(logPath);
            using var fs = new StreamWriter(logPath, append: true) { AutoFlush = true };
            string? line;
            while ((line = await _process.StandardError.ReadLineAsync()) is not null)
            {
                fs.WriteLine(line);
            }
        }
        catch
        {
            // 进程退出后 reader 结束，忽略
        }
    }

    /// <summary>stderr 日志上限（字节）：超过即轮转为 .1（只留一份备份）。</summary>
    private const long StderrLogMaxBytes = 8L * 1024 * 1024;

    /// <summary>超限则把当前 stderr 日志轮转为 .1；失败不抛（不影响日志本身）。</summary>
    private static void RotateIfOversize(string path)
    {
        try
        {
            var fi = new FileInfo(path);
            if (!fi.Exists || fi.Length < StderrLogMaxBytes) return;
            var bak = path + ".1";
            if (File.Exists(bak)) File.Delete(bak);
            File.Move(path, bak);
        }
        catch
        {
            // 轮转失败就继续追加，宁可超限也不丢日志
        }
    }

    private void FailAllPending(Exception ex)
    {
        foreach (var kv in _pending)
        {
            if (_pending.TryRemove(kv.Key, out var tcs))
            {
                tcs.SetException(ex);
            }
        }
    }

    public bool HasExited => _process.HasExited;
    public int? ExitCode => _process.HasExited ? _process.ExitCode : null;
    public int ProcessId => _process.Id;

    /// <summary>发送 request 并等待对应 response，按 id 匹配；超时只影响本请求。</summary>
    public async Task<JsonElement> CallAsync(
        string method, object? paramsObj = null, TimeSpan? timeout = null, CancellationToken ct = default)
    {
        if (_backendDown || _process.HasExited)
            throw new BackendDisconnectedException(_process.HasExited ? _process.ExitCode : null);

        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;

        try
        {
            var req = JsonSerializer.Serialize(new
            {
                type = "request",
                id,
                method,
                @params = paramsObj,
            });
            await _writeLock.WaitAsync(ct);
            try
            {
                await _stdin.WriteLineAsync(req.AsMemory(), ct);
                await _stdin.FlushAsync(ct);
            }
            finally
            {
                _writeLock.Release();
            }

            return await tcs.Task.WaitAsync(timeout ?? TimeSpan.FromSeconds(10), ct);
        }
        catch (Exception)
        {
            _pending.TryRemove(id, out _); // 超时/取消/断开：清理占位，不留悬空
            throw;
        }
    }

    /// <summary>
    /// graceful shutdown：发 shutdown → 等进程自退（grace 内）→ 超时 Kill 整个进程树 →
    /// 强杀后有界等待确认真实退出。不 Dispose。返回结果区分「流程走完」与「确认已退」：
    /// 强杀后仍未退出时 ConfirmedExit=false 并记 shell.log，不吞掉残留事实。
    /// </summary>
    public async Task<SidecarShutdownResult> ShutdownAsync(TimeSpan grace, CancellationToken ct = default)
    {
        _closing = true; // 本壳主动关停：随后的 EOF 不再报后端异常退出
        if (!_process.HasExited)
        {
            try
            {
                await CallAsync("shutdown", null, grace, ct);
            }
            catch
            {
                // Python 未响应/已断开：继续走退出流程
            }
        }

        if (!_process.HasExited)
        {
            try
            {
                await _process.WaitForExitAsync(ct).WaitAsync(grace, ct);
            }
            catch (TimeoutException)
            {
                // grace 超时，强制终止；终止后必须有界等待确认（壳带 requireAdministrator，
                // 子进程残留会让升级提交因文件占用失败，必须让上层知道真实退出状态）
                KillTree();
                bool confirmed;
                try { confirmed = _process.WaitForExit(ShutdownConfirmTimeoutMs); }
                catch { confirmed = false; }
                if (!confirmed)
                    ShellLog.Append($"[sidecar] 强制终止后 {ShutdownConfirmTimeoutMs}ms 仍未退出（pid={SafePid()}），存在残留进程");
                return new SidecarShutdownResult(ExitCode, confirmed);
            }
        }
        return new SidecarShutdownResult(ExitCode, ConfirmedExit: true);
    }

    private void KillTree()
    {
        if (_process.HasExited) return;
        try
        {
            _process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException)
        {
            // 已在退出竞态中退出，忽略
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _closing = true; // Dispose 会关流并 KillTree，随后的 EOF 不是后端异常退出
        try { _stdin.Dispose(); } catch { }
        try { _stdout.Dispose(); } catch { }
        try { _writeLock.Dispose(); } catch { }
        if (!_process.HasExited)
        {
            // 关流后先给一个短的 stdin EOF 自退窗口（侧车主循环读 stdin，父关即自退），
            // 窗口过了才强杀；强杀后同样有界等待，不吞掉「仍有残留」的事实。
            bool selfExited;
            try { selfExited = _process.WaitForExit(DisposeSelfExitGraceMs); }
            catch { selfExited = false; }
            if (!selfExited)
            {
                KillTree();
                bool confirmed;
                try { confirmed = _process.WaitForExit(ShutdownConfirmTimeoutMs); }
                catch { confirmed = false; }
                if (!confirmed)
                    ShellLog.Append($"[sidecar] Dispose 强杀后 {ShutdownConfirmTimeoutMs}ms 仍未退出（pid={SafePid()}），存在残留进程");
            }
        }
        _process.Dispose();
    }

    /// <summary>强杀后确认退出的有界等待上限（毫秒）。</summary>
    private const int ShutdownConfirmTimeoutMs = 5_000;

    /// <summary>Dispose 关流后给子进程的 stdin EOF 自退窗口（毫秒）。</summary>
    private const int DisposeSelfExitGraceMs = 1_500;

    private int SafePid()
    {
        try { return _process.Id; }
        catch { return -1; }
    }

    /// <summary>
    /// 启动期清场：上一会话异常退出可能遗留侧车（父壳死了但侧车正忙、尚未走到 stdin EOF 自退）。
    /// 只清理本应用根内的侧车（python 进程路径在本应用根内且命令行在跑本侧车模块）；
    /// 其它检出 / 其它安装的进程不动。PowerShell 扫描失败只记日志并返回 -1，不阻断启动
    /// （侧车 EOF 自退机制仍是兜底，见 experiments/app_update_velopack P0 取证）。
    /// 返回清理掉的进程数。
    /// </summary>
    public static int CleanupLeftovers(string appRoot)
    {
        try
        {
            var candidates = ListLeftoverSidecarPids(appRoot);
            var killed = 0;
            foreach (var pid in candidates)
            {
                try
                {
                    using var k = Process.Start(new ProcessStartInfo
                    {
                        FileName = "taskkill.exe",
                        Arguments = $"/PID {pid} /T /F",
                        UseShellExecute = false,
                        CreateNoWindow = true,
                    });
                    k?.WaitForExit(3000);
                    ShellLog.Append($"[sidecar] 启动清场：终止遗留侧车进程 pid={pid}");
                    killed++;
                }
                catch (Exception ex)
                {
                    ShellLog.Append($"[sidecar] 启动清场：终止遗留侧车 pid={pid} 失败: {ex.Message}");
                }
            }
            return killed;
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[sidecar] 启动清场：扫描失败（不影响启动）: {ex.Message}");
            return -1;
        }
    }

    /// <summary>枚举「python 进程 + 解释器路径在本应用根内 + 命令行在跑本侧车模块」的 pid。</summary>
    private static IEnumerable<int> ListLeftoverSidecarPids(string appRoot)
    {
        const string script = """
            [Console]::OutputEncoding=[System.Text.Encoding]::UTF8
            Get-CimInstance Win32_Process -Filter "Name like 'python%'" | ForEach-Object {
                '{0}|{1}|{2}' -f $_.ProcessId, $_.ExecutablePath, $_.CommandLine
            }
            """;
        var psi = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = "-NoProfile -NonInteractive -EncodedCommand " +
                        Convert.ToBase64String(Encoding.Unicode.GetBytes(script)),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
        };
        using var ps = Process.Start(psi)
            ?? throw new InvalidOperationException("powershell 启动失败");
        string output;
        using (ps)
        {
            if (!ps.WaitForExit(8_000))
            {
                try { ps.Kill(); } catch { }
                throw new TimeoutException("遗留侧车扫描超时（8s）");
            }
            output = ps.StandardOutput.ReadToEnd();
        }

        var rootNorm = Path.TrimEndingDirectorySeparator(Path.GetFullPath(appRoot));
        var result = new List<int>();
        foreach (var line in output.Split('\n'))
        {
            var parts = line.Trim('\r').Split('|', 3);
            if (parts.Length < 3) continue;
            if (!int.TryParse(parts[0], out var pid)) continue;
            var exe = parts[1].Trim();
            var cmd = parts[2];
            if (cmd.Length == 0 || !cmd.Contains(SidecarModuleMarker, StringComparison.Ordinal))
                continue;
            if (exe.Length == 0) continue;
            try
            {
                var exeNorm = Path.TrimEndingDirectorySeparator(Path.GetFullPath(exe));
                if (exeNorm.StartsWith(rootNorm + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(Path.GetDirectoryName(exeNorm), rootNorm, StringComparison.OrdinalIgnoreCase))
                    result.Add(pid);
            }
            catch { /* 非法路径条目跳过 */ }
        }
        return result;
    }

    /// <summary>侧车模块启动命令行标记（与 MainWindow 启动侧车的 -m 参数一致）。</summary>
    private const string SidecarModuleMarker = "maaracing_master.core.sidecar";
}
