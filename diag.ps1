# diag.ps1 - one-shot VPS + bot diagnostic. Run on the VPS:  irm is.gd/mt5diag | iex
# Read-only. Dumps performance, task states, and the tail of every key log so the bad-news
# alerts can be traced to a root cause. Paste the whole output back.
$ErrorActionPreference = 'SilentlyContinue'
$py = 'C:\mt5-venv\Scripts\python.exe'
function Hr($t) { Write-Host ''; Write-Host "===== $t =====" -ForegroundColor Cyan }
function Tail($path, $n) {
    if (Test-Path $path) { Get-Content $path -Tail $n } else { Write-Host "  (no file: $path)" -ForegroundColor DarkGray }
}

Hr 'ACCOUNT + PERFORMANCE (perf_report)'
if (Test-Path "C:\trading-agent\scripts\perf_report.py") {
    & $py 'C:\trading-agent\scripts\perf_report.py' 2>&1 | Select-Object -First 45
} else { Write-Host '  (perf_report.py missing)' -ForegroundColor DarkGray }

Hr 'SCHEDULED TASKS not Ready/Running'
$rows = schtasks /query /fo CSV /nh | ConvertFrom-Csv -Header 'TaskName', 'Next', 'Status'
$mt5 = $rows | Where-Object { $_.TaskName -like '*MT5-*' } |
    ForEach-Object { [pscustomobject]@{ N = ($_.TaskName -replace '^\\', ''); S = $_.Status } } | Sort-Object N -Unique
Write-Host "Total MT5 tasks: $($mt5.Count)"
$bad = $mt5 | Where-Object { $_.S -notin 'Ready', 'Running' }
if ($bad) { $bad | ForEach-Object { Write-Host ("  {0,-10} {1}" -f $_.S, $_.N) -ForegroundColor Yellow } } else { Write-Host '  all Ready/Running' -ForegroundColor Green }

Hr 'AUTO-DEPLOY LOG (last 25) - did a deploy fail?'
Tail 'C:\mt5-paper\analytics\auto-deploy.log' 25

Hr 'THESIS LOG (last 25)'
Tail 'C:\mt5-paper\analytics\thesis.log' 25

Hr 'CONTEXT-INGEST LOG (last 15)'
Tail 'C:\mt5-paper\analytics\context.log' 15

Hr 'POSITION-MONITOR LOG (last 15)'
Tail 'C:\mt5-paper\analytics\position-monitor.log' 15

Hr 'VPS HEALTH (latest json)'
if (Test-Path 'C:\trading-agent\data_cache\vps_health.json') { Get-Content 'C:\trading-agent\data_cache\vps_health.json' -Raw } else { Write-Host '  (no vps_health.json)' -ForegroundColor DarkGray }

Hr 'CONTEXT SCORE (what sizing the bot is actually using)'
if (Test-Path 'C:\trading-agent\data_cache\context_score.json') { Get-Content 'C:\trading-agent\data_cache\context_score.json' -Raw } else { Write-Host '  (none)' -ForegroundColor DarkGray }

Hr 'LATEST THESIS'
if (Test-Path 'C:\trading-agent\data_cache\claude_thesis.json') { Get-Content 'C:\trading-agent\data_cache\claude_thesis.json' -Raw } else { Write-Host '  (none)' -ForegroundColor DarkGray }

Hr 'RECENT LIVE TRADE EVENTS (last 15)'
$je = Get-ChildItem 'C:\mt5-paper\gold-drift\*.jsonl' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($je) { Write-Host "($($je.Name))"; Get-Content $je.FullName -Tail 15 } else { Write-Host '  (no trade events file)' -ForegroundColor DarkGray }

Write-Host ''
Write-Host '===== END DIAG - paste everything above =====' -ForegroundColor Green
