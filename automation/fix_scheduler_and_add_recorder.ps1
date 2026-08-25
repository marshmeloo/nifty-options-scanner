<#
.SYNOPSIS
  One-time fix, run ONCE as Administrator:
    1. Makes "Trading Start" and "Trading Stop" resilient to the PC being
       asleep at their trigger time (WakeToRun + StartWhenAvailable), so a
       missed 09:13 open doesn't silently skip the whole live session --
       as happened 2026-08-21, when the PC was asleep and the live system
       (Anchor, Sentinel, both indices) sat idle from 09:15 to 10:10.
    2. Registers a new "Research Stock Spread Recorder" task so the
       stocks-in-play spread measurement no longer depends on a Claude
       Code background process, which dies when the session ends (root
       cause of the 09:30 sample being missed on 2026-08-20 AND
       2026-08-21).
    3. Registers a new "Morning Token Check" task at 09:00 IST -- wakes
       the PC 13 minutes before Trading Start's own 09:13 trigger (extra
       safety margin on top of fix #1) and pages Telegram every 5 min
       until 09:25 if DHAN_ACCESS_TOKEN is still stale, instead of the
       live system silently authenticating with yesterday's dead token.

  RESEARCH ONLY / NO ORDERS -- the new recorder task starts
  research.stock_spread_recorder (records bid/ask spreads only); the new
  token-check task makes one LTP read and sends Telegram messages. Neither
  places a trade.

Run from an elevated PowerShell:
  powershell -ExecutionPolicy Bypass -File "D:\AI Projects\option-scanner\nifty-options-scanner\automation\fix_scheduler_and_add_recorder.ps1"
#>

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Not running as Administrator. Right-click PowerShell -> 'Run as administrator', then re-run this script." -ForegroundColor Red
    exit 1
}

# --- Fix 1: wake + catch-up on the existing live-trading tasks ---
foreach ($name in @("Trading Start", "Trading Stop")) {
    $t = Get-ScheduledTask -TaskName $name
    $s = $t.Settings
    $s.WakeToRun = $true
    $s.StartWhenAvailable = $true
    Set-ScheduledTask -TaskName $name -Settings $s | Out-Null
    Write-Host "updated $name -> WakeToRun=True, StartWhenAvailable=True"
}

# --- Fix 2: register the research recorder as its own durable task ---
$researchRoot = "D:\AI Projects\option-scanner\nifty-options-scanner"
$action = New-ScheduledTaskAction -Execute "python" `
    -Argument "-m research.stock_spread_recorder" `
    -WorkingDirectory $researchRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:10AM

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun -StartWhenAvailable `
    -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName "Research Stock Spread Recorder" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "RESEARCH ONLY, no orders placed. Records F&O stock bid/ask spreads (research/stock_spread_recorder.py) for the stocks-in-play track. Starts 09:10 IST weekdays, 5 min before market open, so the 09:30 selection window is never missed again." `
    -Force | Out-Null

Write-Host "registered 'Research Stock Spread Recorder' -> 09:10 IST weekdays"

# --- Fix 3: wake the PC at 09:00 and page Telegram if the token is stale ---
$tokenAction = New-ScheduledTaskAction -Execute "python" `
    -Argument "automation\check_token.py" `
    -WorkingDirectory $researchRoot

$tokenTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00AM

$tokenSettings = New-ScheduledTaskSettingsSet `
    -WakeToRun -StartWhenAvailable `
    -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "Morning Token Check" `
    -Action $tokenAction -Trigger $tokenTrigger -Settings $tokenSettings -Principal $principal `
    -Description "NO ORDERS PLACED. Wakes the PC at 09:00 IST weekdays and checks DHAN_ACCESS_TOKEN is valid (one LTP read), paging Telegram every 5 min until 09:25 if it's stale. Extra safety margin ahead of Trading Start's own 09:13 trigger." `
    -Force | Out-Null

Write-Host "registered 'Morning Token Check' -> 09:00 IST weekdays"

Write-Host ""
Write-Host "--- verification ---"
Get-ScheduledTask -TaskName "Trading Start","Trading Stop","Research Stock Spread Recorder","Morning Token Check" |
    Select-Object TaskName, State -ExpandProperty Settings |
    Select-Object TaskName, WakeToRun, StartWhenAvailable
