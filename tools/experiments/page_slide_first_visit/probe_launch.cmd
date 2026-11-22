@echo off
rem ASCII-only on purpose: a batch file with CJK content crashes cmd parsing on
rem GBK codepages (unverified root cause for the previous launcher dying on
rem launch, this variant avoids the entire class). Every step is logged.
setlocal
set "LOGDIR=%~dp0_out"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\launch.log"
echo [%date% %time%] launch start > "%LOG%"

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo [elevate] requesting UAC ...
  echo [%date% %time%] requesting elevation >> "%LOG%"
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo [%date% %time%] running elevated >> "%LOG%"

echo [1/3] stopping existing MaaRM shell and its WebView2 processes ...
echo [%date% %time%] stopping old instances >> "%LOG%"
taskkill /F /IM MaaRacingMaster.Shell.exe >> "%LOG%" 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | Where-Object { $_.CommandLine -match 'MaaRacingMaster' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >> "%LOG%" 2>&1
timeout /t 3 /nobreak >nul

echo [2/3] setting debug-port env (CDP 9223) ...
set "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9223 --remote-allow-origins=*"
set "EXE=%~dp0..\..\..\apps\MaaRacingMaster.Shell\bin\Debug\net8.0-windows10.0.19041.0\win-x64\MaaRacingMaster.Shell.exe"
if not exist "%EXE%" (
  echo [error] exe not found: %EXE%
  echo [%date% %time%] exe not found: %EXE% >> "%LOG%"
  pause
  exit /b 1
)

echo [3/3] launching app (this console stays open while the app runs) ...
echo [%date% %time%] launching >> "%LOG%"
"%EXE%"
echo [%date% %time%] app exited >> "%LOG%"
echo App exited. You can close this window.
pause
