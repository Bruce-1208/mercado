@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_COMMAND=py -3"
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 set "PYTHON_COMMAND=python"

%PYTHON_COMMAND% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Run:
    echo %PYTHON_COMMAND% -m pip install pyinstaller
    pause
    exit /b 1
)

echo Building MercadoLocalAgent.exe ...
%PYTHON_COMMAND% -m PyInstaller --noconfirm --clean MercadoLocalAgent.spec

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
