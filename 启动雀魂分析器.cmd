@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0runtime\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0runtime\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.conda\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0runtime\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0runtime\Scripts\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.conda\pythonw.exe"
if not exist "%PYTHON%" (
  echo.
  echo ========================================
  echo  找不到 Python 运行环境！
  echo.
  echo  请确认压缩包完整解压后，文件夹内存在
  echo  .conda 或 runtime 目录。
  echo.
  echo  如果是下载的便携包缺失 .conda 目录，
  echo  请重新下载完整包。
  echo ========================================
  echo.
  pause
  exit /b 1
)

"%PYTHON%" -B "%~dp0tools\paipu_server\update_bootstrap.py" --recover-only
if errorlevel 1 (
  echo 更新正在进行或需要恢复，请稍后重试。请勿删除 .updates 备份目录。
  pause
  exit /b 1
)

set "MISSING_DEPS="
"%PYTHON%" -B "%~dp0tools\paipu_server\update_bootstrap.py" --check-deps >nul 2>nul
if errorlevel 1 set "MISSING_DEPS=1"

if defined MISSING_DEPS (
  echo.
  echo 检测到运行依赖缺失，正在自动安装...
  echo 首次运行可能需要几分钟，请保持网络连接。
  echo.
  "%PYTHON%" -m pip install -r "%~dp0requirements-runtime.txt"
  if errorlevel 1 (
    echo.
    echo ========================================
    echo  依赖安装失败。
    echo.
    echo  请先运行 检查环境.cmd，或手动执行：
    echo  "%PYTHON%" -m pip install -r "%~dp0requirements-runtime.txt"
    echo ========================================
    echo.
    pause
    exit /b 1
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-local-server.ps1" -PythonPath "%PYTHON%"
if errorlevel 1 (
  echo 本地服务未能启动，请根据上面的提示处理后重试。
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8765/paipu-analyzer.html"

endlocal
exit /b 0

