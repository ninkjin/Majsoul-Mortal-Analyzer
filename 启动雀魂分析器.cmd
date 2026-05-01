@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0runtime\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0runtime\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.conda\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.conda\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=python"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $port=8765; $root='%~dp0'; $server=Join-Path $root 'tools\paipu_server\server.py'; $watched=@($server,(Join-Path $root 'tools\paipu_server\majgg_fetcher.py'),(Join-Path $root 'tools\paipu_server\majgg_settlement.py'),(Join-Path $root 'tools\paipu_server\mortal_runner.py'),(Join-Path $root 'tools\paipu_server\browser_probe.py')); $conn=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen | Select-Object -First 1; if (-not $conn) { exit 1 }; $proc=Get-CimInstance Win32_Process -Filter \"ProcessId = $($conn.OwningProcess)\"; if ($proc -and $proc.CommandLine -like '*tools\paipu_server\server.py*') { $started=[Management.ManagementDateTimeConverter]::ToDateTime($proc.CreationDate); $changed=($watched | Where-Object { Test-Path $_ } | ForEach-Object { (Get-Item $_).LastWriteTime } | Sort-Object -Descending | Select-Object -First 1); if ($changed -gt $started) { Stop-Process -Id $conn.OwningProcess -Force; Start-Sleep -Milliseconds 500; exit 1 } }; try { Invoke-WebRequest -Uri 'http://127.0.0.1:8765/paipu-analyzer.html' -UseBasicParsing -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PYTHON%' -ArgumentList @('-B','%~dp0tools\paipu_server\server.py','--host','127.0.0.1','--port','8765') -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0tmp-paipu-server.out' -RedirectStandardError '%~dp0tmp-paipu-server.err'"
  timeout /t 2 /nobreak >nul
)

start "" "http://127.0.0.1:8765/paipu-analyzer.html"

endlocal
