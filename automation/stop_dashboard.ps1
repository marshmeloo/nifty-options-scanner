<#
.SYNOPSIS
  Stops the standalone dashboard instance start_dashboard.ps1 launched.

.NOTES
  Only ever reads/writes state\dashboard_manual_pid.json -- does NOT
  touch the full automation's own dashboard_server.py (tracked
  separately in running_pids.json, stopped by stop_trading.ps1).
  Safe to run with nothing tracked (no pid file -> no-op) and safe to
  run when it already exited on its own.
#>

$ErrorActionPreference = "Stop"

$pidFile = "D:\AI Projects\nifty-options-scanner\state\dashboard_manual_pid.json"

if (-not (Test-Path $pidFile)) {
    Write-Output "Not running (no pid file)."
    exit 0
}

$tracked = Get-Content $pidFile -Raw | ConvertFrom-Json
$proc = Get-Process -Id $tracked.pid -ErrorAction SilentlyContinue
if ($proc) {
    Stop-Process -Id $tracked.pid -Force
    Write-Output "Stopped (PID $($tracked.pid))."
} else {
    Write-Output "Already not running (PID $($tracked.pid) not found)."
}
Remove-Item $pidFile -Force
