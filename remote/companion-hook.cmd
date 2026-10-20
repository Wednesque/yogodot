@echo off
setlocal
if not exist "%~dp0companion-hook.py" exit /b 0
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0companion-hook.py" %*
  exit /b %errorlevel%
)
python "%~dp0companion-hook.py" %*
exit /b %errorlevel%
