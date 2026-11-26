// 更新事务演练的假壳（见 UpdateDrillShell.csproj）。
// 行为只由安装目录 .update 下的两个文件决定：
//   drill-mode.txt = "ack"（默认）→ 写健康回执后退出；"hang" → 不写回执、保持运行。
//   transaction.txt 的 target → 健康回执里的版本号。
// 所有路径都从 MAARM_UPDATE_INSTALL_ROOT 取（启动器下发），缺失时从自身位置向上两级推导。

var install = Environment.GetEnvironmentVariable("MAARM_UPDATE_INSTALL_ROOT");
if (string.IsNullOrWhiteSpace(install))
{
    var dir = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
    install = Path.GetFullPath(Path.Combine(dir, "..", ".."));
}

var updateDir = Path.Combine(install, ".update");
Directory.CreateDirectory(updateDir);
File.WriteAllText(Path.Combine(updateDir, "drill-started.txt"), DateTime.UtcNow.ToString("O"));
File.WriteAllText(Path.Combine(updateDir, "drill-shell-pid.txt"), Environment.ProcessId.ToString());
// 追加而不是覆盖：回滚后会再拉起一次旧版，脚本要能分别核对「被杀掉的那个」与「重启的那个」。
File.AppendAllText(Path.Combine(updateDir, "drill-shell-pids.txt"),
    Environment.ProcessId + "\n");

var modePath = Path.Combine(updateDir, "drill-mode.txt");
var mode = File.Exists(modePath) ? File.ReadAllText(modePath).Trim() : "ack";
if (string.Equals(mode, "hang", StringComparison.OrdinalIgnoreCase))
{
    Thread.Sleep(TimeSpan.FromMinutes(10));
    return 3;
}

var target = string.Empty;
var txPath = Path.Combine(updateDir, "transaction.txt");
if (File.Exists(txPath))
{
    foreach (var line in File.ReadLines(txPath))
    {
        if (line.StartsWith("target=", StringComparison.OrdinalIgnoreCase))
        {
            target = line["target=".Length..].Trim();
            break;
        }
    }
}

if (target.Length > 0)
    File.WriteAllText(Path.Combine(updateDir, "health.txt"), "version=" + target + "\n");

// 留一点时间让启动器等得到回执，再正常退出。
Thread.Sleep(500);
return 0;
