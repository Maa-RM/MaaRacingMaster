#Requires -RunAsAdministrator
<#
.SYNOPSIS
生产模式 ETW 抓取：不跑回放，只监视真实对局的停顿现场标记，命中即停采集落 ETL。

.DESCRIPTION
与 capture_etw.ps1（包裹离线回放）互补：本脚本面向生产故障机制核对。起内存环形
缓冲（dml_stall.wprp，录制期不落盘、体量有界），轮询生产日志目录的 stall_*.txt；
一旦出现新现场立即 wpr -stop，保存故障前后窗口，并把停采前的 Dropped event 计数与
时间边界写进 sidecar，供判读时校验「三分是否完整、是否把丢事件当完整」。

用法（管理员 PowerShell，或经 Start-Process -Verb RunAs 拉起）：
  powershell -ExecutionPolicy Bypass -File tools\experiments\dml_stall_repro\capture_production_etw.ps1
  可选 -MaxMinutes <n> 限制守候时长（默认 300），超时 cancel 不落盘。

产物：%TEMP%\maar_prod_stall_<时刻>.etl + 同名 .sidecar.txt
判读：TraceEvent 临时工程（见 README「解码工具事实」），整窗三分 + wait reason +
DxgKrnl 队列对齐；sidecar 的 dropped / span 用于边界校验。
#>
param([int]$MaxMinutes = 300)
$ErrorActionPreference = "Stop"
$log = Join-Path $env:TEMP "maar_prod_etw_run.log"
Start-Transcript -Path $log -Force | Out-Null
$repo = (Get-Item $PSScriptRoot).Parent.Parent.Parent.FullName
$wprp = Join-Path $PSScriptRoot "dml_stall.wprp"
$etl = Join-Path $env:TEMP ("maar_prod_stall_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".etl")

Write-Host "起生产 ETW（内存环形缓冲，守候上限 $MaxMinutes 分钟）..."
$ErrorActionPreference = "Continue"
wpr -cancel 2>&1 | Out-Null
$ErrorActionPreference = "Stop"
wpr -start "$wprp!DmlStall.Verbose" -start GPU.light
if ($LASTEXITCODE -ne 0) { throw "wpr 启动失败（确认已提权）" }
$status = (wpr -status 2>&1) -join "`n"
if ($status -match "not recording") { throw "wpr start 返回 0 但并未在录制：$status" }
Write-Host $status

$captured = $false
$capStart = Get-Date
$deadline = $capStart.AddMinutes($MaxMinutes)
try {
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $new = Get-ChildItem (Join-Path $env:APPDATA "MaaRacingMaster\logs\*\trace\stall_*.txt") |
            Where-Object { $_.LastWriteTime -gt $capStart } |
            Sort-Object LastWriteTime | Select-Object -First 1
        if ($new) {
            $stopStatus = (wpr -status 2>&1) -join "`n"
            Write-Host "命中生产停顿 $($new.Name)，停采集"
            wpr -stop $etl
            if ($LASTEXITCODE -ne 0) { throw "wpr stop 失败" }
            $captured = $true
            $side = $etl + ".sidecar.txt"
            @(
                "capStart=$($capStart.ToString('o'))",
                "stopAt=$((Get-Date).ToString('o'))",
                "stallFile=$($new.FullName)",
                "--- wpr status at stop (dropped / mode) ---",
                $stopStatus
            ) | Set-Content -Path $side -Encoding UTF8
            break
        }
    }
} finally {
    if (-not $captured) {
        $ErrorActionPreference = "Continue"
        wpr -cancel 2>&1 | Out-Null
        $ErrorActionPreference = "Stop"
        Write-Host "守候超时未命中生产停顿，已取消采集（不落 ETL）"
    }
}
if ($captured) {
    Write-Host "ETL: $etl"
    Write-Host "sidecar: $etl.sidecar.txt"
}
Stop-Transcript | Out-Null