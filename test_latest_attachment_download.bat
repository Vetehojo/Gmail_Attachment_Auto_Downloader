@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Downloader - One Cycle Test
echo ========================================
echo.
echo This runs one production-equivalent scan and processes up to 20 queued jobs.
echo Queue state and retry history are preserved.
echo.

pushd "%~dp0"
if errorlevel 1 goto :END_ERROR

call python gmail_monitor.py --once
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" goto :END_ERROR

echo.
echo One-cycle test completed.
pause
exit /b 0

:END_ERROR
echo.
echo [ERROR] One-cycle test failed with error code %EXIT_CODE%.
pause
exit /b 1
