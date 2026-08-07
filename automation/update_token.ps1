<#
.SYNOPSIS
  Run this every trading morning, BEFORE start_trading.ps1's scheduled
  time, to persist today's fresh Dhan access token as a Windows USER
  environment variable.

.NOTES
  Uses `setx`, which writes to the registry (HKCU\Environment) -- any
  NEW process started after this (including whatever Task Scheduler
  launches later) will read the fresh value. It does NOT affect the
  CURRENT PowerShell session's own $env: copy (setx never does) -- if
  you also want to test something in THIS terminal right after running
  this, additionally run:  $env:DHAN_ACCESS_TOKEN = "..."  yourself.

  The token is only ever held in memory here and passed straight to
  setx -- never written to a log file or printed back.
#>

$token = Read-Host "Paste today's DHAN_ACCESS_TOKEN" -AsSecureString
$plainToken = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($token))
setx DHAN_ACCESS_TOKEN "$plainToken" | Out-Null
Remove-Variable plainToken

$updateClientId = Read-Host "Update DHAN_CLIENT_ID too? (usually not needed -- it rarely changes) [y/N]"
if ($updateClientId -eq "y") {
    $clientId = Read-Host "Paste DHAN_CLIENT_ID"
    setx DHAN_CLIENT_ID "$clientId" | Out-Null
}

Write-Output "Token persisted for future processes (Task Scheduler will pick it up on its next trigger)."
Write-Output "This terminal's own session isn't updated -- that's fine, it doesn't need to be."
