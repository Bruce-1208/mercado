@echo off
chcp 65001 >nul
cd /d "%~dp0"

py -3 -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Run:
    echo py -3 -m pip install pyinstaller
    pause
    exit /b 1
)

echo Building MercadoLocalAgent.exe ...
py -3 -m PyInstaller --noconfirm --clean MercadoLocalAgent.spec

if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Build succeeded. The Zeshun console will include this file in Agent downloads:
echo %~dp0dist\MercadoLocalAgent.exe
pause
