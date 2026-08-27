<#
.SYNOPSIS
  Starts a standalone, LAN-reachable dashboard_server.py instance for
  manual/ad-hoc use -- separate from the one start_trading.ps1 already
  launches as part of the full daily automation.

.NOTES
  Runs on its OWN port (8788, not 8787) so it never conflicts with the
  automated instance if that's already running (e.g. during market
  hours) -- this is a second, independent process reading the SAME
  state/log files (read-only, so no write conflict either way).

  Tracked by its own PID file (state\dashboard_manual_pid.json),
  separate from running_pids.json which start_trading.ps1/
  stop_trading.ps1 own. Deliberately PID-based, not window-title
  matching -- same reasoning as stop_trading.ps1's own docstring:
  title matching is fragile and can hit an unrelated process.

  Always passes --lan (the whole point of this script is checking the
  dashboard from a phone) -- see dashboard_server.py's own docstring
  for the exposure tradeoff that implies.
#>

$ErrorActionPreference = "Stop"

$root = "D:\AI Projects\nifty-options-scanner"
$stateDir = Join-Path $root "state"
$logsDir = Join-Path $root "logs"
$pidFile = Join-Path $stateDir "dashboard_manual_pid.json"
$port = 8788

New-Item -ItemType Directory -Force -Path $stateDir, $logsDir | Out-Null
Set-Location $root

if (Test-Path $pidFile) {
    $existing = Get-Content $pidFile -Raw | ConvertFrom-Json
    $proc = Get-Process -Id $existing.pid -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Output "Already running (PID $($existing.pid)) on port $($existing.port) -- run stop_dashboard.ps1 first if you want to restart it."
        exit 0
    }
}

$stdout = Join-Path $logsDir "dashboard_manual_stdout.log"
$stderr = Join-Path $logsDir "dashboard_manual_stderr.log"
$proc = Start-Process -FilePath "python" -ArgumentList @("dashboard_server.py", "--lan", "--port", "$port") `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

@{ pid = $proc.Id; port = $port } | ConvertTo-Json | Set-Content -Path $pidFile

$lanIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -notmatch "Loopback" -and $_.IPAddress -notlike "169.254*" } |
    Select-Object -First 1 -ExpandProperty IPAddress

Write-Output "Dashboard started (PID $($proc.Id))."
Write-Output "  From this PC:    http://127.0.0.1:$port"
if ($lanIp) {
    Write-Output "  From your phone: http://${lanIp}:$port  (same WiFi)"
} else {
    Write-Output "  From your phone: http://<this PC's LAN IP>:$port  (run ipconfig to find it)"
}
Write-Output "Logs: $stdout / $stderr"
Write-Output "Stop it with stop_dashboard.ps1 (or stop_dashboard.bat)."
