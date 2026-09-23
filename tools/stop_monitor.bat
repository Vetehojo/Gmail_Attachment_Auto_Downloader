@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Monitor - Stop Process Tree
echo ========================================
echo.

set "MONITOR_PY=%~dp0..\app\gmail_monitor.py"
set "INTEGRATION_PY=%~dp0..\app\windows_integration.py"
if not exist "%MONITOR_PY%" goto :END_ERROR
if not exist "%INTEGRATION_PY%" goto :END_ERROR

python "%INTEGRATION_PY%" stop-monitor --script "%MONITOR_PY%"
if errorlevel 1 goto :END_ERROR

echo.
echo Gmail monitor process tree stopped.
pause
exit /b 0

:END_ERROR
echo.
echo [ERROR] Failed to stop Gmail monitor process tree.
pause
exit /b 1
