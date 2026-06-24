# auto_deploy.ps1 - Polls GitHub main for new commits; re-runs update.ps1 when the deploy
# scripts change. Runs every 15 min as MT5-AutoDeploy. Silent when nothing changed.
#
# Watches the main branch HEAD commit SHA (not release tags) so ordinary commits to
# update.ps1 deploy automatically - no need to cut a GitHub release. Sets MT5_AUTODEPLOY=1
# so update.ps1 skips its interactive thesis self-test (no phone push on every deploy).

$ErrorActionPreference = 'SilentlyContinue'
$sha_file = 'C:\mt5-deploy\last_deploy_sha.txt'
$log      = 'C:\mt5-paper\analytics\auto-deploy.log'
$gh_repo  = 'swanhtet01/mt5-vps-deploy'

function Log($msg) {
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss UTC')
    "$ts  $msg" | Add-Content $log
}

try {
    $resp = Invoke-WebRequest "https://api.github.com/repos/$gh_repo/commits/main" `
        -UseBasicParsing -TimeoutSec 15 -Headers @{Accept='application/vnd.github.v3+json'}
    $sha = ($resp.Content | ConvertFrom-Json).sha
} catch {
    Log "GitHub check failed: $_"
    exit 0
}

if (-not $sha) { exit 0 }
$last = if (Test-Path $sha_file) { (Get-Content $sha_file -Raw).Trim() } else { '' }
if ($sha -eq $last) { exit 0 }   # nothing new - silent, no log spam

Log "New main commit $($sha.Substring(0,8)) (was '$last') - deploying..."
try {
    $env:MT5_AUTODEPLOY = '1'   # update.ps1 sees this and skips the thesis self-test (no phone spam)
    $update = Invoke-WebRequest "https://raw.githubusercontent.com/$gh_repo/main/update.ps1" `
        -UseBasicParsing -TimeoutSec 30
    Invoke-Expression $update.Content
    Set-Content $sha_file $sha -NoNewline   # only advance the marker on a clean run
    Log "Deploy complete - now at $($sha.Substring(0,8))"
} catch {
    Log "Deploy FAILED (will retry next poll): $_"
}
