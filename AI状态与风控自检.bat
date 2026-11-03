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

echo === AI doctor: gateway / json / tokens / protocol / rails ===
%PY% main.py ai --doctor
echo.
echo === risk rails selftest ===
%PY% selftest_ai.py
echo.
echo === plan-order selftest ===
%PY% selftest_plans.py
echo.
echo === account status ===
%PY% main.py ai --status

echo.
pause
