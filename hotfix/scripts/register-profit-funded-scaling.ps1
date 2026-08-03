[CmdletBinding()]
param(
    [int]$IntervalMinutes = 60,
    [string]$TaskName = "MT5-ProfitFundedScaling",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
if ($IntervalMinutes -lt 15) { throw "IntervalMinutes must be at least 15" }

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunOnce = Join-Path $PSScriptRoot "run-profit-funded-scaling-once.ps1"
$StateDir = Join-Path $ProjectRoot "state"
$SafeTaskName = $TaskName -replace '[^A-Za-z0-9_.-]', '_'
$TaskRunner = Join-Path $StateDir "$SafeTaskName-paper-runner.vbs"
if (-not (Test-Path -LiteralPath $RunOnce)) { throw "Profit-funded scaling runner is missing" }
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Quote-CommandArgument {
    param([string]$Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

$Command = @(
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle",
    "Hidden",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $RunOnce
) | ForEach-Object { Quote-CommandArgument $_ }
$RunnerCommand = ($Command -join " ").Replace('"', '""')
$RunnerScript = @"
Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run("$RunnerCommand", 0, True)
"@
[IO.File]::WriteAllText($TaskRunner, $RunnerScript, [Text.Encoding]::ASCII)

$TaskToRun = '\"wscript.exe\" \"' + $TaskRunner + '\"'
$CreateCommand = (
    'schtasks /Create /TN "' + ($TaskName -replace '"', '\"') + '" ' +
    '/SC MINUTE /MO ' + $IntervalMinutes + ' ' +
    '/TR "' + $TaskToRun + '" /F'
)
cmd.exe /c $CreateCommand
if ($LASTEXITCODE -ne 0) {
    throw "schtasks failed to register $TaskName with exit code $LASTEXITCODE"
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -Hidden `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable
Set-ScheduledTask -TaskName $TaskName -Settings $Settings | Out-Null

if ($StartNow) { Start-ScheduledTask -TaskName $TaskName }
Write-Host "Registered hidden read-only sizing task $TaskName every $IntervalMinutes minutes."
Write-Host "It cannot send orders and fails to 1x when its evidence artifact is stale or invalid."
