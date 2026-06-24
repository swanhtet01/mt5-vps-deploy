# autologon.ps1 - one-time: enable Windows auto-logon so the VPS survives reboots.
# Run on the VPS (admin PowerShell):  irm is.gd/mt5logon | iex
# You type the admin password ONCE at a secure prompt; it is written only to this machine's
# local registry (where Windows auto-logon reads it) - it is never sent anywhere.

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== ENABLE AUTO-LOGON (reboot survival) ====' -ForegroundColor Cyan
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'NOT admin. Open Start -> Windows PowerShell (Admin) and run it again.' -ForegroundColor Red
    return
}

$user = $env:USERNAME
Write-Host "Account that will auto-logon: $user" -ForegroundColor Yellow
$sec = Read-Host "Type the Windows password for $user (input hidden), then Enter" -AsSecureString
$pw = (New-Object System.Management.Automation.PSCredential('x', $sec)).GetNetworkCredential().Password
if (-not $pw) { Write-Host 'No password entered - aborted, nothing changed.' -ForegroundColor Red; return }

$key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $key -Name AutoAdminLogon   -Value '1'            -Type String
Set-ItemProperty $key -Name DefaultUserName   -Value $user         -Type String
Set-ItemProperty $key -Name DefaultPassword   -Value $pw           -Type String
Set-ItemProperty $key -Name DefaultDomainName -Value $env:COMPUTERNAME -Type String

# Verify the values stuck (do NOT print the password).
$chk = Get-ItemProperty $key
Write-Host ''
if ($chk.AutoAdminLogon -eq '1' -and $chk.DefaultUserName -eq $user -and $chk.DefaultPassword) {
    Write-Host 'AUTO-LOGON ENABLED.' -ForegroundColor Green
    Write-Host "  After any reboot the VPS logs in as '$user' automatically, so MT5," -ForegroundColor Green
    Write-Host '  trading, the kill-switch and the thesis all resume on their own.' -ForegroundColor Green
    Write-Host '  Optional test: reboot the VPS and confirm it returns logged in with MT5 running.' -ForegroundColor Yellow
} else {
    Write-Host 'Something did not stick - re-run, or set it via netplwiz manually.' -ForegroundColor Red
}
