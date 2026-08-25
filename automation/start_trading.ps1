<#
.SYNOPSIS
  Runs the pre-market brief, then starts the order-flow feed, all ten
  live/paper strategy loops, and the read-only dashboard in parallel,
  hidden (no visible console windows).

.NOTES
  NOTHING launched here places a real broker order -- every strategy in
  this project is analytics/paper-tracking only (see each script's own
  docstring), and dashboard_server.py is read-only (never writes state,
  never talks to Dhan/NSE itself). This just automates what you'd
  otherwise do by opening fourteen terminals by hand each morning.

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

# --- Idempotency: clean up anything already tracked from an earlier run.
#     ABORT if stop_trading.ps1 couldn't stop everything (e.g. a
#     process stuck at a different privilege level) -- launching fresh
#     copies on top of ones still alive means two processes writing
#     the same state file at once, a real corruption risk, not just
#     redundant. ---
& (Join-Path $PSScriptRoot "stop_trading.ps1") | Out-Null
if ($LASTEXITCODE -ne 0) {
    Log "ABORT: stop_trading.ps1 could not stop everything from a previous run (see the lines above in this log). Resolve the stuck process manually (Task Manager, or re-run stop_trading.ps1 'as Administrator'), then retry."
    exit 1
}

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
# orderflow_feed.py first: strategies only READ its state file (never
# block on it being alive -- see that script's own docstring), but
# giving its WebSocket connection a head start before anything reads
# from it is harmless and slightly better than not.
#
# orderflow_feed.py's --strike-range 300 matches what was actually
# being run manually before this automation existed -- narrower than
# its own default (config.STRIKE_RANGE_POINTS, 800pts) if omitted.
#
# orderflow_feed_banknifty.py (added 2026-08-16): Bank Nifty's own feed
# process -- orderflow_feed.py only ever subscribed to NIFTY strikes, so
# Bank Nifty's book_imbalance/total_quantity_imbalance were structurally
# guaranteed None forever (not "not enough data," zero coverage). Own
# state file (state/orderflow_banknifty.json), own spread-recording
# directory (logs/orderflow_banknifty/), own WebSocket connection --
# never shares one with orderflow_feed.py's NIFTY instance. --strike-range
# 600 is a first-pass estimate (double NIFTY's 300, matching Bank Nifty's
# 100pt vs 50pt strike spacing), not independently tuned yet.
#
# main_live_sentinel.py / main_live_banknifty_sentinel.py (added
# 2026-08-16): Sentinel v1.1-dev, the correlated-cluster-cap candidate
# -- see STRATEGY_VERSIONS.md. Each is its own process with its own
# state/journal/log files (suffixed _sentinel), started alongside, not
# instead of, its Anchor counterpart. They patch the same shared
# config/trade_tracker modules Anchor imports, but only within their
# OWN process -- Anchor's own main_live.py / main_live_banknifty.py
# processes are unaffected (confirmed by importing them in isolation
# after the Sentinel files were added; STRATEGY_NAME stayed "Anchor",
# CLUSTER_CAP_ENABLED stayed False).
#
# main_live_onetrade.py (added 2026-08-25): "One Trade/Day" v0.1-research
# -- Anchor's own scan -> plan -> risk -> breakeven pipeline with ONE
# change, config.MAX_NEW_TRADES_PER_DAY patched to 1 (default is 999,
# effectively uncapped). Backtested in research/one_trade_per_day_study.py:
# meaningfully lower drawdown and a higher win rate than Anchor's full
# ~9-trades/day volume, on a realistic Rs1L account. Own state/journal/log,
# started alongside Anchor and Sentinel, not instead of either. Status is
# research/experimental (not in STRATEGY_VERSIONS.md's promoted registry) --
# this starts it FORWARD-TRACKING so there is real evidence to eventually
# decide on, same as Sentinel was before it.
#
# position_notifier.py (added 2026-08-16): posts open positions,
# today's pre-market summary, and intraday bias-shift alerts to
# Telegram -- see that file's own docstring. Deliberately started AFTER
# this array's other entries would already have premarket.py's JSON
# brief on disk (premarket.py above runs -Wait, blocking, before this
# whole array starts), so its startup pre-market message has something
# to read on the very first cycle. Needs TELEGRAM_BOT_TOKEN /
# TELEGRAM_CHAT_ID to actually deliver anything -- unlike the Dhan-
# credentials guard above, this is NOT required to start: if unset, it
# just logs failed-send lines to its own log file every cycle instead
# of blocking the other twelve processes, since this is a convenience
# notifier, not something trading itself depends on.
$scripts = @(
    @{ file = "orderflow_feed.py"; args = @("--strike-range", "300") },
    @{ file = "orderflow_feed_banknifty.py"; args = @("--strike-range", "600") },
    @{ file = "main_live.py"; args = @() },
    @{ file = "main_live_sentinel.py"; args = @() },
    @{ file = "main_live_onetrade.py"; args = @() },
    @{ file = "main_condor.py"; args = @() },
    @{ file = "main_directional_spread.py"; args = @() },
    @{ file = "main_price_action.py"; args = @() },
    @{ file = "main_price_action_banknifty.py"; args = @() },
    @{ file = "main_live_banknifty.py"; args = @() },
    @{ file = "main_live_banknifty_sentinel.py"; args = @() },
    @{ file = "main_directional_spread_banknifty.py"; args = @() },
    @{ file = "dashboard_server.py"; args = @() },
    @{ file = "position_notifier.py"; args = @() }
)

$tracked = @{}
foreach ($entry in $scripts) {
    $script = $entry.file
    $name = $script -replace '\.py$', ''
    $stdout = Join-Path $logsDir "$($name)_stdout.log"
    $stderr = Join-Path $logsDir "$($name)_stderr.log"
    $proc = Start-Process -FilePath "python" -ArgumentList (@($script) + $entry.args) -WorkingDirectory $root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $tracked[$script] = $proc.Id
    Log "Started $script $($entry.args -join ' ') (PID $($proc.Id))"
}

$tracked | ConvertTo-Json | Set-Content -Path $pidFile
Log "All $($scripts.Count) strategies started. Tracked PIDs -> $pidFile"
