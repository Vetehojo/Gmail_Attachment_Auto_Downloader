@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "SDIR=%~dp0"
if "%SDIR:~-1%"=="\" set "SDIR=%SDIR:~0,-1%"

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
pushd "%SDIR%"
python -m pip install --upgrade -r requirements.txt
set "PIP_RESULT=%ERRORLEVEL%"
popd
if not "%PIP_RESULT%"=="0" goto :END_ERROR

echo [3/3] Opening setup window...
pushd "%SDIR%"
python gmail_app.py --setup
set "GUI_RESULT=%ERRORLEVEL%"
popd
if not "%GUI_RESULT%"=="0" goto :END_ERROR

if not exist "%SDIR%\config.ini" (
    echo.
    echo Setup was cancelled before configuration was saved.
    goto :END_OK
)

start "" "%PYTHONW%" "%SDIR%\gmail_app.py"

echo.
echo Setup complete. Tray app started.
echo Register automatic startup with register_logon_task.bat when ready.
echo.
goto :END_OK

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
