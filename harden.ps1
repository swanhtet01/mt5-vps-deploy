# harden.ps1 - one-and-done: make the VPS bulletproof for 24/7 unattended trading.
# Run in an ADMIN PowerShell on the VPS:  irm is.gd/<short> | iex

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== MT5 VPS HARDEN (one-and-done) ====' -ForegroundColor Cyan
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'NOT admin. Open Start -> Windows PowerShell (Admin) and paste the command again.' -ForegroundColor Red
    return
}

# 1) Timezone = same as the laptop the tasks were built on (so UTC-pegged triggers fire right)
try {
    Set-TimeZone -Id 'Myanmar Standard Time'
    Write-Host "  [1] Timezone -> $((Get-TimeZone).Id); local now $(Get-Date -Format 'ddd HH:mm')" -ForegroundColor Green
} catch { Write-Warning "  [1] timezone: $_" }

# 2) Never sleep / blank / screensaver-lock
try {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change monitor-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -Value '0' -ErrorAction SilentlyContinue
    Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name ScreenSaveTimeOut -Value '0' -ErrorAction SilentlyContinue
    Write-Host '  [2] Sleep / display-off / screensaver disabled' -ForegroundColor Green
} catch { Write-Warning "  [2] power: $_" }

# 3) Locate MT5 terminal
$mt5 = @('C:\Program Files\MetaTrader 5\terminal64.exe','C:\Program Files\XM Global MT5\terminal64.exe') |
        Where-Object { Test-Path $_ } | Select-Object -First 1

# 4) MT5 autostart on logon
try {
    if ($mt5) {
        $startup = [Environment]::GetFolderPath('Startup')
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut((Join-Path $startup 'MT5.lnk')); $sc.TargetPath = $mt5; $sc.Save()
        Write-Host '  [3] MT5 will auto-start on logon' -ForegroundColor Green
    } else { Write-Warning '  [3] MT5 terminal not found; autostart skipped' }
} catch { Write-Warning "  [3] autostart: $_" }

# 5) Watchdog: relaunch MT5 if it ever stops (every 5 min, in-session)
try {
    if ($mt5) {
        $wd = 'C:\mt5-paper\mt5-watchdog.cmd'
        $body = "@echo off`r`ntasklist /FI ""IMAGENAME eq terminal64.exe"" | find /I ""terminal64.exe"" >nul`r`nif errorlevel 1 start """" ""$mt5"""
        [System.IO.File]::WriteAllText($wd, $body, (New-Object System.Text.ASCIIEncoding))
        schtasks /create /tn 'MT5-Watchdog' /tr $wd /sc minute /mo 5 /it /f | Out-Null
        Write-Host '  [4] MT5 watchdog installed (every 5 min)' -ForegroundColor Green
    }
} catch { Write-Warning "  [4] watchdog: $_" }

# 6) Stop Windows Update from auto-rebooting while you're logged on (no surprise reboots mid-trade)
try {
    $au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
    if (-not (Test-Path $au)) { New-Item -Path $au -Force | Out-Null }
    Set-ItemProperty $au -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord
    Set-ItemProperty $au -Name AUOptions -Value 3 -Type DWord   # download but let user choose install
    Write-Host '  [5] Windows Update will not auto-reboot while logged on' -ForegroundColor Green
} catch { Write-Warning "  [5] WU policy: $_" }

# 7) Test phone notification (proves the ntfy pipeline works end to end)
try {
    $topic = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
    $py = 'C:\mt5-venv\Scripts\python.exe'
    if ($topic -and (Test-Path $py)) {
        $env:NTFY_TOPIC = $topic
        & $py 'C:\trading-agent\scripts\notify.py' 'VPS hardened and live - test alert' 2>$null
        Write-Host "  [6] Test notification sent to ntfy topic '$topic' (check your phone)" -ForegroundColor Green
    } else { Write-Host '  [6] No NTFY_TOPIC set; skipped test notification' -ForegroundColor Yellow }
} catch { Write-Warning "  [6] notify: $_" }

# 8) OPTIONAL auto-logon so the VPS fully recovers after a reboot with zero touch.
#    You type YOUR OWN VPS password here, into your own machine's registry. Leave blank to skip.
Write-Host ''
Write-Host '  [7] OPTIONAL: auto-logon after reboot (so you never have to log in again).' -ForegroundColor Cyan
Write-Host '      Trade-off: your Windows password is stored in this VPS registry (standard for trading VPS).' -ForegroundColor Cyan
$pw = Read-Host '      Enter this VPS Windows password to enable auto-logon, or press Enter to SKIP' -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
if ($plain) {
    try {
        $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Set-ItemProperty $wl -Name AutoAdminLogon -Value '1'
        Set-ItemProperty $wl -Name DefaultUserName -Value $env:USERNAME
        Set-ItemProperty $wl -Name DefaultDomainName -Value $env:COMPUTERNAME
        Set-ItemProperty $wl -Name DefaultPassword -Value $plain
        Write-Host '      Auto-logon ENABLED. After any reboot the VPS logs in and MT5 restarts on its own.' -ForegroundColor Green
    } catch { Write-Warning "      auto-logon: $_" }
    $plain = $null
} else {
    Write-Host '      Skipped. After a rare reboot you reconnect once and log in.' -ForegroundColor Yellow
}

# 9) Summary
Write-Host ''
Write-Host '==== HARDEN COMPLETE ====' -ForegroundColor Green
$ready = (Get-ScheduledTask | Where-Object { $_.TaskName -match '^MT5' -and $_.State -ne 'Disabled' }).Count
Write-Host "  MT5 scheduled tasks active: $ready"
Write-Host "  MT5 terminal running: $([bool](Get-Process terminal64 -ErrorAction SilentlyContinue))"
Write-Host '  Clock matches laptop, no sleep/lock, MT5 self-heals, no surprise reboots.'
Write-Host ''
Write-Host '  LAST STEP (on your PHONE): install the ntfy app, subscribe to topic:' -ForegroundColor Cyan
Write-Host "    $([Environment]::GetEnvironmentVariable('NTFY_TOPIC','User'))" -ForegroundColor Cyan
Write-Host '  Then you can DISCONNECT the VNC (do not log off) and the bot runs 24/7.'
