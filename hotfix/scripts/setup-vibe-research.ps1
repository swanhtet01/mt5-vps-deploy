[CmdletBinding()]
param(
    [string]$SidecarRoot = "C:\mt5-vibe-research",
    [ValidateSet("none", "anthropic")]
    [string]$Provider = "none",
    [string]$Model = "claude-sonnet-4-6",
    [switch]$SkipTaskRegistration
)

$ErrorActionPreference = "Stop"
$AuditedCommit = "652917e74e2b2e1f767ef596623bae7f098a53c4"
$Repository = "https://github.com/HKUDS/Vibe-Trading.git"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectPython = @(
    $env:MT5_PYTHON,
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    "C:\mt5-venv\Scripts\python.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
$VendorRoot = Join-Path $SidecarRoot "vendor\Vibe-Trading"
$VenvRoot = Join-Path $SidecarRoot ".venv"

if (-not $ProjectPython) {
    throw "Project Python not found in the project venv, MT5_PYTHON, or C:\mt5-venv"
}
New-Item -ItemType Directory -Force -Path $SidecarRoot | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $VendorRoot ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VendorRoot) | Out-Null
    & git clone --filter=blob:none $Repository $VendorRoot
    if ($LASTEXITCODE -ne 0) { throw "Vibe Trading clone failed" }
}
& git -C $VendorRoot fetch --depth 1 origin $AuditedCommit
if ($LASTEXITCODE -ne 0) { throw "Vibe Trading audited commit fetch failed" }
& git -C $VendorRoot checkout --detach $AuditedCommit
if ($LASTEXITCODE -ne 0) { throw "Vibe Trading audited commit checkout failed" }
$ActualCommit = (& git -C $VendorRoot rev-parse HEAD).Trim()
if ($ActualCommit -ne $AuditedCommit) {
    throw "Vibe Trading commit mismatch: expected $AuditedCommit, got $ActualCommit"
}

if (-not (Test-Path -LiteralPath (Join-Path $VenvRoot "Scripts\python.exe"))) {
    & $ProjectPython -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Sidecar virtual environment creation failed" }
}
$SidecarPython = Join-Path $VenvRoot "Scripts\python.exe"
& $SidecarPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed" }
$InstallTarget = $VendorRoot
if ($Provider -eq "anthropic") { $InstallTarget = "${VendorRoot}[anthropic]" }
& $SidecarPython -m pip install --disable-pip-version-check --no-cache-dir $InstallTarget
if ($LASTEXITCODE -ne 0) { throw "Vibe Trading install failed" }
& $SidecarPython -c "import importlib.util,sys; sys.exit(1 if importlib.util.find_spec('MetaTrader5') else 0)"
if ($LASTEXITCODE -ne 0) { throw "Research sidecar unexpectedly contains the MetaTrader5 package" }

$FreezePath = Join-Path $SidecarRoot "installed-packages.txt"
& $SidecarPython -m pip freeze | Set-Content -LiteralPath $FreezePath -Encoding utf8
New-Item -ItemType Directory -Force -Path (Join-Path $SidecarRoot "live") | Out-Null
@{
    reason = "Research sidecar has no live order authority"
    created_at = [DateTime]::UtcNow.ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $SidecarRoot "live\HALT") -Encoding utf8
@{
    schema = "mt5.vibe_sidecar_install.v1"
    repository = $Repository
    commit = $ActualCommit
    provider_extra = $Provider
    model = $Model
    installed_at = [DateTime]::UtcNow.ToString("o")
    order_authority = $false
    deterministic_baseline = $true
    historical_candidate_screen = $true
    agent_stage_optional = $true
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $SidecarRoot "install.json") -Encoding utf8

Write-Host "Installed audited Vibe Trading research sidecar at $SidecarRoot"
Write-Host "No MT5 connector extra or broker credentials were installed."

if (-not $SkipTaskRegistration) {
    $Runner = Join-Path $PSScriptRoot "run-vibe-research.ps1"
    $LauncherRoot = 'C:\mt5-paper\launchers'
    New-Item -ItemType Directory -Force -Path $LauncherRoot | Out-Null
    function New-VibeTaskLauncher {
        param(
            [Parameter(Mandatory=$true)][string]$Name,
            [Parameter(Mandatory=$true)][string]$Body
        )
        $TaskPs = Join-Path $LauncherRoot "$Name.ps1"
        $TaskVbs = Join-Path $LauncherRoot "$Name.vbs"
        [IO.File]::WriteAllText($TaskPs, $Body, (New-Object Text.UTF8Encoding($false)))
        $EscapedTaskPs = $TaskPs.Replace('"', '""')
        $VbsBody = @"
Set shell = CreateObject("WScript.Shell")
result = shell.Run("powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ""$EscapedTaskPs""", 0, True)
WScript.Quit result
"@
        [IO.File]::WriteAllText($TaskVbs, $VbsBody, (New-Object Text.ASCIIEncoding))
        return "wscript.exe `"$TaskVbs`""
    }

    $BaselineBody = "& '$Runner' -SidecarRoot '$SidecarRoot' -Config 'config.research-multi-asset-h1.toml' -TimeoutMinutes 30 -SkipAgent *>> 'C:\mt5-paper\analytics\vibe-baseline.log'`r`nexit `$LASTEXITCODE"
    $BaselineAction = New-VibeTaskLauncher -Name "vibe-baseline" -Body $BaselineBody
    schtasks /create /tn 'MT5-VibeBaseline' /tr $BaselineAction /sc daily /st 04:00 /it /f | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "MT5-VibeBaseline task registration failed" }

    $ResearchBody = "& '$Runner' -SidecarRoot '$SidecarRoot' -Config 'config.research-multi-asset-h1.toml' -TimeoutMinutes 60 *>> 'C:\mt5-paper\analytics\vibe-research.log'`r`nexit `$LASTEXITCODE"
    $ResearchAction = New-VibeTaskLauncher -Name "vibe-research" -Body $ResearchBody
    schtasks /create /tn 'MT5-VibeResearch' /tr $ResearchAction /sc weekly /d SUN /st 15:30 /it /f | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "MT5-VibeResearch task registration failed" }

    try {
        $BaselineSettings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
            -StartWhenAvailable -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5) `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 35)
        Set-ScheduledTask -TaskName 'MT5-VibeBaseline' -Settings $BaselineSettings | Out-Null
        $ResearchSettings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
            -StartWhenAvailable -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5) `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 70)
        Set-ScheduledTask -TaskName 'MT5-VibeResearch' -Settings $ResearchSettings | Out-Null
    } catch { Write-Host "WARN: tasks registered, but reliability settings were not applied." }
    $ShadowRegister = Join-Path $PSScriptRoot "register-vibe-shadow.ps1"
    if (-not (Test-Path -LiteralPath $ShadowRegister)) {
        throw "Vibe shadow task registration script is missing"
    }
    & $ShadowRegister -IntervalMinutes 5 -SidecarRoot $SidecarRoot -StartNow
    if ($LASTEXITCODE -ne 0) { throw "MT5-VibeShadow task registration failed" }
    Write-Host "Registered hidden daily baseline, weekly research, and five-minute quote-only shadow tasks."
}
