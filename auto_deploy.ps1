# auto_deploy.ps1 - Polls GitHub main for new commits; re-runs update.ps1 when the deploy
# scripts change. Runs every 15 min as MT5-AutoDeploy. Silent when nothing changed.
#
# Watches the main-branch HEAD commit SHA (not release tags) so ordinary commits deploy
# automatically. Sets MT5_AUTODEPLOY=1 so update.ps1 skips its thesis self-test + phone
# push on auto-runs (no spam). A commit that keeps FAILING is retried a bounded number of
# times, then parked (and the phone is alerted) until a NEW commit lands - no 15-min churn.

$ErrorActionPreference = 'SilentlyContinue'
$deploy    = 'C:\mt5-deploy'
$sha_file  = "$deploy\last_deploy_sha.txt"        # last SUCCESSFUL deploy
$att_file  = "$deploy\last_deploy_attempt.txt"    # "<sha> <count>" for the failing SHA
$log       = 'C:\mt5-paper\analytics\auto-deploy.log'
$gh_repo   = 'swanhtet01/mt5-vps-deploy'
$py        = 'C:\mt5-venv\Scripts\python.exe'
$MAX_TRIES = 3

function Log($msg) {
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss UTC')
    "$ts  $msg" | Add-Content $log
}

try {
    $resp = Invoke-WebRequest "https://api.github.com/repos/$gh_repo/commits/main" `
        -UseBasicParsing -TimeoutSec 15 -Headers @{Accept='application/vnd.github.v3+json'}
    $sha = ($resp.Content | ConvertFrom-Json).sha
} catch { Log "GitHub check failed: $_"; exit 0 }
if (-not $sha) { exit 0 }

$last = if (Test-Path $sha_file) { (Get-Content $sha_file -Raw).Trim() } else { '' }
if ($sha -eq $last) { exit 0 }   # already deployed this commit; silent, no log spam

# How many times have we already tried THIS sha? A new commit resets the retry budget.
$attSha = ''; $attCount = 0
if (Test-Path $att_file) {
    $parts = (Get-Content $att_file -Raw).Trim() -split '\s+'
    $attSha = $parts[0]
    if ($parts.Count -gt 1) { [int]::TryParse($parts[1], [ref]$attCount) | Out-Null }
}
if ($sha -ne $attSha) { $attCount = 0 }
if ($sha -eq $attSha -and $attCount -ge $MAX_TRIES) { exit 0 }   # parked; wait for a new commit

$attCount++
Set-Content $att_file "$sha $attCount" -NoNewline
Log "New main commit $($sha.Substring(0,8)) (deployed='$last', try $attCount/$MAX_TRIES) - deploying..."
try {
    $env:MT5_AUTODEPLOY = '1'   # update.ps1 sees this and stays silent (no thesis push)
    $update = Invoke-WebRequest "https://raw.githubusercontent.com/$gh_repo/main/update.ps1" `
        -UseBasicParsing -TimeoutSec 30
    Invoke-Expression $update.Content
    Set-Content $sha_file $sha -NoNewline                 # mark success
    Remove-Item $att_file -ErrorAction SilentlyContinue
    Log "Deploy complete - now at $($sha.Substring(0,8))"
} catch {
    Log "Deploy FAILED (try $attCount/$MAX_TRIES): $_"
    if ($attCount -ge $MAX_TRIES) {
        Log "Giving up on $($sha.Substring(0,8)) after $MAX_TRIES tries; alerting phone."
        try {
            $env:NTFY_TOPIC = [Environment]::GetEnvironmentVariable('NTFY_TOPIC','User')
            & $py "C:\trading-agent\scripts\notify.py" "VPS auto-deploy FAILED on commit $($sha.Substring(0,8)) after $MAX_TRIES tries - check the VPS." 2>$null
        } catch {}
    }
}
