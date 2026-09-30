[CmdletBinding()]
param(
    [int]$TimeoutMinutes = 45,
    [int]$Bars = 30000
)

# Bounded, read-only launcher for the structural research scan. A broker history call can
# block indefinitely, so this wrapper owns the child process and always records an outcome.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = @(
    $env:MT5_PYTHON,
    (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
    'C:\mt5-venv\Scripts\python.exe'
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $Python) { throw 'MT5 Python runtime is unavailable' }

$DataCache = Join-Path $ProjectRoot 'data_cache'
$Reports = Join-Path $ProjectRoot 'reports'
$Logs = 'C:\mt5-paper\analytics'
$StatePath = Join-Path $DataCache 'structural_walk_forward_state.json'
New-Item -ItemType Directory -Force -Path $DataCache, $Reports, $Logs | Out-Null
$Stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$Output = Join-Path $Reports "structural-walk-forward-$Stamp.json"
$Stdout = Join-Path $Logs "structural-walk-forward-$Stamp.stdout.log"
$Stderr = Join-Path $Logs "structural-walk-forward-$Stamp.stderr.log"
$Ledger = Join-Path $DataCache 'fdr_ledger.jsonl'
$Script = Join-Path $PSScriptRoot 'structural_walk_forward.py'

function Write-State([string]$Status, [string]$Reason = '', [string]$ReportSha256 = '') {
    $payload = [ordered]@{
        schema = 'mt5.structural_walk_forward_state.v1'
        status = $Status
        started_at_utc = $started.ToString('o')
        finished_at_utc = [DateTime]::UtcNow.ToString('o')
        timeout_minutes = $TimeoutMinutes
        bars = $Bars
        output = $Output
        report_sha256 = $ReportSha256
        stdout = $Stdout
        stderr = $Stderr
        reason = $Reason
        order_authority = $false
    }
    [IO.File]::WriteAllText($StatePath, ($payload | ConvertTo-Json -Depth 5) + "`n", [Text.UTF8Encoding]::new($false))
}

$started = [DateTime]::UtcNow
try {
    $arguments = @($Script, '--bars', [string][Math]::Max($Bars, 1000), '--fdr-ledger', $Ledger, '--output', $Output)
    $process = Start-Process -FilePath $Python -ArgumentList $arguments -WindowStyle Hidden `
        -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -PassThru
    if (-not $process.WaitForExit([Math]::Max($TimeoutMinutes, 5) * 60 * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Write-State 'timeout' "structural research exceeded $TimeoutMinutes minutes"
        exit 1
    }
    $process.WaitForExit()
    $process.Refresh()
    # Windows PowerShell can expose a null ExitCode for a redirected child even after it
    # has exited. The required report artifact below remains the authoritative success proof.
    $exitCode = $process.ExitCode
    if ($null -ne $exitCode -and $exitCode -ne 0) {
        Write-State 'failed' "structural research exited $exitCode"
        exit $exitCode
    }
    if (-not (Test-Path -LiteralPath $Output)) {
        Write-State 'failed' 'structural research did not produce its report artifact'
        exit 1
    }
    $reportSha256 = (Get-FileHash -LiteralPath $Output -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-State 'completed' '' $reportSha256
} catch {
    Write-State 'failed' $_.Exception.Message
    throw
}
