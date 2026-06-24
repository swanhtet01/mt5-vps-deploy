# auto_deploy.ps1 - Polls GitHub releases; re-deploys when a new bundle is published.
# Runs every 15 min as MT5-AutoDeploy scheduled task. Silent when nothing changed.
# To trigger a deploy: push a new GitHub Release (v2, v3, ...) on swanhtet01/mt5-vps-deploy.

$ErrorActionPreference = 'SilentlyContinue'
$tag_file = 'C:\mt5-deploy\last_release_tag.txt'
$log      = 'C:\mt5-paper\analytics\auto-deploy.log'
$gh_repo  = 'swanhtet01/mt5-vps-deploy'

function Log($msg) {
    $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss UTC')
    "$ts  $msg" | Add-Content $log
}

try {
    $rel = Invoke-WebRequest "https://api.github.com/repos/$gh_repo/releases/latest" `
        -UseBasicParsing -TimeoutSec 15 -Headers @{Accept='application/vnd.github.v3+json'}
    $tag = ($rel.Content | ConvertFrom-Json).tag_name
} catch {
    Log "GitHub check failed: $_"
    exit 0
}

if (-not $tag) { Log 'No release tag found'; exit 0 }

$last = if (Test-Path $tag_file) { (Get-Content $tag_file -Raw).Trim() } else { '' }

if ($tag -eq $last) {
    # Nothing new — silent exit (no log spam)
    exit 0
}

Log "New release detected: $tag (was: '$last') — starting deploy..."

try {
    # Re-run the standard update script (downloads bundle, refreshes code, re-seeds tasks)
    $update = Invoke-WebRequest 'https://raw.githubusercontent.com/swanhtet01/mt5-vps-deploy/main/update.ps1' `
        -UseBasicParsing -TimeoutSec 30
    Invoke-Expression $update.Content
    Set-Content $tag_file $tag -NoNewline
    Log "Deploy complete — now on $tag"
} catch {
    Log "Deploy FAILED: $_"
}
