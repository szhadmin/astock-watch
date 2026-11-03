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

title astock-watch AI trader
echo === AI trader loop (Ctrl+C to stop) ===
%PY% main.py ai --loop --interval-min 30 %*
echo.
echo [ai trader stopped]

echo.
pause
