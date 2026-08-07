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

  A single process that can't be stopped (e.g. "Access is denied" --
  usually means it was started at a DIFFERENT privilege level than
  this script is running at now, most commonly because start_trading
  was once run "as Administrator") does NOT abort the rest of this
  loop; every other tracked process still gets stopped. Any process
  that couldn't be stopped stays in the pid file (with everything else
  removed from it) and this script exits non-zero, so
  start_trading.ps1 knows to ABORT rather than launch a second copy of
  that strategy on top of one that's still alive -- two processes
  writing the same state file at once is a real corruption risk, not
  just redundant.
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
$stillRunning = @{}

foreach ($prop in $tracked.PSObject.Properties) {
    $script = $prop.Name
    $procId = $prop.Value
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) {
        Log "$script (PID $procId) already not running"
        continue
    }
    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Log "Stopped $script (PID $procId)"
    } catch {
        $stillRunning[$script] = $procId
        Log ("COULD NOT STOP $script (PID $procId): $($_.Exception.Message) -- " +
             "likely a privilege mismatch (started elevated?). Fix: close it manually " +
             "(Task Manager, or an elevated PowerShell: Stop-Process -Id $procId -Force), " +
             "or re-run this script 'as Administrator'.")
    }
}

if ($stillRunning.Count -gt 0) {
    $stillRunning | ConvertTo-Json | Set-Content -Path $pidFile
    Log "stop_trading: $($stillRunning.Count) process(es) still running -- see above. Pid file kept for those only."
    exit 1
} else {
    Remove-Item $pidFile -Force
    Log "stop_trading: done, pid file cleared."
    exit 0
}
