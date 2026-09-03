@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Building MercadoWorkbench.exe ...
python -m PyInstaller --noconfirm --clean MercadoWorkbench.spec

if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

copy /Y "workbench-server.example.json" "dist\MercadoWorkbench\workbench-server.example.json" >nul
copy /Y "workbench-client.example.json" "dist\MercadoWorkbench\workbench-client.example.json" >nul

echo.
echo Build succeeded.
echo Output: %~dp0dist\MercadoWorkbench\MercadoWorkbench.exe
pause
