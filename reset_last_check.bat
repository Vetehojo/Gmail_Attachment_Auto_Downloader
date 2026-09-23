@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Monitor - Reset Mail Cursor
echo ========================================
echo.
echo This will make the next monitor run rescan the configured lookback_days range.
echo Existing queued/success/failed jobs and credentials are preserved.
echo.
set /p "CONFIRM=Reset the mail cursor? [y/N]: "
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    echo.
    pause
    exit /b 0
)

pushd "%~dp0"
call python reset_cursor.py
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Failed to reset the mail cursor.
    pause
    exit /b 1
)

echo.
echo Next monitor run will rescan lookback_days and deduplicate against the durable queue.
echo.
pause
exit /b 0
