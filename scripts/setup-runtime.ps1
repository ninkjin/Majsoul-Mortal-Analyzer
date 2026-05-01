$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root "runtime"
$python = Join-Path $runtime "python.exe"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "System Python was not found. Install Python 3.12 first, then rerun this script."
}

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
  throw "Rust/Cargo was not found. Install Rust from https://rustup.rs first, then rerun this script."
}

if (-not (Test-Path $python)) {
  Write-Host "Creating portable runtime: $runtime"
  python -m venv $runtime
}

Write-Host "Upgrading pip..."
& $python -m pip install --upgrade pip

Write-Host "Installing Python dependencies..."
& $python -m pip install -r (Join-Path $root "requirements-runtime.txt")

Write-Host "Building and installing libriichi..."
& $python -m maturin develop --release --manifest-path (Join-Path $root "libriichi\Cargo.toml")

Write-Host ""
Write-Host "Runtime is ready: $python"
Write-Host "You can now run the analyzer launcher without Docker Desktop."
