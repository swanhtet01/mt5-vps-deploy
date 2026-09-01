# tasks.ps1 - show MT5-* scheduled task states and explain the health "scheduled tasks" warning.
# Run on the VPS:  irm is.gd/mt5tasks | iex
$ErrorActionPreference = 'SilentlyContinue'
$rows = schtasks /query /fo CSV /nh | ConvertFrom-Csv -Header 'TaskName','Next','Status'
$mt5 = $rows | Where-Object { $_.TaskName -like '*MT5-*' } | ForEach-Object {
    [pscustomobject]@{ Name = ($_.TaskName -replace '^\\',''); Status = $_.Status }
} | Sort-Object Name -Unique

$bad = $mt5 | Where-Object { $_.Status -notin @('Ready','Running') }
# Only tasks update.ps1 actually installs belong here. MT5-Watchdog has no script in this
# repo or the monorepo, and MT5-Heartbeat's script is not carried by hotfix-manifest.json, so
# listing them made this check permanently red -- which trains the operator to ignore it.
$critical = @('MT5-StructuralScheduler','MT5-GoldDrift-KillSwitch','MT5-PositionMonitor',
    'MT5-ContextIngest','MT5-LLMThesis','MT5-ApplyThesis','MT5-AutoDeploy',
    'MT5-VPS-Health','MT5-Maintenance')
if (Test-Path 'C:\mt5-vibe-research\install.json') { $critical += 'MT5-VibeBaseline' }
$present = @($mt5 | Select-Object -ExpandProperty Name)
$missing = @($critical | Where-Object { $present -notcontains $_ })

Write-Host ''
Write-Host "MT5 tasks: $($mt5.Count) total, $($bad.Count) NOT Ready/Running" -ForegroundColor Cyan
if (-not $bad -and -not $missing) { Write-Host 'All critical MT5 tasks are present and Ready/Running.' -ForegroundColor Green; return }

Write-Host ''
Write-Host 'These are what the health warning is about (Disabled = deliberately off):' -ForegroundColor Yellow
$bad | ForEach-Object { Write-Host ("   {0,-10} {1}" -f $_.Status, $_.Name) -ForegroundColor Gray }

$critBad = @($bad | Where-Object { $critical -contains $_.Name })
Write-Host ''
if ($critBad -or $missing) {
    Write-Host 'PROBLEM: these are CRITICAL tasks and should NOT be off:' -ForegroundColor Red
    $critBad | ForEach-Object { Write-Host "   $($_.Name)" -ForegroundColor Red }
    $missing | ForEach-Object { Write-Host "   $_ (MISSING)" -ForegroundColor Red }
    if ($critBad) {
        Write-Host 'Disabled -> re-enable:  schtasks /change /tn "<name>" /enable' -ForegroundColor Yellow
    }
    if ($missing) {
        # /enable cannot create a task that was never registered, which is what the old
        # advice told you to do. Re-running the installer creates any it is missing.
        Write-Host 'MISSING -> re-run the installer (it creates absent tasks, leaves existing ones alone):' -ForegroundColor Yellow
        Write-Host '    irm is.gd/mt5update | iex' -ForegroundColor Yellow
    }
} else {
    Write-Host 'VERDICT: all disabled tasks are retired legacy edge tasks.' -ForegroundColor Green
    Write-Host 'The broker-clock structural scheduler and safety tasks are healthy.' -ForegroundColor Green
}
