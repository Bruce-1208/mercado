@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Restart Mercado Workbench

set "PORT=5000"
set "PROJECT_ROOT="
set "PYTHON_CMD="

echo.
echo ========================================
echo   Restart Mercado Workbench
echo ========================================
echo.

rem Try the batch file folder first.
if exist "%~dp0bit\bit_interface.py" set "PROJECT_ROOT=%~dp0"

rem Search common project folders when this batch file has been moved.
if not defined PROJECT_ROOT call :search_root "%USERPROFILE%\PycharmProjects"
if not defined PROJECT_ROOT call :search_root "%USERPROFILE%\Projects"
if not defined PROJECT_ROOT call :search_root "%USERPROFILE%\source\repos"
if not defined PROJECT_ROOT call :search_root "%USERPROFILE%\Documents"

if not defined PROJECT_ROOT (
    echo Project folder was not found automatically.
    set /p "PROJECT_ROOT=Enter the Mercado project folder: "
)

if not exist "%PROJECT_ROOT%\bit\bit_interface.py" (
    echo.
    echo [FAILED] The folder does not contain bit\bit_interface.py.
    goto failed
)

for %%R in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fR"
echo Project: %PROJECT_ROOT%

for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYTHON_CMD set "PYTHON_CMD=%%P"
if not defined PYTHON_CMD (
    echo.
    echo [FAILED] Python was not found in PATH.
    goto failed
)

echo Stopping the service on port %PORT% ...
set "FOUND_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    set "FOUND_PID=%%P"
    taskkill /PID %%P /F >nul 2>&1
)

if defined FOUND_PID (
    echo Stopped PID !FOUND_PID!.
    ping 127.0.0.1 -n 3 >nul
) else (
    echo No running service was found. Starting a new one.
)

echo Starting Mercado Workbench ...
pushd "%PROJECT_ROOT%"
start "Mercado Workbench" /min "%PYTHON_CMD%" -m bit.bit_interface --role server
popd

set "READY="
for /L %%I in (1,1,30) do (
    if not defined READY (
        for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "READY=%%P"
        if not defined READY ping 127.0.0.1 -n 2 >nul
    )
)

if not defined READY (
    echo.
    echo [FAILED] The service did not listen on port %PORT% within 30 seconds.
    goto failed
)

echo.
echo [SUCCESS] Mercado Workbench is running on port %PORT% with PID !READY!.
echo.
pause
exit /b 0

:search_root
set "SEARCH_BASE=%~1"
if not exist "%SEARCH_BASE%" exit /b 0
for /d /r "%SEARCH_BASE%" %%D in (bit) do (
    if not defined PROJECT_ROOT if exist "%%D\bit_interface.py" (
        for %%R in ("%%D\..") do set "PROJECT_ROOT=%%~fR"
    )
)
exit /b 0

:failed
echo.
pause
exit /b 1
