// DML 停顿 ETL 判读解码器（issue18 实验工具，非生产/测试加载）。
// 用法: dotnet run -- <etl> <tid> <slo> <shi>   或   dotnet run -- --raw <etl>
// --raw: source.AllEvents 原始 ProviderGuid 计数（分层采集 vs 解码）。
// 正常: 停顿窗调度三分(在跑/就绪/阻塞) + 采样IP模块归属 + GPU队列100ms桶时间线。
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;

static int Int(object o)
{
    switch (o)
    {
        case int i: return i;
        case uint u: return (int)u;
        case long l: return (int)l;
        case ulong ul: return (int)ul;
        case string s when int.TryParse(s, out var r): return r;
    }
    try { return Convert.ToInt32(o); } catch { return -1; }
}
static ulong U64(object o)
{
    switch (o)
    {
        case ulong u: return u;
        case long l: return (ulong)l;
        case uint u: return u;
        case int i: return (ulong)i;
        case string s when ulong.TryParse(s, out var r): return r;
    }
    try { return Convert.ToUInt64(o); } catch { return 0; }
}

var etl = args[0];
if (etl == "--raw")
{
    var path = args[1];
    var dxg = new Guid("802ec45a-1e99-4b83-9920-87c98277ba9d");
    var img = new Guid("b3e675d7-2554-4f18-830b-2762732560de");
    long nDxg = 0, nImg = 0, nAll = 0;
    var guidHist = new Dictionary<Guid, long>();
    using var rs = new ETWTraceEventSource(path);
    rs.AllEvents += e =>
    {
        nAll++;
        if (e.ProviderGuid == dxg) nDxg++;
        if (e.ProviderGuid == img) nImg++;
        guidHist[e.ProviderGuid] = guidHist.TryGetValue(e.ProviderGuid, out var c) ? c + 1 : 1;
    };
    rs.Process();
    Console.WriteLine($"total={nAll} DxgKrnl(802ec45a)={nDxg} ImageID(b3e675d7)={nImg}");
    foreach (var kv in guidHist.OrderByDescending(kv => kv.Value).Take(12))
        Console.WriteLine($"{kv.Value,10} {kv.Key}");
    return;
}
int T = int.Parse(args[1]);
var slo = DateTime.Parse(args[2]);
var shi = DateTime.Parse(args[3]);
var lo = slo.AddSeconds(-1.5);
var hi = shi.AddSeconds(1.5);

using var source = new ETWTraceEventSource(etl);
var switches = new List<(DateTime ts, bool inn, int other, string state, int reason, int cpu, int nwt)>();
var readies = new List<DateTime>();
var samples = new List<(DateTime ts, ulong ip)>();
var modules = new List<(ulong b, ulong e, string name)>();
var dynHist = new Dictionary<string, int>();
var gpuEv = new List<(DateTime ts, string name)>();

source.Dynamic.All += d =>
{
    if (d.TimeStamp < slo || d.TimeStamp > shi) return;
    var k = d.ProviderName + "/" + d.EventName;
    dynHist[k] = dynHist.TryGetValue(k, out var c) ? c + 1 : 1;
    if (d.ProviderName.Contains("DxgKrnl")) gpuEv.Add((d.TimeStamp, d.EventName));
};

source.Kernel.All += d =>
{
    if (d.EventName is "Image/DCStart" or "Image/Load")
    {
        ulong b = U64(d.PayloadByName("ImageBase"));
        ulong sz = U64(d.PayloadByName("ImageSize"));
        var fn = d.PayloadByName("FileName")?.ToString() ?? "?";
        if (b != 0 && sz != 0) modules.Add((b, b + sz, fn));
        return;
    }
    if (d.TimeStamp < lo || d.TimeStamp > hi) return;
    if (d.EventName == "Thread/CSwitch")
    {
        int nw = Int(d.PayloadValue(3));
        int od = Int(d.PayloadValue(0));
        if (nw != T && od != T) return;
        var st = d.PayloadValue(13)?.ToString() ?? "?";
        switches.Add((d.TimeStamp, nw == T, nw == T ? od : nw, st,
            Int(d.PayloadValue(11)), Int(d.PayloadValue(6)), Int(d.PayloadValue(15))));
    }
    else if (d.EventName == "Dispatcher/ReadyThread")
    {
        if (Int(d.PayloadValue(0)) == T) readies.Add(d.TimeStamp);
    }
    else if (d.EventName == "PerfInfo/Sample" && d.ThreadID == T)
    {
        samples.Add((d.TimeStamp, U64(d.PayloadValue(0))));
    }
};
source.Process();

double runMs = 0, readyMs = 0, waitMs = 0;
DateTime? runStart = null;
string? leftState = null;
DateTime leftAt = default;
foreach (var s in switches)
{
    if (s.inn)
    {
        if (leftState != null)
        {
            var a = leftAt < slo ? slo : leftAt;
            var b = s.ts > shi ? shi : s.ts;
            if (b > a)
            {
                if (leftState is "Ready" or "Standby" or "DeferredReady") readyMs += (b - a).TotalMilliseconds;
                else waitMs += (b - a).TotalMilliseconds;
            }
            leftState = null;
        }
        runStart = s.ts;
    }
    else
    {
        if (runStart is DateTime rs)
        {
            var a = rs < slo ? slo : rs;
            var b = s.ts > shi ? shi : s.ts;
            if (b > a) runMs += (b - a).TotalMilliseconds;
            runStart = null;
        }
        leftState = s.state;
        leftAt = s.ts;
    }
}
var inStall = switches.Where(s => s.ts >= slo && s.ts <= shi).ToList();
Console.WriteLine($"window={(shi - slo).TotalMilliseconds:F0}ms running={runMs:F1} ready={readyMs:F1} wait/other={waitMs:F1}");
Console.WriteLine($"samples in stall={samples.Count(t => t.ts >= slo && t.ts <= shi)} (1kHz/CPU); modules={modules.Count}");
var modHist = new Dictionary<string, int>();
foreach (var smp in samples.Where(t => t.ts >= slo && t.ts <= shi))
{
    var m = modules.FirstOrDefault(mm => smp.ip >= mm.b && smp.ip < mm.e);
    var key = m.name == null ? $"unmapped(0x{smp.ip:X})" : System.IO.Path.GetFileName(m.name);
    modHist[key] = modHist.TryGetValue(key, out var c) ? c + 1 : 1;
}
Console.WriteLine("--- sample IP module attribution (stall window) ---");
foreach (var kv in modHist.OrderByDescending(kv => kv.Value).Take(15))
    Console.WriteLine($"{kv.Value,6} {kv.Key}");
var outHist = new Dictionary<string, int>();
var reasonHist = new Dictionary<int, int>();
foreach (var s in inStall.Where(s => !s.inn))
{
    outHist[s.state] = outHist.TryGetValue(s.state, out var c) ? c + 1 : 1;
    reasonHist[s.reason] = reasonHist.TryGetValue(s.reason, out var r) ? r + 1 : 1;
}
Console.WriteLine("OUT states: " + string.Join(", ", outHist.OrderByDescending(kv => kv.Value).Select(kv => $"{kv.Key}={kv.Value}")));
Console.WriteLine("OUT wait reasons: " + string.Join(", ", reasonHist.OrderByDescending(kv => kv.Value).Select(kv => $"{kv.Key}={kv.Value}")));
Console.WriteLine("--- GPU queue timeline (100ms buckets: DmaPacket=exec / QueuePacket.Start=submit) ---");
for (var t = slo; t < shi; t = t.AddMilliseconds(100))
{
    var t2 = t.AddMilliseconds(100);
    int exec = gpuEv.Count(g => g.ts >= t && g.ts < t2 && g.name == "DmaPacket");
    int sub = gpuEv.Count(g => g.ts >= t && g.ts < t2 && g.name == "QueuePacket/Start");
    Console.WriteLine($"{t:HH:mm:ss.fff} exec={exec,4} submit={sub,4}");
}
Console.WriteLine("--- dynamic providers in stall window ---");
foreach (var kv in dynHist.OrderByDescending(kv => kv.Value).Take(12))
    Console.WriteLine($"{kv.Value,8} {kv.Key}");