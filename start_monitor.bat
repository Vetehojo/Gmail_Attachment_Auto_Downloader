@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Monitor - Start Process
echo ========================================
echo.

pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Failed to access script folder.
    echo   %~dp0
    echo.
    goto :END_ERROR
)

echo Starting gmail_monitor.py...
echo Press Ctrl+C to stop the monitor.
echo.

call python gmail_monitor.py
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
