# 更新事务演练：用正式发布包 + 假壳跑启动器的三条链。
#
# 用例 1 正常升级：暂存树就位 → 试启动 → 假壳写健康回执 → 事务与备份清理，且假壳在 30 秒内启动。
# 用例 2 无回执：假壳保持运行 → 启动器杀进程、旧树原样恢复、回滚后重新拉起旧版。
# 用例 3 中断恢复：旧目录已移走（swapping）与新目录已就位（swapped、无回执）两种断电现场，
#        确认普通启动能把旧树恢复出来并从原入口启动。
#
# 不进发布产物；只由 package-test.yml 的手动触发调用。

param(
    [Parameter(Mandatory = $true)][string]$PackageZip,
    [Parameter(Mandatory = $true)][string]$StubDir,
    [Parameter(Mandatory = $true)][string]$WorkRoot
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.IO.Compression

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '演练需要管理员身份：启动器清单要求提升，非管理员会弹 UAC 卡住'
}

function Assert([bool]$condition, [string]$message) {
    if (-not $condition) { throw "断言失败: $message" }
    Write-Host "  [PASS] $message"
}

function Write-Utf8([string]$path, [string]$text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false)))
}

function New-Install([string]$name) {
    $dir = Join-Path $WorkRoot $name
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($PackageZip, $dir)
    $appDir = Join-Path $dir 'program\app'
    if (-not (Test-Path $appDir)) { throw "发布包缺 program\app: $dir" }
    Copy-Item (Join-Path $StubDir '*') $appDir -Recurse -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $dir '.update') | Out-Null
    $script:drillInstalls += $dir
    return $dir
}

function Set-DrillMode([string]$install, [string]$mode) {
    Write-Utf8 (Join-Path $install '.update\drill-mode.txt') $mode
}

function Write-Tx([string]$install, [string]$state, [string]$target) {
    $updateDir = Join-Path $install '.update'
    New-Item -ItemType Directory -Force -Path $updateDir | Out-Null
    $text = @(
        "state=$state"
        "install=$install"
        "staging=$(Join-Path $updateDir 'staging')"
        "backup=$(Join-Path $updateDir 'backup')"
        "target=$target"
        "shell_pid=0"
        "launcher_pid=0"
        "watcher_pid=0"
    ) -join "`n"
    Write-Utf8 (Join-Path $updateDir 'transaction.txt') ($text + "`n")
}

function New-Staging([string]$install) {
    $program = Join-Path $install 'program'
    $staging = Join-Path $install '.update\staging'
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Copy-Item (Join-Path $program '*') $staging -Recurse -Force
    Set-Content -Path (Join-Path $staging 'drill-new.txt') -Value 'new' -Encoding ascii
}

function Get-TreeSignature([string]$root) {
    if (-not (Test-Path $root)) { return '<missing>' }
    $parts = Get-ChildItem $root -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            $rel = $_.FullName.Substring($root.Length).TrimStart('\').Replace('\', '/')
            "$rel|$((Get-FileHash $_.FullName -Algorithm SHA256).Hash)"
        }
    return ($parts -join "`n")
}

function Invoke-Launcher([string]$install, [string[]]$arguments, [int]$timeoutSeconds,
                         [string]$label) {
    $launcher = Join-Path $install 'MaaRacingMaster.exe'
    if (-not (Test-Path $launcher)) { throw "发布包缺根启动器: $launcher" }
    $splat = @{ FilePath = $launcher; PassThru = $true; WindowStyle = 'Hidden' }
    if ($null -ne $arguments -and @($arguments).Count -gt 0) {
        $splat['ArgumentList'] = $arguments
    }
    $process = Start-Process @splat
    if (-not $process.WaitForExit($timeoutSeconds * 1000)) {
        try { $process.Kill() } catch { }
        throw "$label 超过 $timeoutSeconds 秒未退出"
    }
    if ($process.ExitCode -ne 0) {
        Write-Host "$label 退出码 $($process.ExitCode)"
        Show-Diagnostics $install
    }
    return $process.ExitCode
}

function Assert-StubStarted([string]$install, [datetime]$notBeforeUtc, [int]$maxSeconds) {
    $trace = Join-Path $install '.update\drill-started.txt'
    Assert (Test-Path $trace) "假壳被启动（$install）"
    $stamp = (Get-Content $trace -Raw).Trim()
    $started = [DateTime]::Parse($stamp, $null,
        [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    $delay = ($started - $notBeforeUtc).TotalSeconds
    Assert ($delay -ge 0 -and $delay -le $maxSeconds) `
        ("假壳启动耗时 {0:N1}s，在 0..{1}s 内" -f $delay, $maxSeconds)
}

function Get-StubPids([string]$install) {
    $file = Join-Path $install '.update\drill-shell-pids.txt'
    if (-not (Test-Path $file)) { return @() }
    return @(Get-Content $file | Where-Object { $_.Trim().Length -gt 0 } |
        ForEach-Object { [int]$_.Trim() })
}

function Wait-ProcessExit([int]$processId, [int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "进程 $processId 在 $timeoutSeconds 秒内没有退出"
}

function Wait-StubCount([string]$install, [int]$count, [int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ((Get-StubPids $install).Count -ge $count) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "等待第 $count 个假壳启动超时"
}

# 只按演练记录的 PID 清理，并且核对可执行文件路径在本次临时安装目录内：
# 假壳与正式壳同名，按进程名强杀会误关开发机上正在运行的正式应用。
function Show-Diagnostics([string]$install) {
    if (-not $install -or -not (Test-Path $install)) { return }
    Write-Host "---- 诊断：$install ----"
    $updateDir = Join-Path $install '.update'
    if (Test-Path $updateDir) {
        Get-ChildItem $updateDir -Force | Select-Object Name, Length | Format-Table |
            Out-String | Write-Host
        $log = Join-Path $updateDir 'apply.log'
        if (Test-Path $log) {
            Write-Host '---- apply.log ----'
            Get-Content $log -Tail 80 | Write-Host
        }
        $tx = Join-Path $updateDir 'transaction.txt'
        if (Test-Path $tx) {
            Write-Host '---- transaction.txt ----'
            Get-Content $tx | Write-Host
        }
    }
}

function Stop-Stubs([string[]]$installs) {
    foreach ($install in $installs) {
        if (-not $install -or -not (Test-Path $install)) { continue }
        foreach ($processId in (Get-StubPids $install)) {
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($null -eq $process) { continue }
            try {
                $path = $process.Path
                if ($path -and $path.StartsWith($install, [StringComparison]::OrdinalIgnoreCase)) {
                    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
                }
            }
            catch { }
        }
    }
}

$drillInstalls = @()

try {
    # ---------- 用例 1：正常升级 ----------
    Write-Host '== 用例 1：正常升级 =='
    $install = New-Install 'install-ok'
    Set-DrillMode $install 'ack'
    New-Staging $install
    Write-Tx $install 'prepared' '9.9.9'
    Assert (Test-Path (Join-Path $install 'program\app\MaaRacingMaster.Shell.exe')) '假壳已就位'
    $startedAt = [DateTime]::UtcNow
    $code = Invoke-Launcher $install @('--apply', (Join-Path $install '.update\transaction.txt')) 240 '正常升级'
    Assert ($code -eq 0) "正常升级返回 0（实际 $code）"
    Assert (Test-Path (Join-Path $install 'program\drill-new.txt')) '暂存树已就位'
    Assert (-not (Test-Path (Join-Path $install '.update\transaction.txt'))) '事务记录已清理'
    Assert (-not (Test-Path (Join-Path $install '.update\backup'))) '旧树备份已清理'
    Assert (-not (Test-Path (Join-Path $install '.update\health.txt'))) '健康回执已清理'
    Assert-StubStarted $install $startedAt 30
    $pids = Get-StubPids $install
    Assert ($pids.Count -eq 1) "只启动一次新版本（实际 $($pids.Count) 次）"
    Wait-ProcessExit $pids[0] 15
    Stop-Stubs $script:drillInstalls

    # ---------- 用例 2：无回执 → 回滚 ----------
    Write-Host '== 用例 2：试启动无回执 → 回滚 =='
    $install = New-Install 'install-timeout'
    Set-DrillMode $install 'hang'
    $before = Get-TreeSignature (Join-Path $install 'program')
    New-Staging $install
    Write-Tx $install 'prepared' '9.9.9'
    $code = Invoke-Launcher $install @('--apply', (Join-Path $install '.update\transaction.txt')) 300 '超时回滚'
    Assert ($code -eq 1) "无回执升级返回 1（实际 $code）"
    Assert ((Get-TreeSignature (Join-Path $install 'program')) -eq $before) '旧树逐文件原样恢复'
    Assert (-not (Test-Path (Join-Path $install 'program\drill-new.txt'))) '新树文件未残留'
    Assert (-not (Test-Path (Join-Path $install '.update\transaction.txt'))) '事务记录已清理'
    Assert (-not (Test-Path (Join-Path $install '.update\backup'))) '备份已清理'
    Wait-StubCount $install 2 30
    $pids = Get-StubPids $install
    Wait-ProcessExit $pids[0] 20
    Assert ($null -ne (Get-Process -Id $pids[$pids.Count - 1] -ErrorAction SilentlyContinue)) '回滚后重新拉起旧版'
    Stop-Stubs $script:drillInstalls

    # ---------- 用例 3a：替换中断（旧目录已移走） ----------
    Write-Host '== 用例 3a：旧目录已移走，普通启动恢复 =='
    $install = New-Install 'install-crash-mid'
    $program = Join-Path $install 'program'
    $backup = Join-Path $install '.update\backup'
    Move-Item $program $backup
    $before = Get-TreeSignature $backup
    Write-Tx $install 'swapping' '9.9.9'
    $startedAt = [DateTime]::UtcNow
    $code = Invoke-Launcher $install @() 120 '中断恢复（旧目录已移走）'
    Assert ($code -eq 0) "恢复后正常退出（实际 $code）"
    Assert ((Get-TreeSignature $program) -eq $before) '备份已回到 program'
    Assert (-not (Test-Path (Join-Path $install '.update\transaction.txt'))) '事务记录已清理'
    Assert-StubStarted $install $startedAt 60
    Stop-Stubs $script:drillInstalls

    # ---------- 用例 3b：新目录已就位但无回执 ----------
    Write-Host '== 用例 3b：新目录已就位、无回执，普通启动回滚 =='
    $install = New-Install 'install-crash-after-swap'
    $program = Join-Path $install 'program'
    $backup = Join-Path $install '.update\backup'
    $staging = Join-Path $install '.update\staging'
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Copy-Item (Join-Path $program '*') $staging -Recurse -Force
    Set-Content -Path (Join-Path $staging 'drill-new.txt') -Value 'new' -Encoding ascii
    $before = Get-TreeSignature $program
    Move-Item $program $backup
    Move-Item $staging $program
    Write-Tx $install 'swapped' '9.9.9'
    $code = Invoke-Launcher $install @() 120 '中断恢复（新目录已就位）'
    Assert ($code -eq 0) "回滚后正常退出（实际 $code）"
    Assert ((Get-TreeSignature $program) -eq $before) '旧树逐文件恢复'
    Assert (-not (Test-Path (Join-Path $install 'program\drill-new.txt'))) '新树文件未残留'
    Assert (-not (Test-Path (Join-Path $install '.update\transaction.txt'))) '事务记录已清理'
    Stop-Stubs $script:drillInstalls

    Write-Host '更新事务演练全部通过'
}
catch {
    Write-Host '---- 演练失败：输出全部安装目录诊断 ----'
    foreach ($dir in $script:drillInstalls) { Show-Diagnostics $dir }
    throw
}
finally {
    Stop-Stubs $script:drillInstalls
}
