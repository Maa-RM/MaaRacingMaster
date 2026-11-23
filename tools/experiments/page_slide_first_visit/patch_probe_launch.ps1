# Patch probe_launch.cmd: flip MAARM_PROBE_EXTRA value per experiment run + keep CRLF.
param([string]$Value = '--disable-gpu-rasterization')
$p = 'D:\maaracing_assistant\tools\experiments\page_slide_first_visit\probe_launch.cmd'
$c = [IO.File]::ReadAllText($p)
$c = $c -replace 'set "MAARM_PROBE_EXTRA=[^"]*"', ('set "MAARM_PROBE_EXTRA=' + $Value + '"')
$c = $c -replace "(?<!`r)`n", "`r`n"
[IO.File]::WriteAllText($p, $c, [Text.Encoding]::ASCII)
Write-Output ('flag now: ' + $Value)
