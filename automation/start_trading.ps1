<#
.SYNOPSIS
  Runs the pre-market brief, then starts all five live/paper strategy
  loops in parallel, hidden (no visible console windows).

.NOTES
  NOTHING launched here places a real broker order -- every strategy in
  this project is analytics/paper-tracking only (see each script's own
  docstring). This just automates what you'd otherwise do by opening
  five terminals by hand each morning.

  REQUIRES DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID to already be set as
  PERSISTENT Windows USER environment variables (via `setx`, run in
  ANY terminal -- see update_token.ps1) before this runs. Task
  Scheduler launches a brand-new process tree on every trigger, which
  reads whatever is CURRENTLY persisted in the registry at that
  moment -- unlike a long-lived interactive shell, it does NOT need to
  already be open when you update the token. Update the token every
  trading morning before this script's scheduled start time.

  Idempotent: stops any processes tracked from an earlier run (e.g. a
  manual re-run mid-session) before starting fresh ones, so re-running
  this never leaves orphaned duplicates.

  Every script here already writes its own dated log file (see each
  one's own logging setup) -- the stdout/stderr redirection below is
  only a safety net for a crash before that logging is even
  configured (e.g. a missing import, a bad env var), not the primary
  log.
#>

$ErrorActionPreference = "Stop"

$root = "D:\AI Projects\nifty-options-scanner"
$stateDir = Join-Path $root "state"
$logsDir = Join-Path $root "logs"
$pidFile = Join-Path $stateDir "running_pids.json"
$automationLog = Join-Path $logsDir "automation.log"

New-Item -ItemType Directory -Force -Path $stateDir, $logsDir | Out-Null
Set-Location $root

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $automationLog -Value $line
    Write-Output $line
}

# --- Guard: refuse to start anything without real credentials ---
if (-not $env:DHAN_ACCESS_TOKEN -or -not $env:DHAN_CLIENT_ID) {
    Log "ABORT: DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID not set. Run update_token.ps1 (or setx directly) before market open, then retry."
    exit 1
}

# --- Idempotency: clean up anything already tracked from an earlier run ---
& (Join-Path $PSScriptRoot "stop_trading.ps1") | Out-Null

# --- One-shot pre-market brief. Blocks until it finishes (it's meant
#     to be read before the session starts, not run continuously),
#     then the loops below start immediately after. ---
Log "Running premarket.py..."
$premarketExit = 0
try {
    Start-Process -FilePath "python" -ArgumentList "premarket.py" -WorkingDirectory $root `
        -Wait -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logsDir "premarket_stdout.log") `
        -RedirectStandardError (Join-Path $logsDir "premarket_stderr.log") | Out-Null
} catch {
    $premarketExit = 1
    Log "premarket.py failed to launch: $_"
}
if ($premarketExit -eq 0) {
    Log "premarket.py finished -- see logs/premarket_brief_$(Get-Date -Format 'yyyyMMdd').md"
}

# --- Long-running strategy loops, launched in parallel, hidden windows ---
$scripts = @(
    "main_live.py",
    "main_condor.py",
    "main_directional_spread.py",
    "main_price_action.py",
    "main_price_action_banknifty.py"
)

$tracked = @{}
foreach ($script in $scripts) {
    $name = $script -replace '\.py$', ''
    $stdout = Join-Path $logsDir "$($name)_stdout.log"
    $stderr = Join-Path $logsDir "$($name)_stderr.log"
    $proc = Start-Process -FilePath "python" -ArgumentList $script -WorkingDirectory $root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $tracked[$script] = $proc.Id
    Log "Started $script (PID $($proc.Id))"
}

$tracked | ConvertTo-Json | Set-Content -Path $pidFile
Log "All $($scripts.Count) strategies started. Tracked PIDs -> $pidFile"
