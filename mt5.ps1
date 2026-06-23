# mt5.ps1  finds MetaTrader 5 wherever it installed and launches it.
$ErrorActionPreference = 'SilentlyContinue'
Write-Host 'Looking for MetaTrader 5...' -ForegroundColor Cyan
$paths = @(
    'C:\Program Files\MetaTrader 5\terminal64.exe',
    'C:\Program Files\XM Global MT5\terminal64.exe',
    'C:\Program Files (x86)\MetaTrader 5\terminal64.exe',
    "$env:APPDATA\MetaQuotes\Terminal\*\terminal64.exe"
)
$exe = $null
foreach ($p in $paths) {
    $hit = Get-Item $p -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { $exe = $hit.FullName; break }
}
if (-not $exe) {
    Write-Host 'Scanning Program Files (takes a few seconds)...'
    $exe = (Get-ChildItem 'C:\Program Files','C:\Program Files (x86)' -Recurse -Filter terminal64.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
}
if ($exe) {
    Write-Host "FOUND: $exe" -ForegroundColor Green
    Start-Process $exe
    Write-Host 'MT5 is opening. Log in: 314105549 / your XM password / server XMGlobal-MT5' -ForegroundColor Yellow
} else {
    Write-Host 'MetaTrader 5 not found on disk. Tell Claude and we will reinstall it.' -ForegroundColor Red
}
