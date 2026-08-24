$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$MercadoRoot = Split-Path -Parent $PackageRoot
$PythonExe = Join-Path $PackageRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw 'Python environment is missing. Run .\yandex\run.ps1 once to install dependencies.'
}

Push-Location $MercadoRoot
try {
    & $PythonExe -m yandex
} finally {
    Pop-Location
}
