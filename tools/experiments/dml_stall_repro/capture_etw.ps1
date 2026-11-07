#Requires -RunAsAdministrator
<#
.SYNOPSIS
提权后跑：用 ETW（CPU + GPU）包裹一轮可复现回放，抓到的 .etl 用于区分
「DML 提交链 CPU 工作被争抢拉长」与「GPU 不执行导致 CPU 自旋」。

.DESCRIPTION
普通 shell 起 wpr 会报 0xc5585011（需管理员）。本脚本须在有管理员权限的
PowerShell 里跑。它先起 CPU+GPU 采集，再跑 replay_business.py（live 真采集 +
真识别负载）；回放抓到停顿现场即自行退出，随后停采集落 .etl。

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
$etl = Join-Path $env:TEMP ("maar_stall_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".etl")

Write-Host "起 ETW 采集（CPU + GPU）..."
wpr -cancel 2>$null | Out-Null   # 清幽灵会话：不清则 start 撞 already-running、stop 落空
wpr -start CPU -start GPU -filemode
if ($LASTEXITCODE -ne 0) { throw "wpr 启动失败（确认已提权）" }
try {
    $capStart = Get-Date
    for ($r = 1; $r -le 4; $r++) {
        Write-Host "跑可复现回放轮 $r（抓到停顿即停本轮）..."
        & $py $replay --capture live --graph real --seconds 240 `
            --background "ETW 包裹轮 $r：游戏开在驾驶页自动巡航"
        $new = Get-ChildItem (Join-Path $env:APPDATA "MaaRacingMaster\logs\*\trace\stall_*.txt") |
            Where-Object { $_.LastWriteTime -gt $capStart }
        if ($new) { Write-Host "轮 $r 抓到停顿，停采集"; break }
        Write-Host "轮 $r 未复发，继续下一轮"
    }
} finally {
    Write-Host "停采集，落 $etl ..."
    wpr -stop $etl
}
Write-Host ""
Write-Host "ETL: $etl"
Write-Host "解码: tracerpt `"$etl`" -o `"$etl.csv`" -of CSV -y"
$stall = Get-ChildItem (Join-Path $env:APPDATA "MaaRacingMaster\logs\*\trace\stall_*.txt") |
    Sort-Object LastWriteTime | Select-Object -Last 1
if ($stall) { Write-Host "最新现场: $($stall.FullName)" }
Stop-Transcript | Out-Null