@echo off
setlocal EnableExtensions
title Restart Mercado Workbench

rem This launcher may be copied anywhere on this computer.
rem Project lookup order:
rem   1. The first command-line argument
rem   2. MERCADO_PROJECT_ROOT
rem   3. The launcher's own directory
rem   4. The checkout path recorded when this launcher was created

set "PROJECT_ROOT="

if not "%~1"=="" set "PROJECT_ROOT=%~f1"
if not defined PROJECT_ROOT if defined MERCADO_PROJECT_ROOT set "PROJECT_ROOT=%MERCADO_PROJECT_ROOT%"
if not defined PROJECT_ROOT if exist "%~dp0start_local_workbench.ps1" set "PROJECT_ROOT=%~dp0"
if not defined PROJECT_ROOT set "PROJECT_ROOT=C:\Users\1\mercado\mercado"

for %%R in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fR"
set "START_SCRIPT=%PROJECT_ROOT%\start_local_workbench.ps1"

echo.
echo ========================================
echo   Restart Mercado Workbench
echo ========================================
echo Project: %PROJECT_ROOT%
echo.

if not exist "%START_SCRIPT%" (
    echo [FAILED] Startup script not found:
    echo          %START_SCRIPT%
    echo.
    echo Pass the project folder as the first argument:
    echo   "%~nx0" "D:\path\to\mercado"
    echo.
    echo Or set MERCADO_PROJECT_ROOT to the project folder.
    exit /b 2
)

rem The standard startup script safely stops this project's listeners first,
rem then checks MySQL, starts the workbench, and waits for port 5000.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%START_SCRIPT%"
set "RESULT=%ERRORLEVEL%"

if not "%RESULT%"=="0" (
    echo.
    echo [FAILED] Workbench restart failed with exit code %RESULT%.
    exit /b %RESULT%
)

echo.
echo [SUCCESS] Mercado Workbench has been restarted.
exit /b 0
