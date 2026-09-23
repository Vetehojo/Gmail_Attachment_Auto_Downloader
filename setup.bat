@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "SDIR=%~dp0"
if "%SDIR:~-1%"=="\" set "SDIR=%SDIR:~0,-1%"
set "APP_PY=%~dp0app\gmail_app.py"
set "MONITOR_PY=%~dp0app\gmail_monitor.py"
set "CONFIG_INI=%~dp0config.ini"

echo.
echo ============================================
echo  Gmail Auto Downloader Setup
echo ============================================
echo.

echo [1/3] Checking Python 3.14...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.14 and enable Add Python to PATH.
    goto :END_ERROR
)
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,14) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.14 is required. Older versions and newer versions such as 3.15 are not supported.
    echo [ERROR] If multiple Python versions are installed, make sure 3.14 is first on PATH.
    python --version
    goto :END_ERROR
)
for /f "usebackq delims=" %%p in (`python -X utf8 -c "import os,sys; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYTHONW=%%p"
if not defined PYTHONW goto :PYTHONW_ERROR
if not exist "%PYTHONW%" goto :PYTHONW_ERROR

echo [2/3] Installing Python packages...
rem The exact versions tested with this app (requirements.lock).
pushd "%SDIR%"
python -m pip install -r requirements.lock
set "PIP_RESULT=%ERRORLEVEL%"
popd
if not "%PIP_RESULT%"=="0" goto :END_ERROR

echo [3/3] Opening setup window...
rem config.ini's timestamp tells whether settings were saved in this run.
rem --setup also exits 0 when the tray app is already running.
call :CONFIG_STAMP
set "STAMP_BEFORE=%STAMP%"
pushd "%SDIR%"
python "%APP_PY%" --setup
set "GUI_RESULT=%ERRORLEVEL%"
popd
if not "%GUI_RESULT%"=="0" goto :END_ERROR
call :CONFIG_STAMP
if "%STAMP%"=="%STAMP_BEFORE%" (
    echo.
    echo Settings were not saved in this run.
    echo If the tray app is already running, change settings from its menu instead.
    goto :END_OK
)

echo.
echo Settings saved. Automatic fetching has not started yet.
echo.
echo A trial download saves only the newest 3 attachment mails of each account,
echo so you can check the save folder and file names first.
set "TRIAL_ANSWER=Y"
set /p "TRIAL_ANSWER=Run the trial download now? [Y/n]: "
rem Drop typed quotes so no answer can break the comparisons below.
set "TRIAL_ANSWER=%TRIAL_ANSWER:"=%"
if /i "%TRIAL_ANSWER%"=="n" goto :SKIP_TRIAL
if /i "%TRIAL_ANSWER%"=="no" goto :SKIP_TRIAL

echo.
pushd "%SDIR%"
python "%MONITOR_PY%" --trial 3
set "TRIAL_RESULT=%ERRORLEVEL%"
popd
if not "%TRIAL_RESULT%"=="0" (
    echo.
    echo [WARNING] The trial download did not finish cleanly. Exit code: %TRIAL_RESULT%
    echo Your settings are saved. Check the messages above, then run
    echo trial_download.bat to try again.
)
goto :NEXT_STEPS

:SKIP_TRIAL
echo Trial download skipped.

:NEXT_STEPS
echo.
echo Next steps:
echo   1. Check the files in the save folder.
echo   2. Run register_logon_task.bat to register and start automatic fetching.
echo To run the trial again, use trial_download.bat.
echo It never saves an attachment that was already saved.
goto :END_OK

:CONFIG_STAMP
set "STAMP=missing"
for /f "usebackq delims=" %%s in (`python -c "import os; p = os.environ['CONFIG_INI']; print(os.stat(p).st_mtime_ns if os.path.isfile(p) else 'missing')"`) do set "STAMP=%%s"
exit /b 0

:PYTHONW_ERROR
echo.
echo [ERROR] pythonw.exe was not found next to the python.exe on PATH.
echo [ERROR] PYTHONW value: "%PYTHONW%"
goto :END_ERROR

:END_ERROR
echo.
echo [ERROR] Setup failed.
pause
exit /b 1

:END_OK
echo.
pause
exit /b 0
