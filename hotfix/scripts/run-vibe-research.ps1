[CmdletBinding()]
param(
    [string]$SidecarRoot = "C:\mt5-vibe-research",
    [string]$Config = "config.research-multi-asset-h1.toml",
    [string[]]$Symbols = @(
        "GOLD", "SILVER", "OILCash", "BTCUSD", "ETHUSD", "US500Cash", "USDJPY",
        "UK100Cash", "AUDJPY", "GBPJPY", "EURUSD", "GBPUSD", "GER40Cash", "JP225Cash"
    ),
    [string]$StructuralReport = "",
    [int]$TimeoutMinutes = 60,
    [switch]$SkipAgent
)

$ErrorActionPreference = "Stop"
$AuditedCommit = "652917e74e2b2e1f767ef596623bae7f098a53c4"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectPython = @(
    $env:MT5_PYTHON,
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    "C:\mt5-venv\Scripts\python.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
$SidecarPython = Join-Path $SidecarRoot ".venv\Scripts\python.exe"
$VendorRoot = Join-Path $SidecarRoot "vendor\Vibe-Trading"
$LogsRoot = Join-Path $SidecarRoot "logs"
$ExportsRoot = Join-Path $SidecarRoot "exports"
$ReportsRoot = Join-Path $SidecarRoot "reports"
$StatePath = Join-Path $SidecarRoot "last-run.json"
$BaselineStatePath = Join-Path $SidecarRoot "last-baseline.json"
$AgentStatePath = Join-Path $SidecarRoot "last-agent-run.json"
$LockPath = Join-Path $SidecarRoot "research.lock"

function Quote-NativeArgument {
    param([Parameter(Mandatory=$true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Start-BoundedPython {
    param(
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [Parameter(Mandatory=$true)][string]$StdoutPath,
        [Parameter(Mandatory=$true)][string]$StderrPath,
        [Parameter(Mandatory=$true)][int]$Minutes
    )
    $Process = Start-Process -FilePath $SidecarPython -ArgumentList $Arguments `
        -WindowStyle Hidden -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath -PassThru
    try { $Process.PriorityClass = "BelowNormal" } catch {}
    if (-not $Process.WaitForExit([Math]::Max($Minutes, 1) * 60 * 1000)) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        throw "Vibe child process exceeded $Minutes minutes"
    }
    # The timed overload can return before redirected stream handlers finish and
    # before PowerShell exposes ExitCode. The parameterless wait completes both.
    $Process.WaitForExit()
    $Process.Refresh()
    $ExitCode = $Process.ExitCode
    # Windows PowerShell can expose a null ExitCode for Start-Process children
    # even when HasExited is true. Each caller therefore also validates a strict
    # JSON output contract; a crash cannot be mistaken for a successful run.
    if ($null -ne $ExitCode -and $ExitCode -ne 0) {
        $Tail = if (Test-Path -LiteralPath $StderrPath) {
            (Get-Content -LiteralPath $StderrPath -Tail 5) -join " | "
        } else { "no stderr captured" }
        throw "Vibe child process exited with code ${ExitCode}: $Tail"
    }
}

function Write-JsonState {
    param(
        [Parameter(Mandatory=$true)][hashtable]$Payload,
        [Parameter(Mandatory=$true)][string]$Path
    )
    $Payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding utf8
}

New-Item -ItemType Directory -Force -Path $LogsRoot, $ExportsRoot, $ReportsRoot | Out-Null
$Lock = $null
try {
    try {
        $Lock = [IO.File]::Open($LockPath, "OpenOrCreate", "ReadWrite", "None")
    } catch [IO.IOException] {
        Write-Host "Another Vibe research run is active; skipping."
        exit 0
    }
    if (-not $ProjectPython) { throw "Project Python missing" }
    if (-not (Test-Path -LiteralPath $SidecarPython)) { throw "Run setup-vibe-research.ps1 first" }
    $ResearchEntry = Join-Path $PSScriptRoot "vibe_research_entry.py"
    $BaselineEntry = Join-Path $PSScriptRoot "vibe_deterministic_research.py"
    $ScreenEntry = Join-Path $PSScriptRoot "vibe_candidate_screen.py"
    if (-not (Test-Path -LiteralPath $ResearchEntry)) { throw "Safe Vibe research adapter missing" }
    if (-not (Test-Path -LiteralPath $BaselineEntry)) { throw "Deterministic Vibe baseline missing" }
    if (-not (Test-Path -LiteralPath $ScreenEntry)) { throw "Deterministic Vibe candidate screen missing" }
    $ActualCommit = (& git -C $VendorRoot rev-parse HEAD).Trim()
    if ($ActualCommit -ne $AuditedCommit) { throw "Audited Vibe commit mismatch" }

    $ExportArguments = @(
        (Join-Path $PSScriptRoot "export_vibe_research_bundle.py"),
        "--config", (Join-Path $ProjectRoot $Config),
        "--out-root", $ExportsRoot,
        "--timeframe", "H1",
        "--bars", "5000",
        "--history-days", "365"
    )
    if ($Symbols.Count -gt 0) {
        $ExportArguments += "--symbols"
        $ExportArguments += $Symbols
    }
    & $ProjectPython @ExportArguments
    if ($LASTEXITCODE -ne 0) { throw "Sanitized MT5 export failed" }

    $Pointer = Get-Content -LiteralPath (Join-Path $ExportsRoot "latest.json") -Raw | ConvertFrom-Json
    $Bundle = [IO.Path]::GetFullPath([string]$Pointer.bundle)
    $ExportsPrefix = [IO.Path]::GetFullPath($ExportsRoot.TrimEnd('\') + '\')
    if (-not $Bundle.StartsWith($ExportsPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Bundle pointer escaped the sidecar export root"
    }
    $ManifestPath = Join-Path $Bundle "manifest.json"
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($Manifest.mode -ne "research_only" -or $Manifest.order_authority -ne $false) {
        throw "Bundle research boundary is invalid"
    }

    $env:VIBE_TRADING_HOME = $SidecarRoot
    $env:VIBE_TRADING_ENABLE_SHELL_TOOLS = "0"
    $env:VIBE_TRADING_DATA_CACHE = "0"
    $env:VIBE_TRADING_ALLOWED_FILE_ROOTS = $Bundle
    $env:VIBE_TRADING_HYPOTHESES_PATH = Join-Path $SidecarRoot "hypotheses.json"
    $HomeRoot = Join-Path $SidecarRoot "home"
    $env:HOME = $HomeRoot
    $env:USERPROFILE = $HomeRoot
    $BridgeRoot = Join-Path $HomeRoot ".vibe-trading\data-bridge"
    New-Item -ItemType Directory -Force -Path $BridgeRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $Bundle "data-bridge\config.yaml") `
        -Destination (Join-Path $BridgeRoot "config.yaml") -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $SidecarRoot "live") | Out-Null
    if (-not (Test-Path -LiteralPath (Join-Path $SidecarRoot "live\HALT"))) {
        throw "Research sidecar HALT sentinel is missing"
    }

    $ToolAuditPath = Join-Path $SidecarRoot "tool-audit.json"
    $ToolAuditOutput = & $SidecarPython $ResearchEntry --audit-tools
    if ($LASTEXITCODE -ne 0) { throw "Safe Vibe tool audit failed" }
    $ToolAuditOutput | Set-Content -LiteralPath $ToolAuditPath -Encoding utf8
    $ToolAudit = Get-Content -LiteralPath $ToolAuditPath -Raw | ConvertFrom-Json
    if ($ToolAudit.order_authority -ne $false -or [int]$ToolAudit.tool_count -lt 1) {
        throw "Safe Vibe tool audit reported an invalid research boundary"
    }

    $Stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $ReportDir = Join-Path $ReportsRoot $Stamp
    while (Test-Path -LiteralPath $ReportDir) {
        Start-Sleep -Seconds 1
        $Stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
        $ReportDir = Join-Path $ReportsRoot $Stamp
    }
    $BaselineStdoutPath = Join-Path $LogsRoot "baseline-$Stamp.stdout.log"
    $BaselineStderrPath = Join-Path $LogsRoot "baseline-$Stamp.stderr.log"
    $BaselineArguments = @(
        (Quote-NativeArgument $BaselineEntry),
        "--bundle", (Quote-NativeArgument $Bundle),
        "--output-dir", (Quote-NativeArgument $ReportDir),
        "--maximum-candidates", "8"
    )
    $ResolvedStructuralReport = $null
    if ($StructuralReport) {
        $ResolvedStructuralReport = [IO.Path]::GetFullPath($StructuralReport)
    } else {
        $ResolvedStructuralReport = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "reports") `
            -Filter "structural-walk-forward*.json" -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if ($ResolvedStructuralReport -and (Test-Path -LiteralPath $ResolvedStructuralReport)) {
        $BaselineArguments += "--structural-report"
        $BaselineArguments += Quote-NativeArgument $ResolvedStructuralReport
    }
    Start-BoundedPython -Arguments $BaselineArguments -StdoutPath $BaselineStdoutPath `
        -StderrPath $BaselineStderrPath -Minutes ([Math]::Min([Math]::Max($TimeoutMinutes, 5), 30))
    $BaselineSummary = Get-Content -LiteralPath $BaselineStdoutPath -Raw | ConvertFrom-Json
    if ($BaselineSummary.order_authority -ne $false -or -not (Test-Path -LiteralPath $BaselineSummary.report)) {
        throw "Deterministic Vibe baseline returned an invalid boundary or missing report"
    }
    $BaselineReport = Get-Content -LiteralPath $BaselineSummary.report -Raw | ConvertFrom-Json
    if (
        $BaselineReport.schema -ne "mt5.vibe_deterministic_research.v1" -or
        $BaselineReport.mode -ne "research_only" -or
        $BaselineReport.order_authority -ne $false -or
        $BaselineReport.data_quality_status -ne "PASS"
    ) {
        throw "Deterministic Vibe baseline failed its report contract"
    }
    $ActualBaselineHash = (Get-FileHash -LiteralPath $BaselineSummary.report -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualBaselineHash -ne ([string]$BaselineSummary.report_sha256).ToLowerInvariant()) {
        throw "Deterministic Vibe baseline report hash mismatch"
    }
    $ScreenOutput = Join-Path $ReportDir "candidate-screen.json"
    $ScreenStdoutPath = Join-Path $LogsRoot "screen-$Stamp.stdout.log"
    $ScreenStderrPath = Join-Path $LogsRoot "screen-$Stamp.stderr.log"
    $ScreenArguments = @(
        (Quote-NativeArgument $ScreenEntry),
        "--bundle", (Quote-NativeArgument $Bundle),
        "--handoff", (Quote-NativeArgument ([string]$BaselineSummary.handoff)),
        "--output", (Quote-NativeArgument $ScreenOutput)
    )
    Start-BoundedPython -Arguments $ScreenArguments -StdoutPath $ScreenStdoutPath `
        -StderrPath $ScreenStderrPath -Minutes ([Math]::Min([Math]::Max($TimeoutMinutes, 5), 30))
    $ScreenSummary = Get-Content -LiteralPath $ScreenStdoutPath -Raw | ConvertFrom-Json
    if (
        $ScreenSummary.schema -ne "mt5.vibe_candidate_screen.v1" -or
        $ScreenSummary.status -ne "completed" -or
        $ScreenSummary.order_authority -ne $false -or
        [int]$ScreenSummary.paper_candidate_count -ne 0 -or
        [int]$ScreenSummary.live_eligible_count -ne 0 -or
        -not (Test-Path -LiteralPath $ScreenSummary.report)
    ) {
        throw "Vibe candidate screen returned an invalid boundary or missing report"
    }
    $ScreenReport = Get-Content -LiteralPath $ScreenSummary.report -Raw | ConvertFrom-Json
    if (
        $ScreenReport.schema -ne "mt5.vibe_candidate_screen.v1" -or
        $ScreenReport.mode -ne "historical_research_only" -or
        $ScreenReport.order_authority -ne $false -or
        $ScreenReport.automatic_live_promotion -ne $false -or
        $ScreenReport.forecast_generated -ne $false -or
        [int]$ScreenReport.paper_candidate_count -ne 0 -or
        [int]$ScreenReport.live_eligible_count -ne 0
    ) {
        throw "Vibe candidate screen failed its report contract"
    }
    $ActualScreenHash = (Get-FileHash -LiteralPath $ScreenSummary.report -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualScreenHash -ne ([string]$ScreenSummary.report_sha256).ToLowerInvariant()) {
        throw "Vibe candidate screen report hash mismatch"
    }
    $FinishedAt = [DateTime]::UtcNow.ToString("o")
    $BaselineState = @{
        schema = "mt5.vibe_baseline_run.v1"
        status = "completed"
        finished_at = $FinishedAt
        bundle = $Bundle
        report = [string]$BaselineSummary.report
        report_sha256 = [string]$BaselineSummary.report_sha256
        handoff = [string]$BaselineSummary.handoff
        handoff_sha256 = [string]$BaselineSummary.handoff_sha256
        chart = [string]$BaselineSummary.chart
        symbols_loaded = [int]$BaselineSummary.symbols_loaded
        candidate_count = [int]$BaselineSummary.candidate_count
        candidate_screen = [string]$ScreenSummary.report
        candidate_screen_sha256 = [string]$ScreenSummary.report_sha256
        historical_screen_trials = [int]$ScreenSummary.family_trials
        historical_screen_pass_count = [int]$ScreenSummary.historical_screen_pass_count
        paper_candidate_count = 0
        live_eligible_count = 0
        order_authority = $false
    }
    Write-JsonState -Payload $BaselineState -Path $BaselineStatePath

    $SecretPath = Join-Path $SidecarRoot "secrets\anthropic.dpapi"
    $ProviderReady = Test-Path -LiteralPath $SecretPath
    if ($SkipAgent -or -not $ProviderReady) {
        $SkipReason = if ($SkipAgent) { "scheduled_baseline_only" } else { "provider_secret_missing" }
        Write-JsonState -Path $StatePath -Payload @{
            schema = "mt5.vibe_research_run.v2"
            status = "completed"
            baseline_status = "completed"
            agent_status = "skipped"
            agent_reason = $SkipReason
            provider_ready = $ProviderReady
            bundle = $Bundle
            report = [string]$BaselineSummary.report
            handoff = [string]$BaselineSummary.handoff
            candidate_screen = [string]$ScreenSummary.report
            historical_screen_pass_count = [int]$ScreenSummary.historical_screen_pass_count
            paper_candidate_count = 0
            live_eligible_count = 0
            tool_audit = $ToolAuditPath
            finished_at = [DateTime]::UtcNow.ToString("o")
            order_authority = $false
        }
        Write-Host "Deterministic Vibe baseline completed; agent stage skipped ($SkipReason)."
        exit 0
    }

    $Secure = Get-Content -LiteralPath $SecretPath -Raw | ConvertTo-SecureString
    $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { $env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Ptr) }
    $env:LANGCHAIN_PROVIDER = "anthropic"
    $Install = Get-Content -LiteralPath (Join-Path $SidecarRoot "install.json") -Raw | ConvertFrom-Json
    $env:LANGCHAIN_MODEL_NAME = if ($Install.model) { [string]$Install.model } else { "claude-sonnet-4-6" }
    $env:ANTHROPIC_MAX_TOKENS = "8192"

    $Template = Get-Content -LiteralPath (Join-Path $ProjectRoot "research\vibe_research_prompt.txt") -Raw
    $LocalSymbols = (($Manifest.research_scope.symbols | ForEach-Object { [string]$_ }) -join ",")
    $Prompt = $Template.Replace("{{BUNDLE_PATH}}", $Bundle).Replace("{{LOCAL_SOURCE_SYMBOLS}}", $LocalSymbols)
    $PromptPath = Join-Path $SidecarRoot "prompt-$Stamp.txt"
    $StdoutPath = Join-Path $LogsRoot "research-$Stamp.stdout.log"
    $StderrPath = Join-Path $LogsRoot "research-$Stamp.stderr.log"
    $CandidateOutput = Join-Path $ReportDir "agent-candidate-handoff.json"
    Set-Content -LiteralPath $PromptPath -Value $Prompt -Encoding utf8

    $Arguments = @(
        (Quote-NativeArgument $ResearchEntry),
        "--prompt-file", (Quote-NativeArgument $PromptPath),
        "--bundle-manifest", (Quote-NativeArgument $ManifestPath),
        "--candidate-output", (Quote-NativeArgument $CandidateOutput),
        "--max-iter", "20"
    )
    Start-BoundedPython -Arguments $Arguments -StdoutPath $StdoutPath `
        -StderrPath $StderrPath -Minutes $TimeoutMinutes
    $AgentSummary = Get-Content -LiteralPath $StdoutPath -Raw | ConvertFrom-Json
    if ($AgentSummary.status -ne "success" -or -not (Test-Path -LiteralPath $CandidateOutput)) {
        throw "Vibe agent did not produce a schema-valid candidate handoff"
    }
    $AgentHash = (Get-FileHash -LiteralPath $CandidateOutput -Algorithm SHA256).Hash.ToLowerInvariant()
    $AgentState = @{
        schema = "mt5.vibe_agent_run.v1"
        status = "completed"
        provider = "anthropic"
        finished_at = [DateTime]::UtcNow.ToString("o")
        bundle = $Bundle
        baseline_report = [string]$BaselineSummary.report
        candidate_screen = [string]$ScreenSummary.report
        candidate_screen_sha256 = [string]$ScreenSummary.report_sha256
        candidate_handoff = $CandidateOutput
        candidate_handoff_sha256 = $AgentHash
        candidate_count = [int]$AgentSummary.candidate_count
        run_id = [string]$AgentSummary.run_id
        run_dir = [string]$AgentSummary.run_dir
        order_authority = $false
        automatic_live_promotion = $false
    }
    Write-JsonState -Payload $AgentState -Path $AgentStatePath
    Write-JsonState -Path $StatePath -Payload @{
        schema = "mt5.vibe_research_run.v2"
        status = "completed"
        baseline_status = "completed"
        agent_status = "completed"
        provider_ready = $true
        bundle = $Bundle
        report = [string]$BaselineSummary.report
        deterministic_handoff = [string]$BaselineSummary.handoff
        candidate_screen = [string]$ScreenSummary.report
        historical_screen_pass_count = [int]$ScreenSummary.historical_screen_pass_count
        paper_candidate_count = 0
        live_eligible_count = 0
        agent_handoff = $CandidateOutput
        tool_audit = $ToolAuditPath
        finished_at = [DateTime]::UtcNow.ToString("o")
        order_authority = $false
    }
} catch {
    Write-JsonState -Path $StatePath -Payload @{
        schema = "mt5.vibe_research_run.v2"
        status = "failed"
        error = $_.Exception.Message
        finished_at = [DateTime]::UtcNow.ToString("o")
        order_authority = $false
    }
    throw
} finally {
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
    if ($null -ne $Lock) { $Lock.Dispose() }
}
