@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Monitor - Install Requirements
echo ========================================
echo.

python --version
if errorlevel 1 (
    echo [ERROR] Python was not found.
    pause
    exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,14) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.14 is required. Older versions and newer versions such as 3.15 are not supported.
    echo [ERROR] If multiple Python versions are installed, make sure 3.14 is first on PATH.
    python --version
    pause
    exit /b 1
)

pushd "%~dp0"
python -m pip install --upgrade -r requirements.txt
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Package installation failed.
    pause
    exit /b 1
)

echo.
echo Requirements installed successfully.
pause
exit /b 0
