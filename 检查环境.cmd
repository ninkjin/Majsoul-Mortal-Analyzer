@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.conda\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0runtime\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.conda\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0runtime\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo 正在检查运行环境...
echo.
"%PYTHON%" -c "import torch; print('  torch: ' + torch.__version__)" 2>nul
if errorlevel 1 (
  echo   torch: 未安装
  set MISSING=1
)
"%PYTHON%" -c "import mahjong; print('  mahjong: OK')" 2>nul
if errorlevel 1 (
  echo   mahjong: 未安装
  set MISSING=1
)
"%PYTHON%" -c "import tensoul; print('  tensoul: OK')" 2>nul
if errorlevel 1 (
  echo   tensoul: 未安装
  set MISSING=1
)
"%PYTHON%" -c "import numpy; print('  numpy: ' + numpy.__version__)" 2>nul
if errorlevel 1 (
  echo   numpy: 未安装
  set MISSING=1
)

if defined MISSING (
  echo.
  echo 检测到缺少依赖，正在自动安装，请稍候...
  echo.
  "%PYTHON%" -m pip install -r "%~dp0requirements-runtime.txt"
  if errorlevel 1 (
    echo.
    echo ========================================
    echo  依赖安装失败，请手动运行以下命令：
    echo  "%PYTHON%" -m pip install -r requirements-runtime.txt
    echo ========================================
    pause
    exit /b 1
  )
  echo.
  echo 依赖安装完成！
) else (
  echo.
  echo 环境检查通过，所有依赖已就绪。
)

echo.
pause
endlocal

