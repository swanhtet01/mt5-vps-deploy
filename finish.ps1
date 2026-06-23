# finish.ps1 - resume the VPS setup after the bootstrap fix.
# Run in the ADMIN PowerShell on the VPS:  irm is.gd/<short> | iex
# MT5 is already installed + logged in, so this skips straight to:
#   Python install -> project copy -> 33 scheduled tasks -> env vars -> self-test.

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== MT5 VPS FINISH (resume bootstrap) ====' -ForegroundColor Cyan

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'NOT running as Administrator. Open Start -> Windows PowerShell (Admin), then paste the command again.' -ForegroundColor Red
    return
}

$deploy = 'C:\mt5-deploy'
New-Item -ItemType Directory -Path $deploy -Force | Out-Null
$zip = "$deploy\mt5-bundle.zip"

Write-Host 'Downloading corrected bundle...'
Invoke-WebRequest 'https://github.com/swanhtet01/mt5-vps-deploy/releases/download/v1/mt5-bundle.zip' -OutFile $zip -UseBasicParsing -TimeoutSec 120
Write-Host "  Got $((Get-Item $zip).Length) bytes" -ForegroundColor Green

Write-Host 'Extracting (overwrites the broken scripts)...'
Expand-Archive $zip -DestinationPath $deploy -Force

Set-ExecutionPolicy Bypass -Scope Process -Force
Write-Host 'Running bootstrap: Python + project + 33 tasks...' -ForegroundColor Cyan
& "$deploy\vps_bootstrap.ps1"
