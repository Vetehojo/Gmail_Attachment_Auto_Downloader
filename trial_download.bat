@echo off
setlocal
chcp 65001 >nul

set "MONITOR_PY=%~dp0app\gmail_monitor.py"
set "EXIT_CODE=1"

echo ========================================
echo Gmail Auto Downloader - Trial Download
echo ========================================
echo Saves only the newest 3 attachment mails of each account.
echo Exit the tray app first if it is running.
echo.

if not exist "%MONITOR_PY%" (
    echo [ERROR] app\gmail_monitor.py was not found next to this file.
    goto :END
)

pushd "%~dp0."
python "%MONITOR_PY%" --trial 3
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] The trial download finished with exit code %EXIT_CODE%.
)

:END
echo.
pause
exit /b %EXIT_CODE%
