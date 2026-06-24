# update.ps1 - refresh the VPS bot code + turn on XM-style phone alerts. RE-RUNNABLE.
# Run on the VPS (admin PowerShell):  irm is.gd/mt5update | iex
# Pulls the latest code, refreshes it in place (does NOT touch your scheduled trade tasks),
# and adds the position-monitor alert task. Safe to run again any time to get newer code.

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== MT5 VPS UPDATE ====' -ForegroundColor Cyan
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'NOT admin. Open Start -> Windows PowerShell (Admin) and paste the command again.' -ForegroundColor Red
    return
}

$deploy = 'C:\mt5-deploy'
$repo   = 'C:\trading-agent'
$py     = 'C:\mt5-venv\Scripts\python.exe'

# 1) latest bundle
New-Item -ItemType Directory -Path $deploy -Force | Out-Null
Invoke-WebRequest 'https://github.com/swanhtet01/mt5-vps-deploy/releases/download/v1/mt5-bundle.zip' `
    -OutFile "$deploy\mt5-bundle.zip" -UseBasicParsing -TimeoutSec 120
Expand-Archive "$deploy\mt5-bundle.zip" -DestinationPath $deploy -Force
Write-Host '  [1] latest code downloaded' -ForegroundColor Green

# 2) refresh code only (scripts + engine src). Does NOT re-import scheduled tasks.
robocopy "$deploy\trading-agent\scripts" "$repo\scripts" /E /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy "$deploy\trading-agent\src" "$repo\src" /E /NFL /NDL /NJH /NJS /NP | Out-Null
Write-Host '  [2] scripts + engine refreshed' -ForegroundColor Green

# 3) position-monitor task -> phone alert on every trade open/close (every 5 min)
$cmd = 'C:\mt5-paper\position-monitor-tick.cmd'
New-Item -ItemType Directory -Path 'C:\mt5-paper\analytics' -Force | Out-Null
$body = "@echo off`r`n`"$py`" `"$repo\scripts\position_monitor.py`" >> `"C:\mt5-paper\analytics\position-monitor.log`" 2>&1"
[System.IO.File]::WriteAllText($cmd, $body, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-PositionMonitor' /tr $cmd /sc minute /mo 5 /it /f | Out-Null
Write-Host '  [3] MT5-PositionMonitor scheduled (alerts every 5 min)' -ForegroundColor Green

# 4) seed alerts silently + apply remote control once
& $py "$repo\scripts\position_monitor.py" 2>&1 | Out-Null
& $py "$repo\scripts\remote_control.py" 2>&1 | Out-Null
Write-Host '  [4] alerts seeded + remote control applied' -ForegroundColor Green

# 5) auto-deploy task — VPS polls GitHub releases every 15 min and self-updates
$adScript = "$deploy\auto_deploy.ps1"
Invoke-WebRequest 'https://raw.githubusercontent.com/swanhtet01/mt5-vps-deploy/main/auto_deploy.ps1' `
    -OutFile $adScript -UseBasicParsing -TimeoutSec 30
$adCmd = "C:\mt5-paper\auto-deploy.cmd"
$adBody = "@echo off`r`npowershell -ExecutionPolicy Bypass -File `"$adScript`" >> `"C:\mt5-paper\analytics\auto-deploy.log`" 2>&1"
[System.IO.File]::WriteAllText($adCmd, $adBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-AutoDeploy' /tr $adCmd /sc minute /mo 15 /it /f | Out-Null
# Seed the current release tag so the first poll doesn't re-deploy immediately
$rel = Invoke-WebRequest 'https://api.github.com/repos/swanhtet01/mt5-vps-deploy/releases/latest' `
    -UseBasicParsing -TimeoutSec 15 -Headers @{Accept='application/vnd.github.v3+json'}
$curTag = ($rel.Content | ConvertFrom-Json).tag_name
if ($curTag) { Set-Content "$deploy\last_release_tag.txt" $curTag -NoNewline }
Write-Host '  [5] MT5-AutoDeploy scheduled (checks GitHub every 15 min)' -ForegroundColor Green

# 6) symbol scanner task — runs every Sunday 08:00 UTC to discover new edges
$scanCmd = "C:\mt5-paper\symbol-scanner.cmd"
$scanBody = "@echo off`r`n`"$py`" `"$repo\scripts\multi_symbol_scanner.py`" --symbols SPY,TLT,QQQ,CL,GC --timeframes 1h,4h --parallel 5 >> `"C:\mt5-paper\analytics\scanner.log`" 2>&1"
[System.IO.File]::WriteAllText($scanCmd, $scanBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-SymbolScanner' /tr $scanCmd /sc weekly /d SUN /st 08:00 /it /f | Out-Null
Write-Host '  [6] MT5-SymbolScanner scheduled (Sundays 08:00 UTC)' -ForegroundColor Green

# 7) confirm to phone
$topic = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
if ($topic) {
    $env:NTFY_TOPIC = $topic
    & $py "$repo\scripts\notify.py" 'Update done - auto-deploy + scanner now active' 2>$null
}

Write-Host ''
Write-Host '==== UPDATE COMPLETE ====' -ForegroundColor Green
Write-Host '  - Auto-deploy: VPS now self-updates when you push a new GitHub release'
Write-Host '  - Symbol scanner: runs every Sunday 08:00 UTC (finds new edges automatically)'
Write-Host '  - Trade alerts: fire on real opens/closes only (no spam)'
