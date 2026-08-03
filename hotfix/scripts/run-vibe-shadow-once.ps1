[CmdletBinding()]
param(
    [string]$SidecarRoot = "C:\mt5-vibe-research",
    [string]$LogFile = "C:\mt5-paper\analytics\vibe-shadow-task.log"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = @(
    $env:MT5_PYTHON,
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    "C:\mt5-venv\Scripts\python.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $Python) { throw "MT5 project Python was not found" }

$ResolvedLog = if ([IO.Path]::IsPathRooted($LogFile)) {
    $LogFile
} else {
    Join-Path $ProjectRoot $LogFile
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ResolvedLog) | Out-Null

# The shadow runner has no broker-order code. These process-local values also
# clear inherited live flags before it imports any project module.
$env:MT5_GOLD_DRIFT_LIVE = "0"
$env:MT5_STRUCTURAL_SCHEDULER_LIVE = "0"
$env:MT5_STRUCTURAL_FORCE_PAPER_ONLY = "1"
$env:MT5_VIBE_SHADOW_FORCE_PAPER_ONLY = "1"
$env:MT5_VIBE_ROOT = $SidecarRoot
$env:MT5_REPO = $ProjectRoot

& $Python (Join-Path $PSScriptRoot "vibe_shadow_forward.py") `
    --sidecar-root $SidecarRoot 2>&1 |
    Out-File -LiteralPath $ResolvedLog -Append -Encoding utf8
exit $LASTEXITCODE
