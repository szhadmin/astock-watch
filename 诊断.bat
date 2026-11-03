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

echo ============================================================
echo   astock-watch  DIAGNOSTICS
echo ============================================================
echo [1] working dir:
cd
echo.
echo [2] python:
%PY% -c "import sys;print(sys.version);print(sys.executable)"
echo.
echo [3] port 8770 / 8771 in use?
netstat -ano | findstr ":8770" | findstr LISTENING
netstat -ano | findstr ":8771" | findstr LISTENING
echo   (empty above = free)
echo.
echo [4] import check:
%PY% -c "import sys;sys.path.insert(0,'.');import astock.server,astock.ai_agent,astock.feed;print('modules OK')"
echo.
echo [5] quick data check:
%PY% main.py test

echo.
pause
