# layer2.ps1 - one-and-done Layer-2: remote-control channel + audit fix.
# Run in an ADMIN PowerShell on the VPS:  irm is.gd/<short> | iex
#
# After this runs ONCE, the VPS can be controlled without the VNC: editing the GitHub
# control.json disables/enables any edge (or pauses all) within 5 minutes, because the
# bot honors the same blacklist this writes. Config-pull only - never executes remote code.

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== MT5 VPS LAYER-2 (remote control + audit fix) ====' -ForegroundColor Cyan
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'NOT admin. Open Start -> Windows PowerShell (Admin) and paste the command again.' -ForegroundColor Red
    return
}

$repo = 'C:\trading-agent'
$py   = 'C:\mt5-venv\Scripts\python.exe'

# 1) Audit fix: pull GOLD_TUE (magic 88009) off live - it fails honest Bonferroni (t=3.31).
try {
    $g = Get-ScheduledTask -TaskName 'MT5-GOLD-Tue-*' -ErrorAction SilentlyContinue
    if ($g) { $g | Disable-ScheduledTask | Out-Null; Write-Host "  [1] GOLD_TUE tasks disabled ($($g.Count))" -ForegroundColor Green }
    else { Write-Host '  [1] no MT5-GOLD-Tue tasks found (already gone)' -ForegroundColor Yellow }
} catch { Write-Warning "  [1] $_" }

# 2) Install the remote-control agent (config-pull -> blacklist the traders already honor)
try {
    Invoke-WebRequest 'https://raw.githubusercontent.com/swanhtet01/mt5-vps-deploy/main/remote_control.py' `
        -OutFile "$repo\scripts\remote_control.py" -UseBasicParsing -TimeoutSec 60
    Write-Host '  [2] remote_control.py installed' -ForegroundColor Green
} catch { Write-Warning "  [2] download failed: $_"; return }

# 2b) Upgrade the phone-summary to plain English
try {
    Invoke-WebRequest 'https://raw.githubusercontent.com/swanhtet01/mt5-vps-deploy/main/notify.py' `
        -OutFile "$repo\scripts\notify.py" -UseBasicParsing -TimeoutSec 60
    Write-Host '  [2b] notify.py upgraded (plain-English phone summaries)' -ForegroundColor Green
} catch { Write-Warning "  [2b] notify.py update skipped: $_" }

# 3) Apply it once now (so GOLD_TUE is blacklisted immediately), then schedule every 5 min
try {
    $cmd = 'C:\mt5-paper\remote-control-tick.cmd'
    $body = "@echo off`r`n`"$py`" `"$repo\scripts\remote_control.py`" >> `"C:\mt5-paper\analytics\remote-control.log`" 2>&1"
    New-Item -ItemType Directory -Path 'C:\mt5-paper\analytics' -Force | Out-Null
    [System.IO.File]::WriteAllText($cmd, $body, (New-Object System.Text.ASCIIEncoding))
    & $py "$repo\scripts\remote_control.py"
    schtasks /create /tn 'MT5-RemoteControl' /tr $cmd /sc minute /mo 5 /it /f | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host '  [3] applied now + MT5-RemoteControl scheduled (every 5 min)' -ForegroundColor Green }
    else { Write-Warning "  [3] schtasks exit $LASTEXITCODE" }
} catch { Write-Warning "  [3] $_" }

# 4) Confirm to phone
try {
    $topic = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
    if ($topic -and (Test-Path $py)) {
        $env:NTFY_TOPIC = $topic
        & $py "$repo\scripts\notify.py" 'Layer-2 installed: remote control live, GOLD_TUE demoted' 2>$null
        Write-Host "  [4] Confirmation sent to ntfy '$topic'" -ForegroundColor Green
    }
} catch { Write-Warning "  [4] notify: $_" }

Write-Host ''
Write-Host '==== LAYER-2 COMPLETE ====' -ForegroundColor Green
Write-Host '  - GOLD_TUE (88009) is off live (failed Bonferroni)'
Write-Host '  - The bot now polls GitHub control.json every 5 min.'
Write-Host '  - To disable/enable any edge or pause-all from now on: just edit control.json'
Write-Host '    (no VNC needed). Current blacklist:'
try { & $py -c "import sys; sys.path.insert(0,r'$repo\scripts'); from paths import BLACKLIST_FILE, read_json; import json; print(json.dumps([{k:e.get(k) for k in ('symbol','magic','reason')} for e in (read_json(BLACKLIST_FILE) or {}).get('entries',[])], indent=2))" } catch {}
