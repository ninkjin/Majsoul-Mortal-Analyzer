$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".conda\python.exe"

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  throw "Rust/Cargo was not found. Install Rust from https://rustup.rs first, then rerun this script."
}

if (-not (Test-Path $python)) {
  throw ".conda\python.exe was not found. Create a local conda environment at .conda first, then rerun this script."
}

& $python --version | Out-Host

Write-Host "Upgrading pip..."
& $python -m pip install --upgrade pip

Write-Host "Installing Python dependencies..."
& $python -m pip install -r (Join-Path $root "requirements-runtime.txt")

Write-Host "Building and installing libriichi..."
$manifest = Join-Path $root "libriichi\Cargo.toml"
& $python -m maturin build --release --manifest-path $manifest -i $python
$wheel = Get-ChildItem -Path (Join-Path $root "target\wheels") -Filter "libriichi-*-cp*.whl" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if (-not $wheel) {
  throw "libriichi wheel was not built."
}
& $python -m pip install --force-reinstall $wheel.FullName

$sitePackages = (& $python -c "import sysconfig; print(sysconfig.get_path('purelib'))").Trim()
$builtDll = Join-Path $root "target\release\riichi.dll"
if (-not (Test-Path $builtDll)) {
  throw "target\release\riichi.dll was not found after building libriichi."
}
Copy-Item -LiteralPath $builtDll -Destination (Join-Path $sitePackages "libriichi.pyd") -Force

& $python -c "import torch, mahjong, tensoul, numpy; from libriichi.mjai import Bot; print('runtime deps ok')"

Write-Host ""
Write-Host ".conda runtime source is ready: $python"
Write-Host "You can now run scripts\package-portable.ps1 to build the portable zip."
