using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Security.Cryptography.X509Certificates;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;

namespace MaaRacingMaster.Shell;

/// <summary>
/// WebView2 运行时维护：查询已安装版本，按需下载微软官方引导程序并静默安装。
///
/// 边界（安全取舍）：
/// - 下载地址固定为本进程常量，不接受前端传入 URL / 路径；跳转后的最终主机必须是
///   microsoft.com 域，否则中止。
/// - 落盘后先校验 Authenticode（链有效 + 签名者为 Microsoft Corporation）再执行；
///   校验失败一律不运行，把处置权交回用户（打开官方页）。
/// - 宿主 manifest 声明 requireAdministrator，子进程继承提权 token，安装器无需二次 UAC。
/// - 安装只换运行时目录，不触碰用户数据；正在运行的界面仍用旧版本，重启后生效。
/// </summary>
internal static class WebView2RuntimeUpdater
{
    private const string BootstrapperUrl = "https://go.microsoft.com/fwlink/p/?linkid=2124703";
    private const string ExpectedSigner = "Microsoft Corporation";
    private const string RuntimeClientId = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";
    private const long MaxDownloadBytes = 8L * 1024 * 1024;
    private static readonly TimeSpan DownloadTimeout = TimeSpan.FromMinutes(5);
    private static readonly TimeSpan InstallTimeout = TimeSpan.FromMinutes(10);

    private static readonly Guid WinTrustAction =
        new("00AAC56B-CD44-11d0-8CC2-00C04FC295EE");

    private static int _inFlight;

    /// <summary>已安装的运行时常量版本；读取失败返回 null（不抛给调用方）。
    /// 优先读 EdgeUpdate 客户端登记（这是运行时本身），而不是 GetAvailableBrowserVersionString：
    /// 后者在装了 Edge 预览通道的机器上会返回预览版，和本程序实际使用的稳定运行时不是一回事。</summary>
    public static string? InstalledVersion()
    {
        var registered = ReadRegisteredVersion(Registry.LocalMachine,
                              @"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\" + RuntimeClientId)
                         ?? ReadRegisteredVersion(Registry.LocalMachine,
                              @"SOFTWARE\Microsoft\EdgeUpdate\Clients\" + RuntimeClientId)
                         ?? ReadRegisteredVersion(Registry.CurrentUser,
                              @"Software\Microsoft\EdgeUpdate\Clients\" + RuntimeClientId);
        if (registered is not null)
            return registered;
        try
        {
            var version = CoreWebView2Environment.GetAvailableBrowserVersionString();
            return string.IsNullOrWhiteSpace(version) ? null : version;
        }
        catch
        {
            return null;
        }
    }

    private static string? ReadRegisteredVersion(RegistryKey root, string path)
    {
        try
        {
            using var key = root.OpenSubKey(path);
            var pv = key?.GetValue("pv") as string;
            if (string.IsNullOrWhiteSpace(pv) || pv == "0.0.0.0")
                return null;
            return pv;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>下载落点：%TEMP%\MaaRacingMaster\webview2-&lt;guid&gt;；本次结束即删。
    /// 上次若因安装器占用删不掉，会在下一次更新前先清一遍，避免临时目录积累。</summary>
    private static void CleanupStaleTempDirs()
    {
        try
        {
            var root = Path.Combine(Path.GetTempPath(), "MaaRacingMaster");
            if (!Directory.Exists(root)) return;
            foreach (var dir in Directory.GetDirectories(root, "webview2-*"))
            {
                try
                {
                    Directory.Delete(dir, recursive: true);
                }
                catch
                {
                    // 残留文件被占用：下次再清
                }
            }
        }
        catch
        {
            // 临时目录不可读：不阻断更新
        }
    }

    /// <summary>下载并静默安装最新运行时；返回 before/after 供界面判断是否需要重启。</summary>
    public static async Task<(bool Ok, object? Data, string? Error)> UpdateAsync()
    {
        // 单飞：关闭弹层再打开可能触发第二次更新，两个安装器并发会互相破坏
        if (Interlocked.CompareExchange(ref _inFlight, 1, 0) != 0)
            return (false, null, "更新已在进行中，请稍候");

        CleanupStaleTempDirs();
        var before = InstalledVersion();
        var dir = Path.Combine(Path.GetTempPath(), "MaaRacingMaster",
            "webview2-" + Guid.NewGuid().ToString("N"));
        var exe = Path.Combine(dir, "MicrosoftEdgeWebview2Setup.exe");
        try
        {
            Directory.CreateDirectory(dir);
            using (var http = new HttpClient { Timeout = DownloadTimeout })
            using (var resp = await http.GetAsync(BootstrapperUrl,
                       HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false))
            {
                resp.EnsureSuccessStatusCode();
                var finalHost = resp.RequestMessage?.RequestUri?.Host ?? "";
                if (!finalHost.EndsWith(".microsoft.com", StringComparison.OrdinalIgnoreCase)
                    && !finalHost.Equals("microsoft.com", StringComparison.OrdinalIgnoreCase))
                {
                    return (false, null, "下载跳转到了非微软域名，已中止");
                }

                var declared = resp.Content.Headers.ContentLength;
                if (declared is > MaxDownloadBytes)
                    return (false, null, "安装包体积异常，已中止");

                await using var src = await resp.Content.ReadAsStreamAsync().ConfigureAwait(false);
                await using var dst = new FileStream(exe, FileMode.Create, FileAccess.Write,
                    FileShare.None, 81920, useAsync: true);
                var buffer = new byte[81920];
                long total = 0;
                int read;
                while ((read = await src.ReadAsync(buffer).ConfigureAwait(false)) > 0)
                {
                    total += read;
                    if (total > MaxDownloadBytes)
                        return (false, null, "安装包体积异常，已中止");
                    await dst.WriteAsync(buffer.AsMemory(0, read)).ConfigureAwait(false);
                }
            }

            if (!VerifyAuthenticode(exe, out var signer, out var verifyError))
            {
                ShellLog.Append($"[webview2] 安装包验签失败: {verifyError}（签名者 {signer}）");
                return (false, null, "安装包签名校验未通过，已拒绝执行");
            }

            ShellLog.Append("[webview2] 开始静默安装运行时");
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                Arguments = "/silent /install",
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var process = Process.Start(psi);
            if (process is null)
                return (false, null, "安装程序启动失败");
            using (var cts = new CancellationTokenSource(InstallTimeout))
            {
                try
                {
                    await process.WaitForExitAsync(cts.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return (false, null, "安装超时，请重试或打开官方页面手动安装");
                }
            }
            if (process.ExitCode != 0)
                return (false, null, $"安装程序返回错误码 {process.ExitCode}");

            // 安装器可能异步收尾，给注册表一点时间再读一次
            string? after = null;
            for (var i = 0; i < 10; i++)
            {
                await Task.Delay(600).ConfigureAwait(false);
                after = InstalledVersion();
                if (after is not null && !string.Equals(after, before, StringComparison.Ordinal))
                    break;
            }
            after ??= InstalledVersion();
            var changed = !string.IsNullOrEmpty(after)
                && !string.Equals(after, before, StringComparison.Ordinal);
            ShellLog.Append($"[webview2] 安装完成: {before ?? "未知"} -> {after ?? "未知"}");
            return (true, new { before, after, changed }, null);
        }
        catch (Exception ex)
        {
            ShellLog.Append($"[webview2] 更新失败: {ex.Message}");
            return (false, null, ex.Message);
        }
        finally
        {
            try
            {
                if (Directory.Exists(dir)) Directory.Delete(dir, recursive: true);
            }
            catch
            {
                // 安装器仍占用临时文件时忽略：系统临时目录会自行清理
            }
            Interlocked.Exchange(ref _inFlight, 0);
        }
    }

    private static bool VerifyAuthenticode(string path, out string signer, out string error)
    {
        signer = "";
        error = "";
        try
        {
            using var cert = new X509Certificate2(X509Certificate.CreateFromSignedFile(path));
            signer = cert.GetNameInfo(X509NameType.SimpleName, forIssuer: false);
            if (!string.Equals(signer, ExpectedSigner, StringComparison.Ordinal))
            {
                error = "签名者不是 Microsoft Corporation";
                return false;
            }
        }
        catch (Exception ex)
        {
            error = "读取签名失败: " + ex.Message;
            return false;
        }

        var fileInfo = new WinTrustFileInfo
        {
            cbStruct = (uint)Marshal.SizeOf<WinTrustFileInfo>(),
            pcwszFilePath = path,
        };
        var pFile = Marshal.AllocHGlobal(Marshal.SizeOf<WinTrustFileInfo>());
        var pData = Marshal.AllocHGlobal(Marshal.SizeOf<WinTrustData>());
        try
        {
            Marshal.StructureToPtr(fileInfo, pFile, fDeleteOld: false);
            var data = new WinTrustData
            {
                cbStruct = (uint)Marshal.SizeOf<WinTrustData>(),
                dwUIChoice = 2,              // WTD_UI_NONE
                fdwRevocationChecks = 0,     // WTD_REVOKE_NONE
                dwUnionChoice = 1,           // WTD_CHOICE_FILE
                pFile = pFile,
                dwStateAction = 1,           // WTD_STATEACTION_VERIFY
                dwProvFlags = 0x00000010,    // WTD_REVOCATION_CHECK_NONE：不查吊销，
                                             // 但允许按需取中间证书（我们本来就在线）
            };
            Marshal.StructureToPtr(data, pData, fDeleteOld: false);
            var result = WinVerifyTrust(IntPtr.Zero, WinTrustAction, pData);

            data.dwStateAction = 2;          // WTD_STATEACTION_CLOSE
            Marshal.StructureToPtr(data, pData, fDeleteOld: false);
            _ = WinVerifyTrust(IntPtr.Zero, WinTrustAction, pData);

            if (result != 0)
            {
                error = $"WinVerifyTrust 返回 0x{result:X8}";
                return false;
            }
            return true;
        }
        finally
        {
            Marshal.FreeHGlobal(pFile);
            Marshal.FreeHGlobal(pData);
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WinTrustFileInfo
    {
        public uint cbStruct;
        [MarshalAs(UnmanagedType.LPWStr)] public string pcwszFilePath;
        public IntPtr hFile;
        public IntPtr pgKnownSubject;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WinTrustData
    {
        public uint cbStruct;
        public IntPtr pPolicyCallbackData;
        public IntPtr pSIPClientData;
        public uint dwUIChoice;
        public uint fdwRevocationChecks;
        public uint dwUnionChoice;
        public IntPtr pFile;
        public uint dwStateAction;
        public IntPtr hWVTStateData;
        public IntPtr pwszURLReference;
        public uint dwProvFlags;
        public uint dwUIContext;
        public IntPtr pSignatureSettings;
    }

    [DllImport("wintrust.dll", PreserveSig = true, SetLastError = false)]
    private static extern uint WinVerifyTrust(
        IntPtr hwnd,
        [MarshalAs(UnmanagedType.LPStruct)] Guid pgActionID,
        IntPtr pWVTData);
}
