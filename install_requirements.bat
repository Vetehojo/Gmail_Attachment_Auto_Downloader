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

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.12 or later is required.
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
