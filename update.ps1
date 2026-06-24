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

# 1) latest bundle — resolve the newest GitHub release asset (not a hardcoded tag),
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

# 2) refresh code only (scripts + engine src). Does NOT re-import scheduled tasks.
robocopy "$deploy\trading-agent\scripts" "$repo\scripts" /E /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy "$deploy\trading-agent\src" "$repo\src" /E /NFL /NDL /NJH /NJS /NP | Out-Null
# Safety patch: the bundled thesis generator may still reference a retired Claude model.
# Rewrite it to the current model so the LLM thesis never 404s, even from a stale bundle.
$thesisFile = "$repo\src\mt5_agent\claude_thesis_generator.py"
if (Test-Path $thesisFile) {
    (Get-Content $thesisFile -Raw) -replace 'claude-3-5-sonnet-20241022', 'claude-opus-4-8' |
        Set-Content $thesisFile -NoNewline
}
# Robust API-key bootstrap: Task Scheduler can launch with a stale environment that
# doesn't include a recently-set Machine env var, so the anthropic SDK can't find the
# key. Inject a registry read so the thesis generator self-loads ANTHROPIC_API_KEY
# from HKLM regardless of how the process was started. Idempotent (marker-guarded).
$gen = "$repo\src\mt5_agent\claude_thesis_generator.py"
if ((Test-Path $gen) -and -not (Select-String -Path $gen -Pattern '_load_api_key_from_registry' -Quiet)) {
    $boot = @'
    # Self-load the key from the Windows Machine registry if the process env lacks it
    # (Task Scheduler may not inherit a recently-set Machine env var). _load_api_key_from_registry
    import os as _os
    if not _os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import winreg as _wr
            with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as _k:
                _os.environ["ANTHROPIC_API_KEY"] = _wr.QueryValueEx(_k, "ANTHROPIC_API_KEY")[0]
        except Exception:
            pass
    client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var
'@
    (Get-Content $gen -Raw) -replace '    client = anthropic\.Anthropic\(\)  # Uses ANTHROPIC_API_KEY env var', $boot |
        Set-Content $gen -NoNewline
}
Write-Host '  [2] scripts + engine refreshed' -ForegroundColor Green

# 2b) ensure required Python deps exist in the venv (anthropic for thesis, yfinance for scanner).
#     pip is a no-op if already satisfied, so this is safe to run every time.
& $py -m pip install --quiet anthropic yfinance 2>&1 | Out-Null
Write-Host '  [2b] python deps verified (anthropic + yfinance)' -ForegroundColor Green

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

# 7) LLM thesis self-test — verify the Claude API key + model work end-to-end.
#    thesis_ingest.py logs to STDERR; capturing that via "2>&1 |" while
#    $ErrorActionPreference='Stop' makes PowerShell treat normal log lines as a
#    fatal NativeCommandError and abort. So: temporarily relax EAP and redirect
#    ALL streams to a file. Success is judged by a freshly-written
#    claude_thesis.json (only written when Claude actually responds), not log text.
Write-Host '  [7] testing LLM thesis (calls Claude)...' -ForegroundColor Yellow
# Load the key into THIS process env (setx/Machine scope doesn't reach an already-open shell)
$env:ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','Machine')
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

# 8) confirm to phone
$topic = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
if ($topic) {
    $env:NTFY_TOPIC = $topic
    & $py "$repo\scripts\notify.py" 'Update done - auto-deploy + scanner + LLM thesis verified' 2>$null
}

Write-Host ''
Write-Host '==== UPDATE COMPLETE ====' -ForegroundColor Green
Write-Host '  - Auto-deploy: VPS now self-updates when you push a new GitHub release'
Write-Host '  - Symbol scanner: runs every Sunday 08:00 UTC (finds new edges automatically)'
Write-Host '  - LLM thesis: tested live against Claude (see [7] above)'
Write-Host '  - Trade alerts: fire on real opens/closes only (no spam)'
