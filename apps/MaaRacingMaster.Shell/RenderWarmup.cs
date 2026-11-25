using System;
using System.IO;
using System.Text.Json;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 首次启动预热判定与完成标记。
/// GPU 光栅下，首访的绘制组合要现场编译着色器（100–250ms 帧停顿）；冷启动时前端在
/// 启动遮罩后把各页面与重弹窗先画一遍，把编译集中到遮罩期间。标记按「WebView2 运行时
/// 版本 + 壳版本」记住：换程序版本、换运行时或着色器缓存被清掉都会重新走一次预热。
/// 标记文件放用户数据目录根（浏览器不会碰它），内容只有两个版本号与时间。
/// </summary>
public static class RenderWarmup
{
    private const string MarkerName = ".render-warmup.json";
    // 着色器缓存低于这个量级就当作冷态（用户清过缓存或刚安装）：512KB 远低于正常
    // 预热后的数 MB，同时能容忍缓存目录里只有索引文件的空壳。
    private const long MinGpuCacheBytes = 512 * 1024;
    // 但字节数不能单独作判据：磁盘缓存的 data_* 块可以在一条有效条目都没有时仍占几百
    // KB，只看字节数会把「清过缓存、只剩空壳块」判成热启动（实测：仅剩 ShaderCache 的
    // data_0..3 + index 时已超过上面的阈值）。所以再要求条目文件够数。
    private const int MinGpuCacheEntries = 8;

    /// <summary>是否需要显示启动预热遮罩（找不到标记、版本变化、缓存过小都算冷启动）。</summary>
    public static bool IsCold(string? udf, string? runtimeVersion, string? appVersion)
    {
        if (string.IsNullOrWhiteSpace(udf)) return false;
        try
        {
            var cacheBytes = GpuCacheBytes(udf, out var cacheEntries);
            if (cacheEntries < MinGpuCacheEntries || cacheBytes < MinGpuCacheBytes) return true;
            var marker = Path.Combine(udf, MarkerName);
            if (!File.Exists(marker)) return true;
            using var doc = JsonDocument.Parse(File.ReadAllText(marker));
            var root = doc.RootElement;
            return !TextEquals(root, "runtime", runtimeVersion)
                || !TextEquals(root, "app", appVersion);
        }
        catch (Exception ex)
        {
            // 判定失败按冷启动处理：多发一次预热遮罩，比带着冷缓存进入界面好。
            ShellLog.Append($"[warmup] 冷启动判定失败，按冷启动处理: {ex.Message}");
            return true;
        }
    }

    /// <summary>前端报告预热完成后落标记；同一版本的下一次启动不再显示遮罩。</summary>
    public static void MarkDone(string? udf, string? runtimeVersion, string? appVersion)
    {
        if (string.IsNullOrWhiteSpace(udf)) return;
        try
        {
            Directory.CreateDirectory(udf);
            var payload = JsonSerializer.Serialize(new
            {
                runtime = runtimeVersion ?? "",
                app = appVersion ?? "",
                at = DateTime.UtcNow.ToString("O"),
            });
            File.WriteAllText(Path.Combine(udf, MarkerName), payload);
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[warmup] 预热标记写入失败: {ex.Message}");
        }
    }

    private static bool TextEquals(JsonElement root, string name, string? expected)
    {
        if (!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
            return false;
        return string.Equals(value.GetString(), expected ?? "", StringComparison.Ordinal);
    }

    // GPU 着色器缓存总量与条目数：GrShaderCache（Skia 程序）与 ShaderCache（ANGLE 程序）
    // 两处。条目是 f_* 文件；data_* / index 只是块与索引，清过条目后仍会留下。
    private static long GpuCacheBytes(string udf, out int entries)
    {
        long total = 0;
        var count = 0;
        foreach (var name in new[] { "GrShaderCache", "ShaderCache" })
        {
            var dir = Path.Combine(udf, "EBWebView", name);
            if (!Directory.Exists(dir)) continue;
            foreach (var file in Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories))
            {
                if (Path.GetFileName(file).StartsWith("f_", StringComparison.Ordinal)) count++;
                try
                {
                    total += new FileInfo(file).Length;
                }
                catch (IOException)
                {
                    // 正被浏览器占用：这一份不计入，不影响判定阈值。
                }
            }
        }
        entries = count;
        return total;
    }
}
