@echo off
setlocal
chcp 65001 >nul

set "APP_PY=%~dp0app\gmail_app.py"
set "WATCHDOG_PY=%~dp0app\watchdog.py"
set "INTEGRATION_PY=%~dp0app\windows_integration.py"
set "SCHTASKS=%SystemRoot%\System32\schtasks.exe"
set "TASK_USER=%USERDOMAIN%\%USERNAME%"

for /f "usebackq delims=" %%p in (`python -X utf8 -c "import os,sys; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYTHONW=%%p"

if not defined PYTHONW goto :PYTHONW_ERROR
if not exist "%PYTHONW%" goto :PYTHONW_ERROR
if not exist "%APP_PY%" goto :END_ERROR
if not exist "%WATCHDOG_PY%" goto :END_ERROR
if not exist "%INTEGRATION_PY%" goto :END_ERROR

echo ========================================
echo Gmail Auto Downloader - Register Tasks
echo ========================================
echo Pythonw: "%PYTHONW%"
echo.

python "%INTEGRATION_PY%" register-tasks ^
    --pythonw "%PYTHONW%" ^
    --app-script "%APP_PY%" ^
    --watchdog-script "%WATCHDOG_PY%" ^
    --user "%TASK_USER%"
if errorlevel 1 goto :END_ERROR

echo.
echo Registered:
echo   Gmail Auto Downloader Monitor  - tray app one minute after logon
echo   Gmail Auto Downloader Watchdog - monitor recovery every five minutes
echo.

rem Start the tray now by running the task just registered. The task runs
rem non-elevated (/RL LIMITED) in this user's session with the exact registered
rem command line, even when this window runs as administrator.
"%SCHTASKS%" /Run /TN "Gmail Auto Downloader Monitor" >nul
if errorlevel 1 (
    echo The tray app could not be started now. It starts at the next logon.
    goto :END_OK
)
echo Started the tray app. It fetches automatically unless it is paused
echo or the settings are not complete yet.
echo If the tray app was already running, that instance keeps running.

:END_OK
echo.
pause
exit /b 0

:PYTHONW_ERROR
echo.
echo [ERROR] pythonw.exe was not found next to the python.exe on PATH.
echo [ERROR] PYTHONW value: "%PYTHONW%"
goto :END_ERROR

:END_ERROR
echo.
echo [ERROR] Failed to register scheduled tasks.
echo Existing tasks were left untouched unless their executable and script matched this install.
echo Any partial update was rolled back and verified where possible.
echo When upgrading from the old layout, exit the tray app and delete the old
echo "Gmail Auto Downloader Monitor" and "Gmail Auto Downloader Watchdog" tasks
echo in Task Scheduler first, then run this file again.
pause
exit /b 1
