param(
  [string]$RuntimeSource = "",
  [string]$OutputDirectory = "",
  [string]$ReviewerPath = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runtimeWasSpecified = [bool]$RuntimeSource
$runtimeSource = if ($RuntimeSource) { [IO.Path]::GetFullPath($RuntimeSource) } else { Join-Path $root ".conda" }
$python = Join-Path $runtimeSource "python.exe"
if (-not (Test-Path $python)) {
  if ($runtimeWasSpecified) { throw "The selected runtime must contain standalone python.exe (not a virtualenv)." }
  $runtimeSource = Join-Path $root "runtime"
  $python = Join-Path $runtimeSource "python.exe"
}
if (-not (Test-Path $python)) {
  throw "portable Python was not found. Prepare .conda\python.exe first."
}
if (Test-Path -LiteralPath (Join-Path $runtimeSource "pyvenv.cfg")) {
  throw "A virtualenv is not a standalone portable runtime. Use the .conda runtime source."
}
& $python -E -B -c "import torch, mahjong, tensoul, numpy; from libriichi.mjai import Bot; print('portable source core deps ok')"
if ($LASTEXITCODE -ne 0) {
  throw "portable source dependency check failed."
}

$dist = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $root "dist" }
$stage = Join-Path $dist "mortal-paipu-analyzer"
$zip = Join-Path $dist "mortal-paipu-analyzer-portable.zip"

if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $zip)) {
  throw "Output already exists. Choose a new -OutputDirectory; previous packages are preserved."
}
if ([IO.Path]::GetFullPath($dist).StartsWith([IO.Path]::GetFullPath($runtimeSource).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
  throw "OutputDirectory must not be inside the runtime source."
}
New-Item -ItemType Directory -Path $stage | Out-Null

$include = @(
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
  "app-version.json",
  "update-trust.json",
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
Remove-StagedBuildTool (Join-Path $stagedRuntime "conda-meta")
$stagedSitePackages = Join-Path $stagedRuntime "Lib\site-packages"
Remove-StagedBuildTool (Join-Path $stagedSitePackages "maturin")
Get-ChildItem -LiteralPath $stagedSitePackages -Directory -Filter "maturin-*.dist-info" -ErrorAction SilentlyContinue |
  ForEach-Object { Remove-StagedBuildTool $_.FullName }

$stagedPython = Join-Path $stagedRuntime "python.exe"
if (-not (Test-Path $stagedPython)) {
  $stagedPython = Join-Path $stagedRuntime "Scripts\python.exe"
}
# Install release dependencies only into the copied runtime, never the developer environment.
& $stagedPython -E -B -m pip install --disable-pip-version-check -r (Join-Path $root "requirements-runtime.txt")
if ($LASTEXITCODE -ne 0) {
  throw "Installing dependencies into the staged runtime failed."
}
& $stagedPython -E -B -c "import importlib.util, pathlib, sys, torch, mahjong, tensoul, numpy, UnityPy, cryptography; from libriichi.mjai import Bot; assert importlib.util.find_spec('maturin') is None; expected=pathlib.Path(sys.argv[1]).resolve(); assert pathlib.Path(sys.prefix).resolve()==expected; assert expected in pathlib.Path(torch.__file__).resolve().parents; print('staged runtime deps and relocation ok')" $stagedRuntime
if ($LASTEXITCODE -ne 0) {
  throw "staged runtime dependency check failed."
}

foreach ($item in $include) {
  $source = Join-Path $root $item
  if (Test-Path $source) {
    Copy-Item -LiteralPath $source -Destination $stage -Recurse -Force
  }
}

$modelSource = Join-Path $root "mj_model\mortal.pth"
if (-not (Test-Path -LiteralPath $modelSource -PathType Leaf)) { throw "Bundled mortal.pth is missing." }
New-Item -ItemType Directory -Path (Join-Path $stage "mj_model") | Out-Null
Copy-Item -LiteralPath $modelSource -Destination (Join-Path $stage "mj_model\mortal.pth")

$reviewer = if ($ReviewerPath) { [IO.Path]::GetFullPath($ReviewerPath) } else { Join-Path $root ".tools\mjai-reviewer\target\release\mjai-reviewer.exe" }
if (-not (Test-Path -LiteralPath $reviewer -PathType Leaf)) { throw "The bundled mjai-reviewer.exe is missing." }
if (Test-Path $reviewer) {
  $reviewerTarget = Join-Path $stage ".tools\mjai-reviewer\target\release"
  New-Item -ItemType Directory -Path $reviewerTarget -Force | Out-Null
  Copy-Item -LiteralPath $reviewer -Destination $reviewerTarget -Force
}

foreach ($launcher in @("启动雀魂分析器.cmd", "检查环境.cmd")) {
  Copy-Item -LiteralPath (Join-Path $root $launcher) -Destination $stage
}

& $stagedPython -E -B (Join-Path $stage "tools\paipu_server\update_bootstrap.py") --self-test
if ($LASTEXITCODE -ne 0) {
  throw "The staged application self-test failed; no package will be created."
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
