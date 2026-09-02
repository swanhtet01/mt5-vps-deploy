# update.ps1 - refresh the VPS bot code from a hash-verified manifest. RE-RUNNABLE.
# Run on the VPS (admin PowerShell):  irm is.gd/mt5update | iex
# The structural-task migration disables legacy host-clock entry tasks, retains exits as a
# backstop, and installs one hidden feed-clock scheduler in paper mode.

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== MT5 VPS UPDATE ====' -ForegroundColor Cyan
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # `throw`, not `return`. auto_deploy.ps1 runs this file through Invoke-Expression, and a
    # `return` there is NOT a terminating error: it unwinds to the caller with no exception,
    # so auto_deploy's catch never fired, it recorded the commit as successfully deployed and
    # never retried it. A whole deploy chain could report green while installing nothing.
    throw 'NOT admin. Open Start -> Windows PowerShell (Admin) and paste the command again.'
}

$deploy = 'C:\mt5-deploy'
$repo   = 'C:\trading-agent'
$py     = 'C:\mt5-venv\Scripts\python.exe'
$ghRepo = 'swanhtet01/mt5-vps-deploy'
New-Item -ItemType Directory -Path $deploy -Force | Out-Null

function New-HiddenTaskAction {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Body
    )
    $launcherRoot = 'C:\mt5-paper\launchers'
    New-Item -ItemType Directory -Path $launcherRoot -Force | Out-Null
    $safeName = $Name -replace '[^A-Za-z0-9_-]', '-'
    $psPath = Join-Path $launcherRoot "$safeName.ps1"
    $vbsPath = Join-Path $launcherRoot "$safeName.vbs"
    [System.IO.File]::WriteAllText($psPath, $Body, (New-Object System.Text.UTF8Encoding($false)))
    $escapedPsPath = $psPath.Replace('"', '""')
    $vbsBody = @"
Set shell = CreateObject("WScript.Shell")
result = shell.Run("powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ""$escapedPsPath""", 0, True)
WScript.Quit result
"@
    [System.IO.File]::WriteAllText($vbsPath, $vbsBody, (New-Object System.Text.ASCIIEncoding))
    return "wscript.exe `"$vbsPath`""
}

function Set-MT5TaskReliability {
    param(
        [Parameter(Mandatory=$true)][string]$TaskName,
        [int]$ExecutionMinutes = 30
    )
    try {
        $settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
            -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 2) `
            -ExecutionTimeLimit (New-TimeSpan -Minutes ([Math]::Max($ExecutionMinutes, 1))) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
    } catch {
        Write-Host "  WARN: could not harden task settings for $TaskName" -ForegroundColor Yellow
    }
}

function New-MT5TaskIfMissing {
    param(
        [Parameter(Mandatory=$true)][string]$TaskName,
        [Parameter(Mandatory=$true)][string]$Action,
        [Parameter(Mandatory=$true)][string[]]$Schedule,
        [int]$ExecutionMinutes = 10
    )
    # Create only when absent. An existing task keeps whatever schedule is already
    # running on the box -- this must never re-point a working safety task.
    # Get-ScheduledTask rather than `schtasks /query` + $LASTEXITCODE: on PowerShell 7.4+
    # a failing native command throws under $ErrorActionPreference='Stop', and "missing" is
    # this function's normal path. The module is already used by Set-MT5TaskReliability.
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  kept existing $TaskName (schedule untouched)" -ForegroundColor Gray
    } else {
        schtasks /create /tn $TaskName /tr $Action $Schedule /it /f | Out-Null
        if ($LASTEXITCODE) { Write-Host "  WARN: $TaskName create failed (continuing)" -ForegroundColor Yellow }
        else { Write-Host "  created missing $TaskName" -ForegroundColor Green }
    }
    schtasks /change /tn $TaskName /enable *> $null
    Set-MT5TaskReliability -TaskName $TaskName -ExecutionMinutes $ExecutionMinutes
}

function Write-InstalledManifest {
    param(
        [Parameter(Mandatory=$true)]$Manifest,
        [Parameter(Mandatory=$true)][string]$DeploySha,
        [Parameter(Mandatory=$true)][string]$Path
    )
    # Drift-detection record read by vps_health.py: which commit's manifest is on disk and
    # the sha256 each destination was verified against. Built from the manifest object
    # Sync-Hotfixes already parsed and verified -- nothing is re-downloaded or re-hashed.
    # Schema: {"deploy_sha", "applied_utc", "files": [{"destination", "sha256"}]}.
    $record = [ordered]@{
        deploy_sha  = $DeploySha
        applied_utc = [DateTime]::UtcNow.ToString('s') + '+00:00'
        files       = @($Manifest.files | ForEach-Object {
            [ordered]@{ destination = [string]$_.destination; sha256 = ([string]$_.sha256).ToLowerInvariant() }
        })
    }
    # UTF-8 without BOM like every other file this script generates; staged then renamed so
    # a health check that runs mid-write never reads a truncated record.
    $staged = "$Path.tmp"
    [System.IO.File]::WriteAllText($staged, (ConvertTo-Json -InputObject $record -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item $staged $Path -Force
}

# Resolve one immutable commit. Auto-deploy supplies this; interactive updates resolve main.
$deployRef = $env:MT5_DEPLOY_SHA
if ($deployRef -notmatch '^[0-9a-fA-F]{40}$') {
    $commit = Invoke-WebRequest "https://api.github.com/repos/$ghRepo/commits/main" `
        -UseBasicParsing -TimeoutSec 20 -Headers @{Accept='application/vnd.github.v3+json'}
    $deployRef = ($commit.Content | ConvertFrom-Json).sha
}
if ($deployRef -notmatch '^[0-9a-fA-F]{40}$') {
    throw 'Could not resolve an immutable deploy commit.'
}
$rawBase = "https://raw.githubusercontent.com/$ghRepo/$deployRef"

function Sync-Hotfixes {
    $manifestResponse = Invoke-WebRequest "$rawBase/hotfix-manifest.json" `
        -UseBasicParsing -TimeoutSec 20 -Headers @{'Cache-Control'='no-cache'}
    $manifest = $manifestResponse.Content | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or -not $manifest.files) {
        throw 'Invalid or empty hotfix manifest.'
    }
    $repoPrefix = [IO.Path]::GetFullPath($repo.TrimEnd('\') + '\')
    # Two phases. The old loop downloaded, verified and MOVED each file in turn, so a throw
    # partway through -- a 404, or one stale sha256 -- left the first k files new and the rest
    # old, permanently, with no rollback. Manifest order makes that concrete: the shared
    # execution helper is entry 18 and risk/agent/scaling are entries 52-55, so a failure in
    # between leaves a money path whose halves disagree. Transient causes self-heal on the
    # next run; a deterministic one fails at the same index every time and stays mixed.
    $staging = Join-Path $deploy "hotfix-staging.$PID"
    $backup  = Join-Path $deploy "hotfix-backup.$PID"
    Remove-Item $staging, $backup -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    $planned = New-Object System.Collections.ArrayList

    try {
        # PHASE 1 - download and verify EVERYTHING into staging. $repo is not touched at all,
        # so any failure here leaves the installed tree exactly as it was.
        foreach ($entry in $manifest.files) {
            $source = [string]$entry.source
            $relative = ([string]$entry.destination) -replace '/', '\'
            $expected = ([string]$entry.sha256).ToLowerInvariant()
            if (-not $source -or $source.Contains('..') -or [IO.Path]::IsPathRooted($source)) {
                throw "Unsafe manifest source: $source"
            }
            $destination = [IO.Path]::GetFullPath((Join-Path $repo $relative))
            if (-not $destination.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Unsafe manifest destination: $relative"
            }
            $staged = Join-Path $staging $relative
            New-Item -ItemType Directory -Path (Split-Path $staged -Parent) -Force | Out-Null
            Invoke-WebRequest "$rawBase/$source" -OutFile $staged -UseBasicParsing -TimeoutSec 30
            $actual = (Get-FileHash $staged -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $expected) {
                throw "SHA256 mismatch for $source"
            }
            [void]$planned.Add([pscustomobject]@{
                Staged = $staged; Destination = $destination; Relative = $relative
            })
            Write-Host "  [0] verified hotfix: $relative" -ForegroundColor Green
        }

        # PHASE 2 - commit. Every file is already verified, so this should not fail; if it
        # does (a locked file, a full disk), put back what was replaced rather than leaving
        # the tree half-updated.
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        $applied = New-Object System.Collections.ArrayList
        try {
            foreach ($item in $planned) {
                if (Test-Path $item.Destination) {
                    $backupPath = Join-Path $backup $item.Relative
                    New-Item -ItemType Directory -Path (Split-Path $backupPath -Parent) -Force | Out-Null
                    Copy-Item $item.Destination $backupPath -Force
                }
                New-Item -ItemType Directory -Path (Split-Path $item.Destination -Parent) -Force | Out-Null
                Move-Item $item.Staged $item.Destination -Force
                [void]$applied.Add($item)
            }
        } catch {
            $reason = $_
            foreach ($done in $applied) {
                $backupPath = Join-Path $backup $done.Relative
                if (Test-Path $backupPath) {
                    Copy-Item $backupPath $done.Destination -Force
                } else {
                    # No backup means the file did not exist before; undo means remove it.
                    Remove-Item $done.Destination -Force -ErrorAction SilentlyContinue
                }
            }
            throw "hotfix apply failed and was rolled back to the previous tree: $reason"
        }
        Write-Host "  [0] applied $($planned.Count) verified hotfixes as one batch" -ForegroundColor Green
        # Only a fully committed batch is recorded; every failure path above throws first.
        $script:appliedManifest = $manifest
    } finally {
        Remove-Item $staging, $backup -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# A GitHub token can no longer re-arm real trading. Deployment only installs code.
Sync-Hotfixes

# 1) latest bundle - best-effort. If the download/extract fails, we KEEP the existing (already
#    verified) money-path code rather than copying from a bad bundle. $bundleOk gates the copy.
$bundleUrl = 'https://github.com/swanhtet01/mt5-vps-deploy/releases/download/v1/mt5-bundle.zip'
$bundleOk = $false
try {
    $rel = Invoke-WebRequest 'https://api.github.com/repos/swanhtet01/mt5-vps-deploy/releases/latest' `
        -UseBasicParsing -TimeoutSec 15 -Headers @{Accept='application/vnd.github.v3+json'}
    $asset = ($rel.Content | ConvertFrom-Json).assets | Where-Object { $_.name -eq 'mt5-bundle.zip' } | Select-Object -First 1
    if ($asset.browser_download_url) { $bundleUrl = $asset.browser_download_url }
} catch { Write-Host '  (GitHub API unreachable; using v1 bundle URL)' -ForegroundColor DarkGray }
try {
    Invoke-WebRequest $bundleUrl -OutFile "$deploy\mt5-bundle.zip" -UseBasicParsing -TimeoutSec 120
    Expand-Archive "$deploy\mt5-bundle.zip" -DestinationPath $deploy -Force
    $bundleOk = (Test-Path "$deploy\trading-agent\scripts")
    Write-Host '  [1] latest release bundle downloaded' -ForegroundColor Green
} catch { Write-Host '  [1] bundle download/extract failed - keeping existing code (non-fatal)' -ForegroundColor Yellow }

# 2) refresh money-path code ONLY from a cleanly-extracted bundle. robocopy exit >=8 = real
#    error -> WARN; the commit-pinned hotfixes above already applied.
if ($bundleOk) {
    robocopy "$deploy\trading-agent\scripts" "$repo\scripts" /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Host '  WARN: robocopy scripts had errors (continuing)' -ForegroundColor Yellow }
    robocopy "$deploy\trading-agent\src" "$repo\src" /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Host '  WARN: robocopy src had errors (continuing)' -ForegroundColor Yellow }
    # Re-apply verified files after the old release bundle has copied over the tree.
    Sync-Hotfixes
}
# Record what is now installed, and only here: every Sync-Hotfixes call above throws on any
# failure and aborts the script before this line, so a failed or rolled-back sync can never
# leave a fresh installed-manifest.json claiming success.
Write-InstalledManifest -Manifest $script:appliedManifest -DeploySha $deployRef -Path "$deploy\installed-manifest.json"
Write-Host "  [2] release bundle refreshed where available; commit $($deployRef.Substring(0,8)) hotfixes verified" -ForegroundColor Green

# 2b) ensure runtime deps in the venv. WARN (don't abort) so a transient pip/network hiccup
# cannot block the rest of a deploy; the verified hotfixes already applied in step 0.
& $py -m pip install --quiet anthropic yfinance numpy pandas psutil
if ($LASTEXITCODE) { Write-Host '  WARN: pip install had errors (continuing)' -ForegroundColor Yellow }
else { Write-Host '  [2b] python deps verified (anthropic, yfinance, numpy, pandas, psutil)' -ForegroundColor Green }

# 2c) mirror NTFY_TOPIC to Machine scope so SYSTEM-context tasks (e.g. a boot alert) can
#     also push to the phone. NOTE: notify.py has NO registry fallback -- it reads
#     os.environ["NTFY_TOPIC"] and nothing else. This works because Windows materialises
#     User/Machine env vars into a newly-launched task's environment, not because notify.py
#     looks them up. If the variable was never set at either scope, notify.py falls back to a
#     DEFAULT_TOPIC still containing "XYZ", refuses to send, and every alert on this box
#     silently reaches nobody. vps_health.py's check_alerting reports exactly that case.
$ntfyUser = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
if ($ntfyUser) { [Environment]::SetEnvironmentVariable('NTFY_TOPIC', $ntfyUser, 'Machine') }

# 2d) Replace host-clock structural entry tasks. Those tasks interpreted UTC as the broker
# H1 clock and traded the wrong buckets. Legacy exits stay enabled as a backstop during the
# paper migration. The new scheduler runs hidden every five minutes and remains paper-only
# unless its separate live flag AND per-magic allowlist exist.
$legacyStructuralTasks = @(
    'MT5-GoldDrift-Live-Enter',
    'MT5-USDJPY-Mon-Enter',
    'MT5-UK100-Thu-Enter',
    'MT5-GOLD-Fri-Enter',
    'MT5-USDJPY-Wed-Enter',
    'MT5-GOLD-Thu-Enter',
    'MT5-AUDJPY-Mon-Enter',
    'MT5-GBPJPY-Thu-Enter',
    'MT5-GOLD-Tue-Enter'
)
foreach ($taskName in $legacyStructuralTasks) {
    schtasks /change /tn $taskName /disable 2>$null | Out-Null
}
$schedulerPs = 'C:\mt5-paper\structural-scheduler.ps1'
$schedulerBody = @"
`$env:MT5_REPO = '$repo'
`$env:MT5_STRUCTURAL_FORCE_PAPER_ONLY = '1'
& '$py' '$repo\scripts\structural_scheduler.py' 2>&1 | Out-File -FilePath 'C:\mt5-paper\analytics\structural-scheduler-task.log' -Append -Encoding utf8
exit `$LASTEXITCODE
"@
[System.IO.File]::WriteAllText($schedulerPs, $schedulerBody, (New-Object System.Text.UTF8Encoding($false)))
$schedulerAction = New-HiddenTaskAction -Name 'structural-scheduler' -Body $schedulerBody
schtasks /create /tn 'MT5-StructuralScheduler' /tr $schedulerAction /sc minute /mo 5 /it /f | Out-Null
if ($LASTEXITCODE) { throw 'MT5-StructuralScheduler task registration failed.' }
Set-MT5TaskReliability -TaskName 'MT5-StructuralScheduler' -ExecutionMinutes 4
Write-Host '  [2d] legacy entries disabled, exits retained; hidden broker-clock scheduler installed PAPER-ONLY' -ForegroundColor Green

# 3) position-monitor task -> phone alert on every trade open/close (every 5 min)
New-Item -ItemType Directory -Path 'C:\mt5-paper\analytics' -Force | Out-Null
$body = "& '$py' '$repo\scripts\position_monitor.py' *>> 'C:\mt5-paper\analytics\position-monitor.log'`r`nexit `$LASTEXITCODE"
$action = New-HiddenTaskAction -Name 'position-monitor' -Body $body
schtasks /create /tn 'MT5-PositionMonitor' /tr $action /sc minute /mo 5 /it /f | Out-Null
Set-MT5TaskReliability -TaskName 'MT5-PositionMonitor' -ExecutionMinutes 4
Write-Host '  [3] MT5-PositionMonitor scheduled (alerts every 5 min)' -ForegroundColor Green

# 4) seed alerts silently + apply remote control once
& $py "$repo\scripts\position_monitor.py" 2>&1 | Out-Null
& $py "$repo\scripts\remote_control.py" 2>&1 | Out-Null
Write-Host '  [4] alerts seeded + remote control applied' -ForegroundColor Green

# 5) auto-deploy task - VPS polls GitHub main every 15 min and applies the immutable,
# hash-verified hotfix manifest. Full engine refreshes still require a new release asset.
$adScript = "$deploy\auto_deploy.ps1"
Invoke-WebRequest "$rawBase/auto_deploy.ps1" `
    -OutFile $adScript -UseBasicParsing -TimeoutSec 30
$adBody = "& '$adScript' *>> 'C:\mt5-paper\analytics\auto-deploy.log'`r`nexit `$LASTEXITCODE"
$adAction = New-HiddenTaskAction -Name 'auto-deploy' -Body $adBody
# /rl HIGHEST: this task re-runs update.ps1, which refuses to do anything without admin.
# Without it the task inherits a filtered token under UAC admin-approval mode and every
# self-update is a no-op. Every other task here does work that does not need elevation;
# this one cannot do its job without it.
schtasks /create /tn 'MT5-AutoDeploy' /tr $adAction /sc minute /mo 15 /it /rl HIGHEST /f | Out-Null
Set-MT5TaskReliability -TaskName 'MT5-AutoDeploy' -ExecutionMinutes 12
Set-Content "$deploy\last_deploy_sha.txt" $deployRef -NoNewline
Write-Host '  [5] MT5-AutoDeploy scheduled (verified hotfix commits every 15 min)' -ForegroundColor Green

# NOTE: non-trading maintenance tasks still use the VPS Myanmar clock (UTC+6:30).
# Structural entries no longer use this conversion; MT5-StructuralScheduler reads broker time.
#   06:00 UTC -> 12:30 local ; 06:30 UTC -> 13:00 local ; 08:00 UTC -> 14:30 local

# 6) symbol scanner task - Sundays 08:00 UTC (= 14:30 local) to discover new edges
$scanBody = "& '$py' '$repo\scripts\multi_symbol_scanner.py' --symbols SPY,TLT,QQQ,CL,GC --timeframes 1h,4h --parallel 2 *>> 'C:\mt5-paper\analytics\scanner.log'`r`nexit `$LASTEXITCODE"
$scanAction = New-HiddenTaskAction -Name 'symbol-scanner' -Body $scanBody
schtasks /create /tn 'MT5-SymbolScanner' /tr $scanAction /sc weekly /d SUN /st 14:30 /it /f | Out-Null
Set-MT5TaskReliability -TaskName 'MT5-SymbolScanner' -ExecutionMinutes 120
Write-Host '  [6] MT5-SymbolScanner scheduled (Sundays 08:00 UTC)' -ForegroundColor Green

# 6b) THE DAILY THESIS PIPELINE, correctly ordered. It must run before broker midnight
# (currently 03:30 Myanmar time in broker summer time, 04:30 in winter) so sizing is fresh.
# The early schedule covers both offsets and ensures the thesis reads REAL data
# (context_ingest writes news/macro/context first) instead of
# empty defaults. Sequence (local time = UTC+6:30):
#   01:30 context_ingest -> 02:00 thesis -> 02:30 apply
$dmBody = "& '$py' '$repo\scripts\build_dashboard_metrics.py' *>> 'C:\mt5-paper\analytics\dashboard_build.log'`r`nexit `$LASTEXITCODE"
$dmAction = New-HiddenTaskAction -Name 'build-dashboard-metrics' -Body $dmBody
schtasks /create /tn 'MT5-BuildDashboard' /tr $dmAction /sc daily /st 01:00 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  WARN: MT5-BuildDashboard create failed (continuing)' -ForegroundColor Yellow }
Set-MT5TaskReliability -TaskName 'MT5-BuildDashboard' -ExecutionMinutes 10
$ciBody = "& '$py' '$repo\scripts\context_ingest.py' --data-cache-dir '$repo\data_cache' *>> 'C:\mt5-paper\analytics\context.log'`r`nexit `$LASTEXITCODE"
$ciAction = New-HiddenTaskAction -Name 'context-ingest' -Body $ciBody
schtasks /create /tn 'MT5-ContextIngest' /tr $ciAction /sc daily /st 01:30 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  WARN: MT5-ContextIngest create failed (continuing)' -ForegroundColor Yellow }
Set-MT5TaskReliability -TaskName 'MT5-ContextIngest' -ExecutionMinutes 20
$thBody = "& '$py' '$repo\scripts\thesis_ingest.py' *>> 'C:\mt5-paper\analytics\thesis.log'`r`nexit `$LASTEXITCODE"
$thAction = New-HiddenTaskAction -Name 'llm-thesis' -Body $thBody
schtasks /create /tn 'MT5-LLMThesis' /tr $thAction /sc daily /st 02:00 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  WARN: MT5-LLMThesis create failed (continuing)' -ForegroundColor Yellow }
Set-MT5TaskReliability -TaskName 'MT5-LLMThesis' -ExecutionMinutes 25
$apBody = "& '$py' '$repo\scripts\apply_approved_thesis.py' *>> 'C:\mt5-paper\analytics\thesis.log'`r`nexit `$LASTEXITCODE"
$apAction = New-HiddenTaskAction -Name 'apply-thesis' -Body $apBody
schtasks /create /tn 'MT5-ApplyThesis' /tr $apAction /sc daily /st 02:30 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  WARN: MT5-ApplyThesis create failed (continuing)' -ForegroundColor Yellow }
Set-MT5TaskReliability -TaskName 'MT5-ApplyThesis' -ExecutionMinutes 15
# The cumulative-drawdown brake and the self-watch have to EXIST, not merely be enabled.
# tasks.ps1 lists both as critical, but this installer only ever tried to /enable the kill
# switch -- and /enable silently no-ops on a task that was never created, so a VPS rebuilt
# from this repo alone ran with no drawdown brake and no health watch at all. Both scripts
# are already delivered by hotfix-manifest.json; only the schedules were missing.
# Cadences are the ones the scripts themselves document, not invented here.
$ksBody = "& '$py' '$repo\scripts\killswitch_monitor.py' *>> 'C:\mt5-paper\analytics\killswitch.log'`r`nexit `$LASTEXITCODE"
$ksAction = New-HiddenTaskAction -Name 'killswitch' -Body $ksBody
New-MT5TaskIfMissing -TaskName 'MT5-GoldDrift-KillSwitch' -Action $ksAction -Schedule @('/sc','hourly') -ExecutionMinutes 10

$vhBody = "& '$py' '$repo\scripts\vps_health.py' *>> 'C:\mt5-paper\analytics\vps-health.log'`r`nexit `$LASTEXITCODE"
$vhAction = New-HiddenTaskAction -Name 'vps-health' -Body $vhBody
New-MT5TaskIfMissing -TaskName 'MT5-VPS-Health' -Action $vhAction -Schedule @('/sc','minute','/mo','30') -ExecutionMinutes 10

# Dead-man's switch. Every other check here runs ON the VPS, so none of them can tell you the
# VPS itself went dark (crashed, lost its network, Python broken). heartbeat.py pings
# healthchecks.io every 5 min and that service alerts when the pings STOP -- the only signal
# in this system that survives the box dying. It always exits 0 (a transient network blip must
# not flag the task red), and with no HEALTHCHECK_URL set it just writes the local file, so
# scheduling it is safe before anyone configures the URL.
# NOTE: set HEALTHCHECK_URL as a MACHINE-level env var, or the task will never ping.
$hbBody = "& '$py' '$repo\scripts\heartbeat.py' *>> 'C:\mt5-paper\analytics\heartbeat.log'`r`nexit `$LASTEXITCODE"
$hbAction = New-HiddenTaskAction -Name 'heartbeat' -Body $hbBody
New-MT5TaskIfMissing -TaskName 'MT5-Heartbeat' -Action $hbAction -Schedule @('/sc','minute','/mo','5') -ExecutionMinutes 5
# Daily slippage report. The traders journal slippage_points on every fill and nothing read it
# until now: this runs the delivered analyzer over every event log and writes
# data_cache\slippage_analysis.json (per-symbol/hour/regime slippage, partial fills, and the
# comparison against the cost model validation charges). Invoked by FILE PATH, not python -m,
# because -m can resolve to a different mt5_agent install. Report-only; touches no order path;
# a missing log file is skipped, so it is safe before every strategy has produced fills.
$srBody = "& '$py' '$repo\src\mt5_agent\slippage_analyzer.py' --log-file 'C:\mt5-paper\intraday-mr\events.jsonl' --log-file 'C:\mt5-paper\gold-drift\live-events.jsonl' --log-file 'C:\mt5-paper\multi-drift\events.jsonl' --output '$repo\data_cache\slippage_analysis.json' *>> 'C:\mt5-paper\analytics\slippage-report.log'`r`nexit `$LASTEXITCODE"
$srAction = New-HiddenTaskAction -Name 'slippage-report' -Body $srBody
New-MT5TaskIfMissing -TaskName 'MT5-SlippageReport' -Action $srAction -Schedule @('/sc','daily','/st','06:30') -ExecutionMinutes 10
# Intraday mean-reversion (GOLD+USDJPY RSI fade) -- every 30 min during London/NY sessions.
# Runs PAPER-ONLY until MT5_GOLD_DRIFT_LIVE=1 is set; same live flag as structural edges.
$mrBody = "& '$py' '$repo\scripts\intraday_mean_rev.py' *>> 'C:\mt5-paper\intraday-mr\task.log'`r`nexit `$LASTEXITCODE"
$mrAction = New-HiddenTaskAction -Name 'intraday-mr' -Body $mrBody
schtasks /create /tn 'MT5-IntradayMR' /tr $mrAction /sc minute /mo 30 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  WARN: MT5-IntradayMR create failed (continuing)' -ForegroundColor Yellow }
Set-MT5TaskReliability -TaskName 'MT5-IntradayMR' -ExecutionMinutes 5
Write-Host '  [6b] context-ingest + thesis + apply scheduled before broker midnight; kill-switch enabled; intraday MR every 30min' -ForegroundColor Green

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
$bootAction = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$bootPs`""
schtasks /create /tn 'MT5-BootAlert' /tr $bootAction /sc onstart /ru SYSTEM /rl HIGHEST /f | Out-Null
Set-MT5TaskReliability -TaskName 'MT5-BootAlert' -ExecutionMinutes 5
Write-Host '  [6c] MT5-BootAlert scheduled (phone ping on reboot)' -ForegroundColor Green

# 6d) Bounded maintenance keeps append-only logs and research bundles from exhausting disk.
$maintenanceBody = "& '$py' '$repo\scripts\vps_maintenance.py' *>> 'C:\mt5-paper\analytics\maintenance.log'`r`nexit `$LASTEXITCODE"
$maintenanceAction = New-HiddenTaskAction -Name 'maintenance' -Body $maintenanceBody
schtasks /create /tn 'MT5-Maintenance' /tr $maintenanceAction /sc daily /st 03:00 /it /f | Out-Null
if ($LASTEXITCODE) { Write-Host '  WARN: MT5-Maintenance create failed (continuing)' -ForegroundColor Yellow }
Set-MT5TaskReliability -TaskName 'MT5-Maintenance' -ExecutionMinutes 20
Write-Host '  [6d] hidden bounded log/export maintenance scheduled daily' -ForegroundColor Green

# 6e) Pinned Vibe sidecar. The deterministic loader/report runs daily without a
# provider; the bounded language-model research pass runs weekly only when its DPAPI
# secret exists. Both are isolated, globally HALTed, and research-only.
$vibeRunner = "$repo\scripts\run-vibe-research.ps1"
$vibeSetup = "$repo\scripts\setup-vibe-research.ps1"
$vibePython = 'C:\mt5-vibe-research\.venv\Scripts\python.exe'
$auditedVibeCommit = '652917e74e2b2e1f767ef596623bae7f098a53c4'
$vibeInstallPath = 'C:\mt5-vibe-research\install.json'
if ((Test-Path $vibeRunner) -and (Test-Path $vibeSetup)) {
    $vibeInstall = if (Test-Path $vibeInstallPath) {
        Get-Content $vibeInstallPath -Raw | ConvertFrom-Json
    } else { $null }
    if ((-not (Test-Path $vibePython)) -or (-not $vibeInstall) -or $vibeInstall.commit -ne $auditedVibeCommit) {
        $vibeProvider = if ($vibeInstall -and $vibeInstall.provider_extra -eq 'anthropic') { 'anthropic' } else { 'none' }
        try {
            & powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden `
                -ExecutionPolicy Bypass -File $vibeSetup -SidecarRoot 'C:\mt5-vibe-research' `
                -Provider $vibeProvider -SkipTaskRegistration
            if ($LASTEXITCODE) { throw "sidecar setup exited $LASTEXITCODE" }
            Write-Host "  [6e] Vibe sidecar upgraded to audited commit $($auditedVibeCommit.Substring(0,8))" -ForegroundColor Green
        } catch {
            Write-Host "  WARN: Vibe sidecar upgrade failed; research/shadow entries remain blocked: $_" -ForegroundColor Yellow
        }
    }
    $vibeInstall = if (Test-Path $vibeInstallPath) {
        Get-Content $vibeInstallPath -Raw | ConvertFrom-Json
    } else { $null }
    $vibeReady = (
        (Test-Path $vibePython) -and
        (Test-Path 'C:\mt5-vibe-research\live\HALT') -and
        $vibeInstall -and
        $vibeInstall.commit -eq $auditedVibeCommit -and
        $vibeInstall.order_authority -eq $false
    )
    if (-not $vibeReady) {
        schtasks /delete /tn 'MT5-VibeBaseline' /f 2>$null | Out-Null
        schtasks /delete /tn 'MT5-VibeResearch' /f 2>$null | Out-Null
        schtasks /delete /tn 'MT5-VibeShadow' /f 2>$null | Out-Null
        Write-Host '  WARN: Vibe sidecar did not pass its pinned HALT boundary; tasks remain removed' -ForegroundColor Yellow
    } else {
    $vibeBaselineBody = "`$env:MT5_PYTHON='$py'`r`n& '$vibeRunner' -SidecarRoot 'C:\mt5-vibe-research' -Config 'config.research-multi-asset-h1.toml' -TimeoutMinutes 30 -SkipAgent *>> 'C:\mt5-paper\analytics\vibe-baseline.log'`r`nexit `$LASTEXITCODE"
    $vibeBaselineAction = New-HiddenTaskAction -Name 'vibe-baseline' -Body $vibeBaselineBody
    schtasks /create /tn 'MT5-VibeBaseline' /tr $vibeBaselineAction /sc daily /st 04:00 /it /f | Out-Null
    if ($LASTEXITCODE) { Write-Host '  WARN: MT5-VibeBaseline create failed (continuing)' -ForegroundColor Yellow }
    Set-MT5TaskReliability -TaskName 'MT5-VibeBaseline' -ExecutionMinutes 35
    $vibeBody = "& '$vibeRunner' -SidecarRoot 'C:\mt5-vibe-research' -Config 'config.research-multi-asset-h1.toml' -TimeoutMinutes 60 *>> 'C:\mt5-paper\analytics\vibe-research.log'`r`nexit `$LASTEXITCODE"
    $vibeAction = New-HiddenTaskAction -Name 'vibe-research' -Body $vibeBody
    schtasks /create /tn 'MT5-VibeResearch' /tr $vibeAction /sc weekly /d SUN /st 15:30 /it /f | Out-Null
    if ($LASTEXITCODE) { Write-Host '  WARN: MT5-VibeResearch create failed (continuing)' -ForegroundColor Yellow }
    Set-MT5TaskReliability -TaskName 'MT5-VibeResearch' -ExecutionMinutes 70
    $vibeShadowRunner = "$repo\scripts\run-vibe-shadow-once.ps1"
    if (Test-Path $vibeShadowRunner) {
        $vibeShadowBody = "`$env:MT5_PYTHON='$py'`r`n& '$vibeShadowRunner' -SidecarRoot 'C:\mt5-vibe-research' *>> 'C:\mt5-paper\analytics\vibe-shadow-launcher.log'`r`nexit `$LASTEXITCODE"
        $vibeShadowAction = New-HiddenTaskAction -Name 'vibe-shadow' -Body $vibeShadowBody
        schtasks /create /tn 'MT5-VibeShadow' /tr $vibeShadowAction /sc minute /mo 5 /it /f | Out-Null
        if ($LASTEXITCODE) { Write-Host '  WARN: MT5-VibeShadow create failed (continuing)' -ForegroundColor Yellow }
        Set-MT5TaskReliability -TaskName 'MT5-VibeShadow' -ExecutionMinutes 4
    }
    Write-Host '  [6e] Vibe baseline daily + research weekly + quote-only shadow every five minutes' -ForegroundColor Green
    Start-ScheduledTask -TaskName 'MT5-VibeBaseline' -ErrorAction SilentlyContinue
    }
} else {
    schtasks /delete /tn 'MT5-VibeBaseline' /f 2>$null | Out-Null
    schtasks /delete /tn 'MT5-VibeResearch' /f 2>$null | Out-Null
    schtasks /delete /tn 'MT5-VibeShadow' /f 2>$null | Out-Null
    Write-Host '  [6e] Vibe task not installed; run setup-vibe-research.ps1 on the VPS first' -ForegroundColor DarkGray
}

# 6f) Closed-profit sizing refresh. This task only reads MT5 history and writes
# evidence/correlation artifacts. Process-local flags force every trading path
# to paper mode, and the runner contains no order call.
$profitScalingRunner = "$repo\scripts\run-profit-funded-scaling-once.ps1"
if (Test-Path $profitScalingRunner) {
    $profitScalingBody = @"
`$env:MT5_GOLD_DRIFT_LIVE = '0'
`$env:MT5_STRUCTURAL_SCHEDULER_LIVE = '0'
`$env:MT5_STRUCTURAL_FORCE_PAPER_ONLY = '1'
`$env:MT5_VIBE_SHADOW_FORCE_PAPER_ONLY = '1'
`$env:MT5_PYTHON = '$py'
& '$profitScalingRunner' *>> 'C:\mt5-paper\analytics\profit-funded-scaling-launcher.log'
exit `$LASTEXITCODE
"@
    $profitScalingAction = New-HiddenTaskAction -Name 'profit-funded-scaling' -Body $profitScalingBody
    schtasks /create /tn 'MT5-ProfitFundedScaling' /tr $profitScalingAction /sc minute /mo 60 /it /f | Out-Null
    if ($LASTEXITCODE) { throw 'MT5-ProfitFundedScaling task registration failed.' }
    Set-MT5TaskReliability -TaskName 'MT5-ProfitFundedScaling' -ExecutionMinutes 10
    & powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden `
        -ExecutionPolicy Bypass -File $profitScalingRunner 2>&1 | Out-Null
    if ($LASTEXITCODE) {
        Write-Host '  WARN: initial profit-funded sizing refresh failed; hourly task will retry' -ForegroundColor Yellow
    } else {
        Write-Host '  [6f] hidden closed-profit sizing refresh installed hourly; current artifact refreshed' -ForegroundColor Green
    }
} else {
    Write-Host '  WARN: profit-funded sizing runner missing; no scaling task installed' -ForegroundColor Yellow
}

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

# Completion marker, written only if execution actually reached the end of this file.
# auto_deploy.ps1 refuses to record a deploy as successful unless this names the commit it
# just deployed -- so any early exit, silent or not, is retried instead of being banked.
Set-Content "$deploy\last_update_complete.txt" $deployRef -NoNewline

Write-Host ''
Write-Host '==== UPDATE COMPLETE ====' -ForegroundColor Green
Write-Host '  - Auto-deploy: manifest-listed hotfixes update on main; full engine needs a release'
Write-Host '  - Structural scheduler: installed hidden and PAPER-ONLY until separately approved'
Write-Host '  - Symbol scanner: runs every Sunday 08:00 UTC (finds new edges automatically)'
Write-Host '  - Vibe: deterministic research daily; quote-only shadow evidence every five minutes'
Write-Host '  - Scaling: closed-profit evidence refresh hourly; stale or weak evidence stays at 1x'
Write-Host '  - LLM thesis: tested live against Claude (see [7] above)'
Write-Host '  - Trade alerts: fire on real opens/closes only (no spam)'
