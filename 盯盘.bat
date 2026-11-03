@echo off
setlocal
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo [ERROR] Python 3.8+ not found in PATH.
  echo Please install Python, then run: python main.py serve
  pause
  exit /b 1
)

title astock-watch monitor
echo === Realtime monitor (Ctrl+C to stop) ===
%PY% main.py monitor %*
echo.
echo [monitor stopped]

echo.
pause
