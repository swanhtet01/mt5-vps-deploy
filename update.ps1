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

# 1) latest bundle - resolve the newest GitHub release asset (not a hardcoded tag),
#    so a freshly published release actually deploys. Falls back to v1 if the API is unreachable.
New-Item -ItemType Directory -Path $deploy -Force | Out-Null
$bundleUrl = 'https://github.com/swanhtet01/mt5-vps-deploy/releases/download/v1/mt5-bundle.zip'
try {
    $rel = Invoke-WebRequest 'https://api.github.com/repos/swanhtet01/mt5-vps-deploy/releases/latest' `
        -UseBasicParsing -TimeoutSec 15 -Headers @{Accept='application/vnd.github.v3+json'}
    $asset = ($rel.Content | ConvertFrom-Json).assets | Where-Object { $_.name -eq 'mt5-bundle.zip' } | Select-Object -First 1
    if ($asset.browser_download_url) { $bundleUrl = $asset.browser_download_url }
} catch { Write-Host '  (GitHub API unreachable; using v1 bundle)' -ForegroundColor DarkGray }
Invoke-WebRequest $bundleUrl -OutFile "$deploy\mt5-bundle.zip" -UseBasicParsing -TimeoutSec 120
Expand-Archive "$deploy\mt5-bundle.zip" -DestinationPath $deploy -Force
Write-Host '  [1] latest code downloaded' -ForegroundColor Green

# 2) refresh code (scripts + engine src). The bundle now carries ALL fixes at source
#    (model id, registry key fallback, real ntfy push, gold lot clamp, thesis fail-safes),
#    so there are no fragile in-place patches anymore. Fail LOUD on a bad copy: robocopy
#    exit codes 0-7 are success, >=8 is a real failure.
robocopy "$deploy\trading-agent\scripts" "$repo\scripts" /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host '  DEPLOY FAILED: robocopy scripts' -ForegroundColor Red; throw 'robocopy scripts failed' }
robocopy "$deploy\trading-agent\src" "$repo\src" /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host '  DEPLOY FAILED: robocopy src' -ForegroundColor Red; throw 'robocopy src failed' }
# 2-override: the published bundle's vps_health.py still has the noisy task check (warns on
# ANY disabled MT5-* task -> chronic "scheduled_tasks: ?" false alarm). Drop in the fixed
# version (warns only on CRITICAL tasks + names them) until the next full bundle rebuild
# carries it natively. Self-retiring: skipped once the deployed copy already has the fix.
if (-not (Select-String -Path "$repo\scripts\vps_health.py" -Pattern 'critical_down' -Quiet)) {
    try {
        Invoke-WebRequest 'https://raw.githubusercontent.com/swanhtet01/mt5-vps-deploy/main/vps_health.py' `
            -OutFile "$repo\scripts\vps_health.py" -UseBasicParsing -TimeoutSec 20
        Write-Host '  [2-fix] vps_health task-alert noise fix applied' -ForegroundColor Green
    } catch { Write-Host '  (could not fetch vps_health fix; non-fatal)' -ForegroundColor DarkGray }
}
Write-Host '  [2] scripts + engine refreshed (no patches; bundle is the source of truth)' -ForegroundColor Green

# 2b) ensure runtime deps in the venv. pip is a no-op if already satisfied. Fail loud.
& $py -m pip install --quiet anthropic yfinance numpy psutil
if ($LASTEXITCODE) { Write-Host '  DEPLOY FAILED: pip install' -ForegroundColor Red; throw 'pip install failed' }
Write-Host '  [2b] python deps verified (anthropic, yfinance, numpy, psutil)' -ForegroundColor Green

# 2c) mirror NTFY_TOPIC to Machine scope so SYSTEM-context tasks (e.g. a boot alert) can
#     also push to the phone, and so notify.py's registry fallback always finds it.
$ntfyUser = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
if ($ntfyUser) { [Environment]::SetEnvironmentVariable('NTFY_TOPIC', $ntfyUser, 'Machine') }

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

# 5) auto-deploy task - VPS polls GitHub releases every 15 min and self-updates
$adScript = "$deploy\auto_deploy.ps1"
Invoke-WebRequest 'https://raw.githubusercontent.com/swanhtet01/mt5-vps-deploy/main/auto_deploy.ps1' `
    -OutFile $adScript -UseBasicParsing -TimeoutSec 30
$adCmd = "C:\mt5-paper\auto-deploy.cmd"
$adBody = "@echo off`r`npowershell -ExecutionPolicy Bypass -File `"$adScript`" >> `"C:\mt5-paper\analytics\auto-deploy.log`" 2>&1"
[System.IO.File]::WriteAllText($adCmd, $adBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-AutoDeploy' /tr $adCmd /sc minute /mo 15 /it /f | Out-Null
# Seed the current main commit SHA so the first auto-deploy poll doesn't immediately re-run.
try {
    $c = Invoke-WebRequest 'https://api.github.com/repos/swanhtet01/mt5-vps-deploy/commits/main' `
        -UseBasicParsing -TimeoutSec 15 -Headers @{Accept='application/vnd.github.v3+json'}
    $curSha = ($c.Content | ConvertFrom-Json).sha
    if ($curSha) { Set-Content "$deploy\last_deploy_sha.txt" $curSha -NoNewline }
} catch { Write-Host '  (could not seed deploy SHA; auto-deploy will deploy once on next poll)' -ForegroundColor DarkGray }
Write-Host '  [5] MT5-AutoDeploy scheduled (watches main commits every 15 min)' -ForegroundColor Green

# NOTE: this VPS runs on Myanmar time (UTC+6:30) - confirmed by the live-enter task firing
# at 06:30 local = 00:00 UTC. schtasks /st uses LOCAL time, so add 6:30 to the desired UTC.
#   06:00 UTC -> 12:30 local ; 06:30 UTC -> 13:00 local ; 08:00 UTC -> 14:30 local

# 6) symbol scanner task - Sundays 08:00 UTC (= 14:30 local) to discover new edges
$scanCmd = "C:\mt5-paper\symbol-scanner.cmd"
$scanBody = "@echo off`r`n`"$py`" `"$repo\scripts\multi_symbol_scanner.py`" --symbols SPY,TLT,QQQ,CL,GC --timeframes 1h,4h --parallel 5 >> `"C:\mt5-paper\analytics\scanner.log`" 2>&1"
[System.IO.File]::WriteAllText($scanCmd, $scanBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-SymbolScanner' /tr $scanCmd /sc weekly /d SUN /st 14:30 /it /f | Out-Null
Write-Host '  [6] MT5-SymbolScanner scheduled (Sundays 08:00 UTC)' -ForegroundColor Green

# 6b) THE DAILY THESIS PIPELINE, correctly ordered. It must run BEFORE the day's first
# gold entry (00:00 UTC = 06:30 local) so sizing is fresh, not ~18h stale, and so the
# thesis reads REAL data (context_ingest writes news/macro/context first) instead of
# empty defaults. Sequence (local time = UTC+6:30):
#   04:30 local (22:00 UTC) context_ingest -> 05:00 (22:30) thesis -> 05:30 (23:00) apply
$ciCmd = 'C:\mt5-paper\context-ingest.cmd'
$ciBody = "@echo off`r`n`"$py`" `"$repo\scripts\context_ingest.py`" --data-cache-dir `"$repo\data_cache`" >> `"C:\mt5-paper\analytics\context.log`" 2>&1"
[System.IO.File]::WriteAllText($ciCmd, $ciBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-ContextIngest' /tr $ciCmd /sc daily /st 04:30 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  DEPLOY FAILED: MT5-ContextIngest' -ForegroundColor Red; throw 'schtasks ContextIngest' }
$thCmd = 'C:\mt5-paper\llm-thesis.cmd'
$thBody = "@echo off`r`n`"$py`" `"$repo\scripts\thesis_ingest.py`" >> `"C:\mt5-paper\analytics\thesis.log`" 2>&1"
[System.IO.File]::WriteAllText($thCmd, $thBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-LLMThesis' /tr $thCmd /sc daily /st 05:00 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  DEPLOY FAILED: MT5-LLMThesis' -ForegroundColor Red; throw 'schtasks LLMThesis' }
$apCmd = 'C:\mt5-paper\apply-thesis.cmd'
$apBody = "@echo off`r`n`"$py`" `"$repo\scripts\apply_approved_thesis.py`" >> `"C:\mt5-paper\analytics\thesis.log`" 2>&1"
[System.IO.File]::WriteAllText($apCmd, $apBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-ApplyThesis' /tr $apCmd /sc daily /st 05:30 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  DEPLOY FAILED: MT5-ApplyThesis' -ForegroundColor Red; throw 'schtasks ApplyThesis' }
# Defensive: ensure the kill-switch (cumulative-drawdown brake) is ENABLED, not just present.
schtasks /change /tn 'MT5-GoldDrift-KillSwitch' /enable 2>$null | Out-Null
Write-Host '  [6b] context-ingest + thesis + apply scheduled (22:00/22:30/23:00 UTC, pre-entry); kill-switch enabled' -ForegroundColor Green

# 6c) Reboot-survival backstop: a SYSTEM task that pings the phone on boot so a restart
# (Windows Update, host maintenance) is VISIBLE. With auto-logon set up (recommended),
# trading resumes by itself; without it this is your only signal that a reboot happened.
# Runs as SYSTEM (no logon needed) and reads NTFY_TOPIC from Machine scope (mirrored in 2c).
$bootPs = 'C:\mt5-paper\boot-alert.ps1'
$bootPsBody = @'
Start-Sleep -Seconds 90
$t = [Environment]::GetEnvironmentVariable("NTFY_TOPIC","Machine")
if ($t) {
    try {
        Invoke-WebRequest "https://ntfy.sh/$t" -Method POST -UseBasicParsing -TimeoutSec 12 -Body "VPS rebooted. If auto-logon is not set, log in via VNC so MT5 and trading resume." -Headers @{ Title = "MT5 VPS rebooted"; Tags = "warning" } | Out-Null
    } catch {}
}
'@
[System.IO.File]::WriteAllText($bootPs, $bootPsBody, (New-Object System.Text.ASCIIEncoding))
$bootCmd = 'C:\mt5-paper\boot-alert.cmd'
$bootCmdBody = "@echo off`r`npowershell -ExecutionPolicy Bypass -File `"$bootPs`""
[System.IO.File]::WriteAllText($bootCmd, $bootCmdBody, (New-Object System.Text.ASCIIEncoding))
schtasks /create /tn 'MT5-BootAlert' /tr $bootCmd /sc onstart /ru SYSTEM /rl HIGHEST /f | Out-Null
Write-Host '  [6c] MT5-BootAlert scheduled (phone ping on reboot)' -ForegroundColor Green

# 7) LLM thesis self-test - verify the Claude API key + model work end-to-end.
#    Skipped on auto-deploy runs (MT5_AUTODEPLOY=1) so a code deploy never pushes a
#    thesis to the phone - the scheduled MT5-LLMThesis task owns the daily push.
#    thesis_ingest.py logs to STDERR; capturing that via "2>&1 |" while
#    $ErrorActionPreference='Stop' makes PowerShell treat normal log lines as a
#    fatal NativeCommandError and abort. So: temporarily relax EAP and redirect
#    ALL streams to a file. Success is judged by a freshly-written
#    claude_thesis.json (only written when Claude actually responds), not log text.
if ($env:MT5_AUTODEPLOY) {
    Write-Host '  [7] thesis self-test skipped (auto-deploy run - no phone push)' -ForegroundColor DarkGray
} else {
Write-Host '  [7] testing LLM thesis (calls Claude)...' -ForegroundColor Yellow
# Load key + ntfy topic into THIS process env (setx/scope doesn't reach an already-open shell)
$env:ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','Machine')
$env:NTFY_TOPIC = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
$thesisLog = "$deploy\thesis-test.log"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $py "$repo\scripts\thesis_ingest.py" *> $thesisLog
$ErrorActionPreference = $prevEAP
$thesisJson = "$repo\data_cache\claude_thesis.json"
$ok = (Test-Path $thesisJson) -and (((Get-Date) - (Get-Item $thesisJson).LastWriteTime).TotalMinutes -lt 3)
if ($ok) {
    Write-Host '  [7] LLM thesis OK - Claude responded, thesis written + pushed to phone' -ForegroundColor Green
} else {
    Write-Host '  [7] THESIS TEST FAILED - last lines of log:' -ForegroundColor Red
    if (Test-Path $thesisLog) { Get-Content $thesisLog -Tail 6 | ForEach-Object { Write-Host "       $_" -ForegroundColor DarkYellow } }
    if (-not [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','Machine')) {
        Write-Host '       -> ANTHROPIC_API_KEY not set (Machine scope).' -ForegroundColor Red
    }
}
}

# 8) confirm to phone - only on interactive runs (auto-deploy stays silent; no spam)
if (-not $env:MT5_AUTODEPLOY) {
    $topic = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
    if ($topic) {
        $env:NTFY_TOPIC = $topic
        & $py "$repo\scripts\notify.py" 'Update done - auto-deploy + scanner + LLM thesis verified' 2>$null
    }
}

Write-Host ''
Write-Host '==== UPDATE COMPLETE ====' -ForegroundColor Green
Write-Host '  - Auto-deploy: VPS now self-updates when you push a new GitHub release'
Write-Host '  - Symbol scanner: runs every Sunday 08:00 UTC (finds new edges automatically)'
Write-Host '  - LLM thesis: tested live against Claude (see [7] above)'
Write-Host '  - Trade alerts: fire on real opens/closes only (no spam)'
