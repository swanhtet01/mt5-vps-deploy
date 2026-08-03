[CmdletBinding()]
param(
    [string]$SidecarRoot = "C:\mt5-vibe-research",
    [ValidateSet("anthropic")]
    [string]$Provider = "anthropic"
)

$ErrorActionPreference = "Stop"
$SecretRoot = Join-Path $SidecarRoot "secrets"
New-Item -ItemType Directory -Force -Path $SecretRoot | Out-Null
$Secret = Read-Host "Enter the $Provider API key (input is hidden)" -AsSecureString
if ($Secret.Length -lt 16) { throw "The supplied secret is unexpectedly short" }
$SecretPath = Join-Path $SecretRoot "$Provider.dpapi"
$Secret | ConvertFrom-SecureString | Set-Content -LiteralPath $SecretPath -Encoding ascii
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $SecretPath /inheritance:r /grant:r "${Identity}:(R,W)" "SYSTEM:(R)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict the provider secret ACL" }
Write-Host "Stored $Provider key with Windows DPAPI for this Windows user: $SecretPath"
