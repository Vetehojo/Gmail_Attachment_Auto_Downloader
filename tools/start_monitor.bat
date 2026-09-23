@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Monitor - Start Process
echo ========================================
echo.

rem Same absolute script path and working folder as the tray/watchdog use,
rem so this process is recognized as the owned monitor.
set "MONITOR_PY=%~dp0..\app\gmail_monitor.py"

pushd "%~dp0.."
if errorlevel 1 (
    echo [ERROR] Failed to access install folder.
    echo   "%~dp0.."
    echo.
    goto :END_ERROR
)

echo Starting gmail_monitor.py...
echo Press Ctrl+C to stop the monitor.
echo.

python "%MONITOR_PY%"
set "EXIT_CODE=%ERRORLEVEL%"

popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Gmail monitor stopped with error code %EXIT_CODE%.
    goto :END_ERROR
)

echo.
echo Gmail monitor stopped.
echo.
pause
exit /b 0

:END_ERROR
echo.
pause
exit /b 1
