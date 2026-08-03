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

# 4) Broker-clock scheduler state, live authorization, and overdue positions
Write-Host ''
Write-Host '--- structural scheduler (broker clock + authorization) ---' -ForegroundColor Yellow
if (Test-Path "$repo\scripts\structural_scheduler.py") {
    & $py "$repo\scripts\structural_scheduler.py" --status 2>&1 | Select-Object -First 40
} else {
    Write-Host 'MISSING: structural_scheduler.py' -ForegroundColor Red
}

# 4b) Regime evidence remains useful, but it does not prove the scheduler is running.
Write-Host ''
Write-Host '--- gold regime evidence ---' -ForegroundColor Yellow
if (Test-Path "$repo\scripts\check_gold_asian_regime.py") { & $py "$repo\scripts\check_gold_asian_regime.py" 2>&1 | Select-Object -First 15 }

# 4c) Provider-independent Vibe research state. This is research evidence only.
Write-Host ''
Write-Host '--- Vibe deterministic baseline (research-only) ---' -ForegroundColor Yellow
$vibeStatePath = 'C:\mt5-vibe-research\last-baseline.json'
if (Test-Path $vibeStatePath) {
    $vibe = Get-Content $vibeStatePath -Raw | ConvertFrom-Json
    $ageHours = if ($vibe.finished_at) {
        [Math]::Round(((Get-Date).ToUniversalTime() - ([DateTime]$vibe.finished_at).ToUniversalTime()).TotalHours, 1)
    } else { $null }
    [pscustomobject]@{
        Status = $vibe.status
        AgeHours = $ageHours
        Symbols = $vibe.symbols_loaded
        Discovered = $vibe.candidate_count
        Screened = $vibe.historical_screen_trials
        ScreenPass = $vibe.historical_screen_pass_count
        Paper = $vibe.paper_candidate_count
        LiveEligible = $vibe.live_eligible_count
        OrderAuthority = $vibe.order_authority
    } | Format-Table -AutoSize
} elseif (Test-Path 'C:\mt5-vibe-research\install.json') {
    Write-Host 'MISSING: deterministic baseline has not completed.' -ForegroundColor Red
} else {
    Write-Host 'Vibe sidecar is not installed.' -ForegroundColor DarkGray
}

# 4d) Autonomous quote-only forward evidence. This never indicates live authority.
Write-Host ''
Write-Host '--- Vibe shadow-forward evidence (quote-only) ---' -ForegroundColor Yellow
$vibeShadowPath = "$repo\data_cache\vibe_shadow_forward_report.json"
if (Test-Path $vibeShadowPath) {
    $shadow = Get-Content $vibeShadowPath -Raw | ConvertFrom-Json
    $shadowAgeMinutes = if ($shadow.generated_at_host_utc) {
        [Math]::Round(((Get-Date).ToUniversalTime() - ([DateTime]$shadow.generated_at_host_utc).ToUniversalTime()).TotalMinutes, 1)
    } else { $null }
    [pscustomobject]@{
        Artifact = $shadow.artifact_status
        AgeMinutes = $shadowAgeMinutes
        Experiments = $shadow.experiment_count
        Open = $shadow.open_position_count
        Closed = $shadow.closed_trade_count
        PaperNetUSD = $shadow.paper_net_pnl_usd
        EvidencePass = $shadow.paper_evidence_gate_pass_count
        LiveEligible = $shadow.live_eligible_count
        OrderAuthority = $shadow.order_authority
    } | Format-Table -AutoSize
} elseif (Test-Path 'C:\mt5-vibe-research\install.json') {
    Write-Host 'MISSING: Vibe shadow heartbeat has not completed.' -ForegroundColor Red
} else {
    Write-Host 'Vibe sidecar is not installed.' -ForegroundColor DarkGray
}

# 5) Push a one-line summary to the phone
$topic = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
if ($topic -and (Test-Path $py)) {
    $env:NTFY_TOPIC = $topic
    & $py "$repo\scripts\notify.py" daily-summary 2>$null
    Write-Host ''
    Write-Host "Summary pushed to your phone (ntfy '$topic')." -ForegroundColor Green
}

Write-Host ''
Write-Host 'READ ME: MT5-StructuralScheduler must run every 5 minutes.' -ForegroundColor Cyan
Write-Host 'MT5-VibeShadow must also run every 5 minutes and always show OrderAuthority=False.' -ForegroundColor Cyan
Write-Host 'PAPER or an empty allowlist means no real entries by design; it is not a regime decision.' -ForegroundColor Cyan
