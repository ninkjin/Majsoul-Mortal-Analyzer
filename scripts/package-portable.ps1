$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runtimeSource = Join-Path $root ".conda"
$python = Join-Path $runtimeSource "python.exe"
if (-not (Test-Path $python)) {
  $runtimeSource = Join-Path $root "runtime"
  $python = Join-Path $runtimeSource "python.exe"
}
if (-not (Test-Path $python)) {
  throw "portable Python was not found. Prepare .conda\python.exe first."
}
& $python -c "import torch, mahjong, tensoul, numpy; from libriichi.mjai import Bot; print('portable source deps ok')"
if ($LASTEXITCODE -ne 0) {
  throw "portable source dependency check failed."
}

$dist = Join-Path $root "dist"
$stage = Join-Path $dist "mortal-paipu-analyzer"
$zip = Join-Path $dist "mortal-paipu-analyzer-portable.zip"

if (Test-Path $stage) {
  Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Path $stage | Out-Null

$include = @(
  "mj_model",
  "mortal",
  "tools",
  "scripts",
  "log-viewer",
  "README.md",
  "LICENSE",
  "mortal-output-viewer.html",
  "majsoul-paipu-fetcher.html",
  "paipu-service.example.json",
  "start-paipu-server.ps1",
  "requirements-runtime.txt",
  "environment.yml",
  "Cargo.toml",
  "Cargo.lock"
)

Copy-Item -LiteralPath $runtimeSource -Destination (Join-Path $stage "runtime") -Recurse -Force

$stagedRuntime = Join-Path $stage "runtime"
$stagedRuntimeRoot = [IO.Path]::GetFullPath($stagedRuntime).TrimEnd('\') + '\'
function Remove-StagedBuildTool([string]$candidate) {
  $target = [IO.Path]::GetFullPath($candidate)
  if (-not $target.StartsWith($stagedRuntimeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing to remove a path outside the staged runtime: $target"
  }
  if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
  }
}

Remove-StagedBuildTool (Join-Path $stagedRuntime "Scripts\maturin.exe")
$stagedSitePackages = Join-Path $stagedRuntime "Lib\site-packages"
Remove-StagedBuildTool (Join-Path $stagedSitePackages "maturin")
Get-ChildItem -LiteralPath $stagedSitePackages -Directory -Filter "maturin-*.dist-info" -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-StagedBuildTool $_.FullName }

$stagedPython = Join-Path $stagedRuntime "python.exe"
if (-not (Test-Path $stagedPython)) {
  $stagedPython = Join-Path $stagedRuntime "Scripts\python.exe"
}
& $stagedPython -B -c "import importlib.util, torch, mahjong, tensoul, numpy; from libriichi.mjai import Bot; assert importlib.util.find_spec('maturin') is None; print('staged runtime deps ok')"
if ($LASTEXITCODE -ne 0) {
  throw "staged runtime dependency check failed."
}

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
$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($tar) {
  & $tar.Source -a -c --options zip:hdrcharset=UTF-8 -f $zip -C $stage .
  if ($LASTEXITCODE -ne 0) {
    throw "tar.exe failed to create the portable zip."
  }
} else {
  Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
}

Write-Host "Portable package written:"
Write-Host $zip
