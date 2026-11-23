# 实验区收尾清理：只杀挂在 webview2-gui-debug 数据目录上的 WebView2 浏览器进程。
# 不碰其他应用（输入法/小组件等）的 WebView2 实例。
Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" |
  Where-Object { $_.CommandLine -match 'webview2-gui-debug' } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output ("killed pid=" + $_.ProcessId)
  }
