# harden.ps1 - make the VPS bulletproof for 24/7 unattended trading.
# Run in an ADMIN PowerShell on the VPS:  irm is.gd/<short> | iex
#   1. Timezone -> Myanmar (so UTC-pegged task triggers fire at the right instant)
#   2. Never sleep / blank / screensaver-lock
#   3. MT5 auto-starts when you log on
#   4. Watchdog relaunches MT5 within 5 min if it ever stops

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== MT5 VPS HARDEN ====' -ForegroundColor Cyan
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'NOT admin. Open Start -> Windows PowerShell (Admin) and paste the command again.' -ForegroundColor Red
    return
}

# 1) Timezone = same as the laptop the tasks were built on
try {
    Set-TimeZone -Id 'Myanmar Standard Time'
    Write-Host "  [1] Timezone -> $((Get-TimeZone).Id) ($(Get-Date -Format 'HH:mm') local)" -ForegroundColor Green
} catch { Write-Warning "  [1] timezone: $_" }

# 2) Never sleep / blank / screensaver
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change monitor-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -Value '0' -ErrorAction SilentlyContinue
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name ScreenSaveTimeOut -Value '0' -ErrorAction SilentlyContinue
Write-Host '  [2] Sleep / display-off / screensaver all disabled' -ForegroundColor Green

# 3) Locate MT5 terminal
$mt5 = @('C:\Program Files\MetaTrader 5\terminal64.exe','C:\Program Files\XM Global MT5\terminal64.exe') |
        Where-Object { Test-Path $_ } | Select-Object -First 1

# 4) MT5 autostart shortcut on logon
if ($mt5) {
    $startup = [Environment]::GetFolderPath('Startup')
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut((Join-Path $startup 'MT5.lnk'))
    $sc.TargetPath = $mt5
    $sc.Save()
    Write-Host "  [3] MT5 will auto-start on logon" -ForegroundColor Green
} else {
    Write-Warning '  [3] MT5 terminal not found; autostart skipped'
}

# 5) Watchdog: relaunch MT5 if it ever stops (every 5 min, in-session)
if ($mt5) {
    $wd = 'C:\mt5-paper\mt5-watchdog.cmd'
    $body = "@echo off`r`ntasklist /FI ""IMAGENAME eq terminal64.exe"" | find /I ""terminal64.exe"" >nul`r`nif errorlevel 1 start """" ""$mt5"""
    [System.IO.File]::WriteAllText($wd, $body, (New-Object System.Text.ASCIIEncoding))
    schtasks /create /tn 'MT5-Watchdog' /tr $wd /sc minute /mo 5 /it /f | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host '  [4] MT5 watchdog installed (checks every 5 min)' -ForegroundColor Green }
    else { Write-Warning "  [4] watchdog create exit $LASTEXITCODE" }
}

Write-Host ''
Write-Host '==== HARDEN COMPLETE ====' -ForegroundColor Green
Write-Host '  - VPS clock matches the laptop -> calendar edges fire at the correct UTC time'
Write-Host '  - Never sleeps or locks while you stay logged on (just DISCONNECT, do not log off)'
Write-Host '  - MT5 auto-starts and self-heals if it crashes'
Write-Host ''
Write-Host '  After a full reboot (e.g. Windows Update): reconnect once and confirm MT5 logged in.'
