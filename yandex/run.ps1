$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$MercadoRoot = Split-Path -Parent $PackageRoot
$PythonExe = Join-Path $PackageRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonExe)) {
    python -m venv (Join-Path $PackageRoot '.venv')
}

& $PythonExe -m pip install -r (Join-Path $PackageRoot 'requirements.txt')
& $PythonExe -m playwright install chromium
Push-Location $MercadoRoot
try {
    & $PythonExe -m yandex
} finally {
    Pop-Location
}
