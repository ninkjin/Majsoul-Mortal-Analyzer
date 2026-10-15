param(
  [Parameter(Mandatory=$true)][string]$PythonPath,
  [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$entry = Join-Path $root "tools\paipu_server\update_bootstrap.py"
$baseUrl = "http://127.0.0.1:$Port"
$watched = @(
  "app-version.json", "tools\paipu_server\server.py", "tools\paipu_server\update_bootstrap.py",
  "tools\paipu_server\app_updater.py", "tools\paipu_server\update_worker.py",
  "tools\paipu_server\remote_service_fetcher.py", "tools\paipu_server\mortal_runner.py",
  "tools\paipu_server\browser_probe.py"
) | ForEach-Object { Join-Path $root $_ }

function Get-AnalyzerPage {
  try {
    $page = Invoke-WebRequest -Uri "$baseUrl/paipu-analyzer.html" -UseBasicParsing -TimeoutSec 2
    return ($page.StatusCode -eq 200 -and $page.Content.Contains('id="fetch-mode"'))
  } catch { return $false }
}

$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
  $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
  $entryMatch = [regex]::Match($process.CommandLine, '(?i)(?:"(?<path>[^"]+\\tools\\paipu_server\\(?:server|update_bootstrap)\.py)"|(?<path>[A-Z]:\\[^"\r\n]*?\\tools\\paipu_server\\(?:server|update_bootstrap)\.py)(?:\s|$))')
  if (-not $entryMatch.Success -or -not (Get-AnalyzerPage)) {
    throw "Port $Port is occupied by an unrecognized process. Nothing was stopped."
  }
  $existingEntry = [IO.Path]::GetFullPath($entryMatch.Groups['path'].Value)
  $existingRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $existingEntry))
  $sameRoot = $existingRoot.Equals($root, [StringComparison]::OrdinalIgnoreCase)
  $newest = ($watched | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object { (Get-Item -LiteralPath $_).LastWriteTime } | Sort-Object -Descending | Select-Object -First 1)
  $needsRestart = -not $sameRoot -or $newest -gt ([datetime]$process.CreationDate)
  if (-not $needsRestart) { exit 0 }
  $state = $null
  try { $state = Invoke-RestMethod -Uri "$baseUrl/api/update/status" -TimeoutSec 2 } catch {}
  if ($state -and ($state.busy -or $state.analysis_busy)) {
    throw "The analyzer is busy. Finish analysis or installation before restarting."
  }
  if (-not $state -or -not ($state.PSObject.Properties.Name -contains "analysis_busy")) {
    Write-Host "An older analyzer is running. Confirm that all analysis tasks have finished."
    if ((Read-Host "Type Y to stop the old analyzer and start this version") -ne "Y") {
      throw "Restart cancelled. The existing analyzer was not stopped."
    }
  }
  # Revalidate the exact observed process before stopping it, never stop a port-only PID.
  $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)"
  if (-not $current -or $current.CommandLine -ne $process.CommandLine -or $current.CreationDate -ne $process.CreationDate) {
    throw "The existing process changed. Please retry; nothing was stopped."
  }
  Stop-Process -Id $process.ProcessId -Force
  Wait-Process -Id $process.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
}

# Quote the script argument explicitly: Start-Process joins ArgumentList on Windows.
$arguments = @('-X','utf8','-B','-u',('"' + $entry + '"'),'--host','127.0.0.1','--port',"$Port")
$child = Start-Process -FilePath $PythonPath -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'tmp-paipu-server.out') -RedirectStandardError (Join-Path $root 'tmp-paipu-server.err') -PassThru
for ($attempt = 0; $attempt -lt 60; $attempt++) {
  if (Get-AnalyzerPage) { exit 0 }
  if ($child.HasExited) { throw "The analyzer exited. Check tmp-paipu-server.err in the installation folder." }
  Start-Sleep -Milliseconds 500
}
throw "The analyzer has not started yet. Check tmp-paipu-server.err before retrying."
