$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root "runtime\python.exe"
if (-not (Test-Path $python)) {
  $python = Join-Path $root "runtime\Scripts\python.exe"
}
if (-not (Test-Path $python)) {
  $python = Join-Path $root ".conda\python.exe"
}
if (-not (Test-Path $python)) {
  $python = "python"
}

Write-Host "Starting local Mahjong Soul paipu analyzer..."
Write-Host "Open: http://127.0.0.1:8765/paipu-analyzer.html"

& $python .\tools\paipu_server\server.py --host 127.0.0.1 --port 8765
