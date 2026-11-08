#Requires -RunAsAdministrator
<#
.SYNOPSIS
提权后跑：用 ETW（CPU 调度 + DxgKrnl）包裹可复现回放，抓到的 .etl 用于区分
「DML 提交链 CPU 工作被争抢拉长」与「GPU 不执行导致 CPU 自旋」。

.DESCRIPTION
普通 shell 起 wpr 会报 0xc5585011（需管理员）。本脚本须在有管理员权限的
PowerShell 里跑。采集走自定义 profile 的内存环形缓冲（dml_stall.wprp，共 2GB）：
录制期间不落盘，stop 才写 .etl——内置 profile 加 -filemode 实测 2 分钟写 15GB，
小余量磁盘扛不住。回放抓到停顿现场即立即停采集（不等回放收尾，防环形覆盖）；
四轮都没抓到则 cancel 不落盘。

用法（管理员 PowerShell，仓库根或任意目录）：
  powershell -ExecutionPolicy Bypass -File tools\experiments\dml_stall_repro\capture_etw.ps1

产物：
  %TEMP%\maar_stall_<时刻>.etl   （CPU 调度 + DxgKrnl GPU 提交/执行事件）
  回放自己的 logs/<会话>/trace/stall_*.txt （含线程 CPU 时间双采样与栈）

解码（无需 ADK，系统自带 tracerpt）：
  tracerpt "<etl>" -o "<etl>.csv" -of CSV -y
随后按回放会话墙钟窗口过滤 DxgKrnl 的提交/执行事件与停顿线程 tid 的调度事件。
#>
$ErrorActionPreference = "Stop"
$log = Join-Path $env:TEMP "maar_etw_run.log"
Start-Transcript -Path $log -Force | Out-Null
$repo = (Get-Item $PSScriptRoot).Parent.Parent.Parent.FullName
$py = Join-Path $repo ".venv\Scripts\python.exe"
$replay = Join-Path $PSScriptRoot "replay_business.py"
$wprp = Join-Path $PSScriptRoot "dml_stall.wprp"
$etl = Join-Path $env:TEMP ("maar_stall_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".etl")

Write-Host "起 ETW 采集（内存环形缓冲：CPU 调度 + DxgKrnl）..."
# 清幽灵会话：不清则 start 撞 already-running、stop 落空。
# 无会话时 cancel 走 stderr，EAP=Stop 下会把脚本当场终止，故这一段降为 Continue。
$ErrorActionPreference = "Continue"
wpr -cancel 2>&1 | Out-Null
$ErrorActionPreference = "Stop"
wpr -start "$wprp!DmlStall.Verbose"
if ($LASTEXITCODE -ne 0) { throw "wpr 启动失败（确认已提权）" }
$status = (wpr -status 2>&1) -join "`n"
Write-Host $status
if ($status -match "not recording") { throw "wpr start 返回 0 但并未在录制：$status" }

$captured = $false
try {
    $capStart = Get-Date
    for ($r = 1; $r -le 4; $r++) {
        Write-Host "跑可复现回放轮 $r（抓到停顿立即停采集）..."
        $rlog = Join-Path $env:TEMP ("maar_replay_round_" + $r + ".log")
        $proc = Start-Process -FilePath $py -ArgumentList @($replay, "--capture", "live",
            "--graph", "real", "--seconds", "240",
            "--background", "ETW 包裹轮 $r：游戏开在驾驶页自动巡航") `
            -NoNewWindow -PassThru -RedirectStandardOutput $rlog -RedirectStandardError ($rlog + ".err")
        # 240s 回放 + 收尾宽限；超时就杀——实测收尾可能挂死，不能让它拖住整轮采集
        $deadline = (Get-Date).AddSeconds(330)
        while (-not $proc.HasExited) {
            Start-Sleep -Milliseconds 500
            $new = Get-ChildItem (Join-Path $env:APPDATA "MaaRacingMaster\logs\*\trace\stall_*.txt") |
                Where-Object { $_.LastWriteTime -gt $capStart }
            if ($new) {
                Write-Host "轮 $r 抓到停顿，立即停采集（不等回放收尾，防环形覆盖）"
                wpr -stop $etl
                if ($LASTEXITCODE -ne 0) { throw "wpr stop 失败" }
                $captured = $true
                break
            }
            if ((Get-Date) -gt $deadline) {
                Write-Host "轮 $r 超时（收尾疑似挂死），杀进程进下一轮"
                Stop-Process -Id $proc.Id -Force
                break
            }
        }
        if ($captured) {
            if (-not $proc.HasExited) { $proc.WaitForExit(60000) | Out-Null }
            if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
            break
        }
        Write-Host "轮 $r 未复发（退出码 $($proc.ExitCode)），继续下一轮"
    }
} finally {
    if (-not $captured) {
        $ErrorActionPreference = "Continue"
        wpr -cancel 2>&1 | Out-Null
        $ErrorActionPreference = "Stop"
        Write-Host "全程未抓到停顿，已取消采集（不落 ETL）"
    }
}
if ($captured) {
    Write-Host ""
    Write-Host "ETL: $etl"
    Write-Host "解码: tracerpt `"$etl`" -o `"$etl.csv`" -of CSV -y"
    $stall = Get-ChildItem (Join-Path $env:APPDATA "MaaRacingMaster\logs\*\trace\stall_*.txt") |
        Sort-Object LastWriteTime | Select-Object -Last 1
    if ($stall) { Write-Host "最新现场: $($stall.FullName)" }
}
Stop-Transcript | Out-Null