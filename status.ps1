# status.ps1 - one short command to check the MT5 VPS bot's health.
# Run on the VPS (any PowerShell):  irm is.gd/mt5status | iex
# Shows: which tasks fired + when, MT5 connection, P/L + regime (why it traded or sat out),
# and pushes a summary to your phone.

$ErrorActionPreference = 'SilentlyContinue'
$py   = 'C:\mt5-venv\Scripts\python.exe'
$repo = 'C:\trading-agent'

Write-Host ''
Write-Host '==== MT5 VPS STATUS ====' -ForegroundColor Cyan

# 1) Are the trading tasks actually firing?
Write-Host ''
Write-Host '--- recent task runs (LastTaskResult 0 = ran OK) ---' -ForegroundColor Yellow
Get-ScheduledTask -TaskName 'MT5-*' -ErrorAction SilentlyContinue | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult |
    Sort-Object LastRunTime -Descending | Select-Object -First 14 | Format-Table -AutoSize

# 2) MT5 terminal alive?
$term = Get-Process terminal64 -ErrorAction SilentlyContinue
$col = if ($term) { 'Green' } else { 'Red' }
Write-Host "MT5 terminal running: $([bool]$term)" -ForegroundColor $col

# 3) P/L + closed trades
Write-Host ''
Write-Host '--- performance ---' -ForegroundColor Yellow
if (Test-Path "$repo\scripts\perf_report.py") { & $py "$repo\scripts\perf_report.py" 2>&1 | Select-Object -First 12 }

# 4) Gold-drift regime gate (THE reason it trades or sits out in the morning)
Write-Host ''
Write-Host '--- gold-drift regime gate (why it traded or sat out) ---' -ForegroundColor Yellow
if (Test-Path "$repo\scripts\check_gold_asian_regime.py") { & $py "$repo\scripts\check_gold_asian_regime.py" 2>&1 | Select-Object -First 15 }
elseif (Test-Path "$repo\scripts\dashboard.py") { & $py "$repo\scripts\dashboard.py" 2>&1 | Select-Object -First 20 }

# 5) Push a one-line summary to the phone
$topic = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
if ($topic -and (Test-Path $py)) {
    $env:NTFY_TOPIC = $topic
    & $py "$repo\scripts\notify.py" daily-summary 2>$null
    Write-Host ''
    Write-Host "Summary pushed to your phone (ntfy '$topic')." -ForegroundColor Green
}

Write-Host ''
Write-Host 'READ ME: if MT5-GoldDrift-Live-Enter shows a LastRunTime today + result 0,' -ForegroundColor Cyan
Write-Host 'the bot is HEALTHY -- it just chose not to trade (regime gate). That is normal.' -ForegroundColor Cyan
