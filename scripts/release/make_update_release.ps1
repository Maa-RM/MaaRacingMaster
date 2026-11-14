# Managed release packaging for MaaRM Windows (updater adaptation, P4).
# Packs a managed content tree (assemble.ps1 -Managed output) with the fixed-version
# SDK CLI into the release assets + per-channel index, enforcing:
#   - version identity across content tree / pack / index (single release truth);
#   - same-version rerun blocking (no overwriting an already-published channel entry);
#   - channel separation (stable / preview each get their own index file).
# Usage:
#   powershell -File make_update_release.ps1 -StageDir <content tree> -Version 1.0.0 `
#     -Channel stable -OutDir <release assets dir> [-VpkPath <vpk.exe>] [-DryRun]
# The client reads the feed location from the bundled update-source.txt written at
# assembly time; the frontend only sends fixed intents (no source/URL/version params).

param(
    [Parameter(Mandatory=$true)][string]$StageDir,
    [Parameter(Mandatory=$true)][string]$Version,
    [ValidateSet('stable','preview')][string]$Channel = 'stable',
    [Parameter(Mandatory=$true)][string]$OutDir,
    [string]$VpkPath = 'vpk',                 # SDK CLI（与运行时 SDK 同版固定 1.2.161，见 csproj）
    [string]$PackId = 'MaaRacingMaster',      # 正式包标识（stub 即 <PackTitle>.exe 由 CLI 生成）
    [string]$PackTitle = 'MaaRacingMaster',
    [string]$MainExe = 'MaaRacingMaster.Shell.exe',
    [switch]$DryRun                           # 只构造并打印 vpk 参数，不执行（CI 冒烟/本锁验证形态）
)

$ErrorActionPreference = 'Stop'
$Fail = { param($m) Write-Host "FATAL: $m" -ForegroundColor Red; exit 1 }

if (-not (Test-Path (Join-Path $StageDir 'MaaRacingMaster.Shell.exe'))) {
    & $Fail "StageDir 缺少内容根真实壳：$StageDir\MaaRacingMaster.Shell.exe（先跑 assemble.ps1 -Managed）"
}
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$OutDir = (Resolve-Path $OutDir).Path

# ---------- 1. 版本一致性：内容树 / pack / 索引必须对应同一次完整发行 ----------
$versionFile = Join-Path $StageDir 'maaracing_master\_version.py'
if (-not (Test-Path $versionFile)) {
    & $Fail "内容树缺少版本快照：$versionFile（assemble.ps1 未完成？）"
}
$treeVersion = (Select-String -Path $versionFile -Pattern '__version__\s*=\s*version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if ($treeVersion -ne $Version) {
    & $Fail "版本不一致：内容树 $treeVersion vs 发布参数 $Version —— 拒绝打包（一次发行一个真源）"
}
Write-Host "[release] 版本一致: $Version (内容树/打包/索引)"

# ---------- 2. 覆盖阻断：同版本重跑拒绝，不同版本放行 ----------
# 已发布渠道条目不可覆盖——修补发布一律走新版本，不改已推送稳定线历史。
$channelIndex = Join-Path $OutDir ("Releases\" + $Channel + "\releases.$Channel.json")
if (Test-Path $channelIndex) {
    try {
        $existing = Get-Content $channelIndex -Raw | ConvertFrom-Json
        $sameVersion = @($existing.Releases | Where-Object { $_.Version -eq $Version })
        if ($sameVersion.Count -gt 0) {
            & $Fail "渠道 $Channel 已存在同版本 $Version 的发行条目（$channelIndex）——同版本重复执行被阻断，修补请发新版本"
        }
        Write-Host "[release] 渠道 $Channel 已有版本：$(@($existing.Releases | ForEach-Object { $_.Version }) -join ', ')；新增 $Version 放行"
    } catch {
        & $Fail "渠道索引无法解析：$channelIndex（$_.Exception.Message）——不盲发"
    }
}

# ---------- 3. vpk 调用（正式 packId / 内容树 / 渠道 / 无增量） ----------
$vpkArgs = @(
    'pack',
    "--packId", $PackId,
    "--packTitle", $PackTitle,
    "--packVersion", $Version,
    "--channel", $Channel,
    "--runtime", "win-x64",
    "--packDir", $StageDir,
    "--mainExe", $MainExe,
    "--outputDir", $OutDir,
    "--delta", "None"
)
Write-Host ("[release] vpk 参数: " + ($vpkArgs -join ' '))
if ($DryRun) {
    Write-Host '[release] -DryRun：不执行打包（渠道索引 releases.$Channel.json 由 CLI 在 $OutDir 生成）'
    exit 0
}

if (-not (Get-Command $VpkPath -ErrorAction SilentlyContinue) -and -not (Test-Path $VpkPath)) {
    & $Fail "找不到 vpk CLI：$VpkPath（dotnet tool install vpk --version 1.2.161，与运行时 SDK 同版固定）"
}
& $VpkPath @vpkArgs
if ($LASTEXITCODE -ne 0) {
    & $Fail "vpk pack 失败 (exit $LASTEXITCODE)"
}
Write-Host "[release] done: channel=$Channel version=$Version out=$OutDir"
