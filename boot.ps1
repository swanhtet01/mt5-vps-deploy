# boot.ps1 — one-line VPS bootstrap. Run in an ADMIN PowerShell on the VPS:
#   irm is.gd/<short> | iex
# Downloads the deploy bundle, extracts it, and runs the full one-shot installer.

$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host '==== MT5 VPS BOOTSTRAP ====' -ForegroundColor Cyan

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'NOT running as Administrator.' -ForegroundColor Red
    Write-Host 'Close this window. Right-click Start -> Windows PowerShell (Admin) -> Yes, then paste the command again.' -ForegroundColor Yellow
    return
}

$deploy = 'C:\mt5-deploy'
New-Item -ItemType Directory -Path $deploy -Force | Out-Null
$zip = "$deploy\mt5-bundle.zip"

Write-Host 'Downloading deploy bundle (~0.5 MB)...'
$bundleUrl = 'https://github.com/swanhtet01/mt5-vps-deploy/releases/download/v1/mt5-bundle.zip'
Invoke-WebRequest $bundleUrl -OutFile $zip -UseBasicParsing -TimeoutSec 120
Write-Host "  Got $((Get-Item $zip).Length) bytes" -ForegroundColor Green

Write-Host 'Extracting...'
Expand-Archive $zip -DestinationPath $deploy -Force

Set-ExecutionPolicy Bypass -Scope Process -Force
Write-Host 'Launching one-shot installer...' -ForegroundColor Cyan
& "$deploy\vps_one_shot.ps1"
