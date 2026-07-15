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

echo.
echo Build succeeded.
echo Output: %~dp0dist\MercadoWorkbench\MercadoWorkbench.exe
pause
