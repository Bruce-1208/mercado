@echo off
setlocal
cd /d "%~dp0.."

echo Starting Yandex assistant...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
    echo.
    echo Yandex assistant failed to start. See .data\server-error.log
    pause
    exit /b 1
)
exit /b 0
