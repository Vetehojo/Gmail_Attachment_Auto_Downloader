@echo off
setlocal
chcp 65001 >nul

echo ========================================
echo Gmail Attachment Downloader - Unit Tests
echo ========================================
echo.

pushd "%~dp0.."
if errorlevel 1 goto :END_ERROR

call python -m unittest discover -s tests -v
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" goto :END_ERROR

echo.
echo Unit tests completed.
pause
exit /b 0

:END_ERROR
echo.
echo [ERROR] Unit tests failed with error code %EXIT_CODE%.
pause
exit /b 1
