[CmdletBinding()]
param(
    [string]$LogFile = "C:\mt5-paper\analytics\profit-funded-scaling-task.log"
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

# This task reads closed history and writes sizing artifacts. It has no order call.
$env:MT5_GOLD_DRIFT_LIVE = "0"
$env:MT5_STRUCTURAL_SCHEDULER_LIVE = "0"
$env:MT5_STRUCTURAL_FORCE_PAPER_ONLY = "1"
$env:MT5_VIBE_SHADOW_FORCE_PAPER_ONLY = "1"
$env:MT5_REPO = $ProjectRoot

Push-Location $ProjectRoot
try {
    & $Python (Join-Path $PSScriptRoot "dynamic_sizing.py") 2>&1 |
        Out-File -LiteralPath $ResolvedLog -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $Python -m mt5_agent.portfolio_budget 2>&1 |
        Out-File -LiteralPath $ResolvedLog -Append -Encoding utf8
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
