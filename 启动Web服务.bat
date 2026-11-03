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

title astock-watch web console
echo ============================================================
echo   astock-watch  WEB CONSOLE
echo   URL   : http://127.0.0.1:8770
echo   Stop  : close this window, or press Ctrl+C
echo ------------------------------------------------------------
echo   If the browser shows an error, wait 3 seconds and refresh.
echo ============================================================
%PY% main.py serve --port 8770 --open %*
echo.
echo [server stopped] errorlevel=%ERRORLEVEL%

echo.
pause
