using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.UI.Xaml.Controls;
using Microsoft.Web.WebView2.Core;

namespace MaaRacingMaster.Shell;

/// <summary>
/// HTML ↔ C# ↔ Python sidecar 的 RPC 转发（主窗口与 PEEP 悬浮窗共用）。
///
/// 之所以能共用：回发按 <c>sender.CoreWebView2</c> 定位，不依赖任何窗口实例字段，
/// 因此每个 WebView2 注册同一套处理即可各自收到自己的响应，不存在串台。
/// sidecar 始终是单进程单通道（<see cref="PythonSidecar"/> 内部有写锁 + 按 id 匹配 pending），
/// 多个窗口并发发请求是安全的。
/// </summary>
internal static class RpcBridge
{
    /// <summary>
    /// 前端通道方法白名单 = 业务方法（与 sidecar.py 的 HANDLERS 同源，扣除壳直发通道）
    /// ∪ 宿主更新动作（壳自有能力，不经 sidecar）。所有者分离：宿主集与业务集不交叠。
    /// 三方一致性由 tests/test_rpc_allowlist.py 锁（前端 JS 亦在断言范围内）。
    /// </summary>
    private static readonly HashSet<string> AllowedMethods = new(StringComparer.Ordinal)
    {
        "get_initial_state", "set_run_queue", "set_start_from", "get_status", "start", "stop",
        "get_module_config", "set_module_config",
        "open_vigembus_download", "open_external_url", "open_user_data_folder",
        "install_asset_start", "install_asset_cancel", "install_asset_status",
        "get_data_usage",
        "get_registry_optimizations", "set_optimization_prompt_ignored",
        "set_registry_optimization",
        "get_optional_dependencies",
        "check_update", "fetch_announcement",
        "fetch_logs", "get_module_dashboard", "get_debug_state",
        "set_debug_mode", "set_file_logging", "set_peep", "get_peep_frame",
        "set_emergency_stop", "set_click_mode", "set_intent_mode",
        "set_mute_game", "pick_game_exe",
        "set_boost_priority",
        "get_achievements", "set_achievement_notify", "note_ui_event",
        "export_achievements", "import_achievements", "reset_achievements",
        // 宿主更新动作（UpdateCoordinator）：固定意图，不携带 URL/路径；设置类只带
        // 枚举值（stable/preview、cnb/github），由协调器校验，前端不传地址。
        "update_check", "update_start_download", "update_cancel_download",
        "update_status", "update_apply",
        "update_set_channel", "update_set_source",
        // 宿主运行环境维护（WebView2RuntimeUpdater）：下载源固定为微软官方跳转，
        // 前端只表达「查状态 / 更新」两个意图，不携带任何 URL 或路径。
        "webview2_status", "webview2_update",
        // 计划内重启（AppRestarter）：等本进程退出后由同构等待进程拉起新实例。
        "app_restart",
    };

    /// <summary>宿主更新动作集（壳自有，处理不经 sidecar；后端不可用不影响它们）。
    /// 与 sidecar HANDLERS 的分离由 tests/test_rpc_allowlist.py 锁定。</summary>
    private static readonly HashSet<string> HostMethods = new(StringComparer.Ordinal)
    {
        "update_check", "update_start_download", "update_cancel_download",
        "update_status", "update_apply",
        "update_set_channel", "update_set_source",
        "webview2_status", "webview2_update",
        "app_restart",
    };

    /// <summary>
    /// 会弹系统对话框的方法给长等待：用户在保存/选择框里磨多久，管道就等多久。
    /// 写死 10 秒会造出「后端已生效、前端报超时」——导入/重置尤其危险（用户一重试
    /// 就盖掉 pre-import 备份）。其余方法维持 10 秒，不把真卡死拖成长等待。
    /// </summary>
    private static readonly HashSet<string> LongCallMethods = new(StringComparer.Ordinal)
    {
        "pick_game_exe", "export_achievements", "import_achievements",
    };

    private static TimeSpan TimeoutFor(string method) =>
        LongCallMethods.Contains(method) ? TimeSpan.FromMinutes(5) : TimeSpan.FromSeconds(10);

    private static PythonSidecar? s_sidecar;
    private static UpdateCoordinator? s_coordinator;

    /// <summary>绑定/解绑业务后端。窗口启动时绑定，sidecar 关闭后置 null（此后请求回 backend unavailable）。</summary>
    public static void Attach(PythonSidecar? sidecar) => s_sidecar = sidecar;

    /// <summary>绑定宿主更新协调器（窗口启动时绑定；宿主动作不经业务后端）。</summary>
    public static void AttachCoordinator(UpdateCoordinator? coordinator) => s_coordinator = coordinator;

    /// <summary>
    /// 尝试接管一条前端消息：只处理 <c>type=call</c>，其余消息类型留给各窗口自行处理。
    /// </summary>
    /// <returns>已接管返回 true。</returns>
    public static bool TryHandleCall(WebView2 sender, JsonElement root)
    {
        if (!root.TryGetProperty("type", out var t) || t.GetString() != "call")
            return false;

        var callId = root.GetProperty("callId").GetInt64();
        var method = root.GetProperty("method").GetString() ?? "";
        // 信任边界前置：非白名单方法直接合成错误响应，不转发 sidecar。
        if (!AllowedMethods.Contains(method))
        {
            _ = ReplyAsync(sender, callId, false, null, $"method not allowed: {method}");
            return true;
        }
        var paramsEl = root.TryGetProperty("params", out var p) && p.ValueKind != JsonValueKind.Null
            ? p
            : (JsonElement?)null;
        if (HostMethods.Contains(method))
        {
            // 宿主动作：壳自有能力，与业务后端解耦（backend unavailable 不影响更新状态查询）
            _ = HandleHostCallAsync(sender, callId, method, paramsEl);
            return true;
        }
        _ = HandleCallAsync(sender, callId, method, paramsEl);
        return true;
    }

    private static async Task HandleHostCallAsync(WebView2 sender, long callId, string method,
                                                  JsonElement? paramsEl)
    {
        // 运行环境维护不依赖更新协调器：后端/协调器不可用时仍能查版本与修复运行时。
        if (method is "webview2_status" or "webview2_update")
        {
            await HandleWebView2CallAsync(sender, callId, method).ConfigureAwait(false);
            return;
        }

        // 计划内重启：先回复再由窗口延迟关窗，等待进程负责在退出后拉起新实例。
        if (method == "app_restart")
        {
            var (restartOk, restartError) = AppRestarter.Schedule();
            await ReplyAsync(sender, callId, restartOk, null, restartError);
            return;
        }

        UpdateSnapshot data;
        string? error = null;
        var ok = true;
        var coordinator = s_coordinator;
        if (coordinator is null)
        {
            ok = false;
            error = "更新协调器不可用";
        }
        else
        {
            try
            {
                data = method switch
                {
                    "update_check" => await coordinator.CheckAsync(),
                    "update_start_download" => await coordinator.StartDownloadAsync(),
                    "update_cancel_download" => coordinator.CancelDownload(),
                    "update_apply" => await coordinator.ApplyAsync(),
                    "update_set_channel" => await coordinator.SetChannelAsync(ReadParam(paramsEl, "value")),
                    "update_set_source" => await coordinator.SetSourceAsync(ReadParam(paramsEl, "value")),
                    _ => coordinator.GetStatus(), // update_status 与未知宿主名兜底
                };
            }
            catch (Exception ex)
            {
                ok = false;
                data = coordinator.GetStatus();
                error = ex.Message;
            }
            await ReplyAsync(sender, callId, ok, ok ? data : null, error);
            return;
        }
        await ReplyAsync(sender, callId, ok, null, error);
    }

    /// <summary>WebView2 运行环境：查已安装版本 vs 当前界面实际使用的版本，或下载安装最新版。
    /// 正在运行的界面不会因安装而切换版本，前端据此提示重启。</summary>
    private static async Task HandleWebView2CallAsync(WebView2 sender, long callId, string method)
    {
        string? running = null;
        try
        {
            running = sender.CoreWebView2?.Environment.BrowserVersionString;
        }
        catch
        {
            // 窗口关闭边缘读取失败只影响「待重启」判断，不阻断状态查询
        }

        if (method == "webview2_status")
        {
            var installed = WebView2RuntimeUpdater.InstalledVersion();
            if (installed is null)
            {
                await ReplyAsync(sender, callId, false, null, "无法读取 WebView2 运行时版本");
                return;
            }
            var health = WebView2RuntimeUpdater.UpdateChannelHealth();
            var data = new
            {
                installed,
                running,
                lastCheck = health.LastCheck?.ToString("yyyy-MM-dd"),
                checkAgeDays = health.AgeDays,
                channelStale = health.Stale,
            };
            await ReplyAsync(sender, callId, true, data, null);
            return;
        }

        var result = await WebView2RuntimeUpdater.UpdateAsync();
        await ReplyAsync(sender, callId, result.Ok, result.Data, result.Error);
    }

    private static async Task HandleCallAsync(WebView2 sender, long callId, string method, JsonElement? paramsEl)
    {
        object? data = null;
        string? error = null;
        var ok = true;
        try
        {
            if (s_sidecar is null)
                throw new InvalidOperationException("backend unavailable");
            var resp = await s_sidecar.CallAsync(method, paramsEl, TimeoutFor(method));
            data = resp.GetProperty("data"); // JsonElement：null 或对象直接嵌入回传
        }
        catch (Exception ex)
        {
            ok = false;
            error = ex.Message;
        }

        await ReplyAsync(sender, callId, ok, data, error);
    }

    private static string? ReadParam(JsonElement? paramsEl, string name)
    {
        // 畸形 params（数组/标量/null）显式判为「无此参数」，不靠 TryGetProperty 抛异常
        // 再被外层 catch 兜成错误响应——非法值一律走协调器的拒绝分支。
        if (paramsEl is null || paramsEl.Value.ValueKind != JsonValueKind.Object) return null;
        return paramsEl.Value.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString()
            : null;
    }

    private static Task ReplyAsync(WebView2 sender, long callId, bool ok, object? data, string? error)
    {
        try
        {
            var reply = JsonSerializer.Serialize(new { type = "response", callId, ok, data, error });
            sender.CoreWebView2.PostWebMessageAsJson(reply); // 同步 API
        }
        catch (Exception ex)
        {
            // 窗口关闭边缘 WebView 可能已释放，忽略即可
            Console.Error.WriteLine($"[shell] 回传 JS 失败: {ex.Message}");
        }
        return Task.CompletedTask;
    }
}
