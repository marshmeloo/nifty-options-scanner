<#
.SYNOPSIS
  Stops exactly the processes start_trading.ps1 launched, by PID.

.NOTES
  Deliberately PID-based, not window-title or command-line matching --
  both are fragile (titles can get renamed by the shell, command-line
  matching can accidentally hit an unrelated python process running a
  similarly-named script elsewhere). start_trading.ps1 records the
  exact PID of each process it launches; this reads that file back and
  kills exactly those, nothing else.

  Safe to run with nothing tracked (no pid file -> no-op), and safe to
  run when some tracked processes already exited on their own --
  start_trading.ps1 calls this itself first on every run, purely to
  clean up before starting fresh.
#>

$ErrorActionPreference = "Stop"

$root = "D:\AI Projects\nifty-options-scanner"
$logsDir = Join-Path $root "logs"
$pidFile = Join-Path $root "state\running_pids.json"
$automationLog = Join-Path $logsDir "automation.log"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $automationLog -Value $line
    Write-Output $line
}

if (-not (Test-Path $pidFile)) {
    Log "stop_trading: no pid file, nothing to stop."
    exit 0
}

$tracked = Get-Content $pidFile -Raw | ConvertFrom-Json
foreach ($prop in $tracked.PSObject.Properties) {
    $script = $prop.Name
    $procId = $prop.Value
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $procId -Force
        Log "Stopped $script (PID $procId)"
    } else {
        Log "$script (PID $procId) already not running"
    }
}

Remove-Item $pidFile -Force
Log "stop_trading: done, pid file cleared."
