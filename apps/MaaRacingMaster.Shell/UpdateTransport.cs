using System;
using System.IO;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;

namespace MaaRacingMaster.Shell;

/// <summary>
/// 更新传输面的最小接口：只取文本（版本标记与清单）和下载文件（整包/增量）。
/// 抽出接口是为了让合同探针用本地目录走同一条代码路径，而不是另写一套假实现。
/// </summary>
public interface IUpdateTransport
{
    Task<string> GetStringAsync(Uri uri, CancellationToken cancellationToken);

    Task DownloadAsync(Uri uri, string destination, IProgress<long>? bytes,
                       CancellationToken cancellationToken);
}

/// <summary>HTTP 传输：小文件一次读完，大文件流式落盘并回报累计字节。</summary>
public sealed class HttpUpdateTransport : IUpdateTransport, IDisposable
{
    private readonly HttpClient _http;

    public HttpUpdateTransport(TimeSpan? timeout = null, HttpMessageHandler? handler = null)
    {
        _http = handler is null ? new HttpClient() : new HttpClient(handler, disposeHandler: false);
        _http.Timeout = timeout ?? TimeSpan.FromMinutes(10);
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("MaaRacingMaster-Updater");
    }

    public async Task<string> GetStringAsync(Uri uri, CancellationToken cancellationToken)
    {
        using var response = await _http
            .GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
    }

    public async Task DownloadAsync(Uri uri, string destination, IProgress<long>? bytes,
                                    CancellationToken cancellationToken)
    {
        using var response = await _http
            .GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        await using var source = await response.Content.ReadAsStreamAsync(cancellationToken)
            .ConfigureAwait(false);
        await CopyWithProgressAsync(source, destination, bytes, cancellationToken).ConfigureAwait(false);
    }

    internal static async Task CopyWithProgressAsync(Stream source, string destination,
                                                     IProgress<long>? bytes,
                                                     CancellationToken cancellationToken)
    {
        await using var target = new FileStream(destination, FileMode.Create, FileAccess.Write,
            FileShare.None, 81920, useAsync: true);
        var buffer = new byte[81920];
        long total = 0;
        int read;
        while ((read = await source.ReadAsync(buffer, cancellationToken).ConfigureAwait(false)) > 0)
        {
            await target.WriteAsync(buffer.AsMemory(0, read), cancellationToken).ConfigureAwait(false);
            total += read;
            bytes?.Report(total);
        }
    }

    public void Dispose() => _http.Dispose();
}

/// <summary>本地目录传输：合同探针与离线演练使用，路径来自 <c>file://</c> URI。</summary>
public sealed class FileUpdateTransport : IUpdateTransport
{
    public async Task<string> GetStringAsync(Uri uri, CancellationToken cancellationToken)
    {
        return await File.ReadAllTextAsync(uri.LocalPath, cancellationToken).ConfigureAwait(false);
    }

    public async Task DownloadAsync(Uri uri, string destination, IProgress<long>? bytes,
                                    CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        await using var source = File.OpenRead(uri.LocalPath);
        await HttpUpdateTransport.CopyWithProgressAsync(source, destination, bytes, cancellationToken)
            .ConfigureAwait(false);
    }
}
