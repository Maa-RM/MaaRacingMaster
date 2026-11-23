# 实验诊断：列出 MaaRM 壳与 WebView2 浏览器进程的关键信息。
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -in @('MaaRacingMaster.Shell.exe','MaaRacingMaster.exe','msedgewebview2.exe') } |
  ForEach-Object {
    $cl = $_.CommandLine
    $port = if ($cl -match '--remote-debugging-port=(\d+)') { $Matches[1] } else { '' }
    $udf  = if ($cl -match '--user-data-dir=("[^"]+"|\S+)') { $Matches[1] } else { '' }
    [PSCustomObject]@{
      pid = $_.ProcessId; name = $_.Name; port = $port
      udf = ($udf -replace '.*[\\](webview2[^\\"]*)$', '$1')
      parent = $_.ParentProcessId
    }
  } | Format-Table -AutoSize
