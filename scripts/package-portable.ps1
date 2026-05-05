$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root "runtime"
$python = Join-Path $runtime "python.exe"
if (-not (Test-Path $python)) {
  $python = Join-Path $runtime "Scripts\python.exe"
}
if (-not (Test-Path $python)) {
  throw "runtime Python was not found. Run scripts\setup-runtime.ps1 first."
}

$dist = Join-Path $root "dist"
$stage = Join-Path $dist "mortal-paipu-analyzer"
$zip = Join-Path $dist "mortal-paipu-analyzer-portable.zip"

if (Test-Path $stage) {
  Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Path $stage | Out-Null

$include = @(
  "runtime",
  "mj_model",
  "mortal",
  "tools",
  "log-viewer",
  "mortal-output-viewer.html",
  "majsoul-paipu-fetcher.html",
  "start-paipu-server.ps1",
  "requirements-runtime.txt",
  "environment.yml",
  "Cargo.toml",
  "Cargo.lock"
)

foreach ($item in $include) {
  $source = Join-Path $root $item
  if (Test-Path $source) {
    Copy-Item -LiteralPath $source -Destination $stage -Recurse -Force
  }
}

$reviewer = Join-Path $root ".tools\mjai-reviewer\target\release\mjai-reviewer.exe"
if (Test-Path $reviewer) {
  $reviewerTarget = Join-Path $stage ".tools\mjai-reviewer\target\release"
  New-Item -ItemType Directory -Path $reviewerTarget -Force | Out-Null
  Copy-Item -LiteralPath $reviewer -Destination $reviewerTarget -Force
}

Get-ChildItem -Path $root -Filter "*.cmd" | ForEach-Object {
  Copy-Item -LiteralPath $_.FullName -Destination $stage -Force
}

if (Test-Path $zip) {
  Remove-Item -LiteralPath $zip -Force
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force

Write-Host "Portable package written:"
Write-Host $zip
