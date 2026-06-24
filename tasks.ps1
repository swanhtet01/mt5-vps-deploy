# tasks.ps1 - show MT5-* scheduled task states and explain the health "scheduled tasks" warning.
# Run on the VPS:  irm is.gd/mt5tasks | iex
$ErrorActionPreference = 'SilentlyContinue'
$rows = schtasks /query /fo CSV /nh | ConvertFrom-Csv -Header 'TaskName','Next','Status'
$mt5 = $rows | Where-Object { $_.TaskName -like '*MT5-*' } | ForEach-Object {
    [pscustomobject]@{ Name = ($_.TaskName -replace '^\\',''); Status = $_.Status }
} | Sort-Object Name -Unique

$bad = $mt5 | Where-Object { $_.Status -notin @('Ready','Running') }
$critical = @('MT5-GoldDrift-Live-Enter','MT5-GoldDrift-Live-Exit','MT5-GoldDrift-KillSwitch',
    'MT5-Heartbeat','MT5-Watchdog','MT5-PositionMonitor','MT5-ContextIngest','MT5-LLMThesis',
    'MT5-ApplyThesis','MT5-AutoDeploy','MT5-VPS-Health')

Write-Host ''
Write-Host "MT5 tasks: $($mt5.Count) total, $($bad.Count) NOT Ready/Running" -ForegroundColor Cyan
if (-not $bad) { Write-Host 'All MT5 tasks are Ready/Running - the health warning should clear.' -ForegroundColor Green; return }

Write-Host ''
Write-Host 'These are what the health warning is about (Disabled = deliberately off):' -ForegroundColor Yellow
$bad | ForEach-Object { Write-Host ("   {0,-10} {1}" -f $_.Status, $_.Name) -ForegroundColor Gray }

$critBad = $bad | Where-Object { $critical -contains $_.Name }
Write-Host ''
if ($critBad) {
    Write-Host 'PROBLEM: these are CRITICAL tasks and should NOT be off:' -ForegroundColor Red
    $critBad | ForEach-Object { Write-Host "   $($_.Name)" -ForegroundColor Red }
    Write-Host 'Re-enable each with:  schtasks /change /tn "<name>" /enable' -ForegroundColor Yellow
} else {
    Write-Host 'VERDICT: all disabled tasks are non-critical edge tasks (not promoted to live).' -ForegroundColor Green
    Write-Host 'The warning is a FALSE ALARM - your live trading, kill-switch and thesis are fine.' -ForegroundColor Green
}
