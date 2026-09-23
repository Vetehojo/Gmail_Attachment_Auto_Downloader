@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Monitor - Test Recent 5 Messages
echo ========================================
echo.
echo This test checks Gmail API access and recent message search only.
echo No files will be downloaded.
echo.

pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Failed to access script folder.
    echo   %~dp0
    echo.
    goto :END_ERROR
)

call python gmail_monitor.py --test-recent 5
set "EXIT_CODE=%ERRORLEVEL%"

popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Gmail monitor test failed with error code %EXIT_CODE%.
    goto :END_ERROR
)

echo.
echo Test completed.
echo.
pause
exit /b 0

:END_ERROR
echo.
pause
exit /b 1
