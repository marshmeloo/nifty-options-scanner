<#
.SYNOPSIS
  Run ONCE as Administrator. Fixes "Morning Token Check" and "Research
  Stock Spread Recorder" (registered by fix_scheduler_and_add_recorder.ps1),
  which both failed silently at their first real run on 2026-08-24
  (error 0x80070002, "file not found").

  ROOT CAUSE: their Action pointed at the bare command "python", which
  Task Scheduler resolved via PATH to the Windows Store app-execution-alias
  stub (C:\Users\xcopc\AppData\Local\Microsoft\WindowsApps\python.exe).
  That stub only works when launched from an interactive Explorer-linked
  session -- it fails from Task Scheduler's non-interactive context.
  "Trading Start" never hit this because it launches a .bat file, which
  loads a full shell environment BEFORE it calls "python" internally
  (by which point PATH resolves normally); these two tasks called
  "python" directly as their own Action, with no shell in between.

  FIX: repoint both tasks at the real interpreter's full path
  (pythoncore-3.14-64), confirmed present on disk, instead of the bare
  "python" command.

Run from an elevated PowerShell:
  powershell -ExecutionPolicy Bypass -File "D:\AI Projects\option-scanner\nifty-options-scanner\automation\fix_python_path_in_tasks.ps1"
#>

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Not running as Administrator. Right-click PowerShell -> 'Run as administrator', then re-run this script." -ForegroundColor Red
    exit 1
}

$pythonExe = "C:\Users\xcopc\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $pythonExe)) {
    Write-Host "Expected python.exe not found at $pythonExe -- check 'where.exe python' and edit this script." -ForegroundColor Red
    exit 1
}

$fixes = @(
    @{ TaskName = "Research Stock Spread Recorder"; Arguments = "-m research.stock_spread_recorder" },
    @{ TaskName = "Morning Token Check"; Arguments = "automation\check_token.py" }
)

$researchRoot = "D:\AI Projects\option-scanner\nifty-options-scanner"

foreach ($fix in $fixes) {
    $newAction = New-ScheduledTaskAction -Execute $pythonExe -Argument $fix.Arguments -WorkingDirectory $researchRoot
    Set-ScheduledTask -TaskName $fix.TaskName -Action $newAction | Out-Null
    Write-Host "updated $($fix.TaskName) -> Execute=$pythonExe"
}

Write-Host ""
Write-Host "--- verification ---"
foreach ($fix in $fixes) {
    (Get-ScheduledTask -TaskName $fix.TaskName).Actions | Select-Object Execute, Arguments, WorkingDirectory | Format-List
}
