@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_local_workbench.ps1"
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上面的错误信息。
    pause
)
endlocal
