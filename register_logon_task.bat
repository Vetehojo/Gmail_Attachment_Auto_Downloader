@echo off
setlocal
chcp 65001 >nul

set "APP_PY=%~dp0gmail_app.py"
set "WATCHDOG_PY=%~dp0watchdog.py"
set "INTEGRATION_PY=%~dp0windows_integration.py"
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
pause
exit /b 1
