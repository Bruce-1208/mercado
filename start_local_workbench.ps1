$ErrorActionPreference = 'Stop'

# Start the local Mercado / Zeshun workbench with local MySQL only.

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogRoot = Join-Path $ProjectRoot 'runtime_logs\local_workbench'
$MysqlRoot = 'C:\Users\1\AppData\Local\Programs\MySQL\MySQL Server 8.0.42'
$MysqlExe = Join-Path $MysqlRoot 'bin\mysql.exe'
$MysqldExe = Join-Path $MysqlRoot 'bin\mysqld.exe'
$MysqlIni = Join-Path $MysqlRoot 'my.ini'

$DbHost = '127.0.0.1'
$DbPort = '3306'
$DbUser = 'mercado'
$DbPassword = 'mercado'
$DbName = 'mercado'

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

function Get-PythonPath {
    $fallback = 'C:\Users\1\AppData\Local\Programs\Python\Python312\python.exe'
    if (Test-Path -LiteralPath $fallback) {
        return $fallback
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        return $python.Source
    }

    throw 'Python 3.12 was not found.'
}

function Test-LocalMysql {
    if (-not (Test-Path -LiteralPath $MysqlExe)) {
        throw "MySQL client was not found: $MysqlExe"
    }

    $mysqlArgs = @(
        '--protocol=TCP',
        "--host=$DbHost",
        "--port=$DbPort",
        "--user=$DbUser",
        "--password=$DbPassword",
        "--database=$DbName",
        '--batch',
        '--skip-column-names',
        '--execute=SELECT 1;'
    )
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $result = & $MysqlExe @mysqlArgs 2>&1
        $mysqlExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    $hasOne = @($result | ForEach-Object { $_.ToString().Trim() } | Where-Object { $_ -eq '1' }).Count -gt 0
    return ($mysqlExitCode -eq 0 -and $hasOne)
}

function Wait-LocalMysql {
    param([int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-LocalMysql) {
            return
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Local MySQL was not ready within $TimeoutSeconds seconds."
}

function Stop-ProjectPort {
    param([int]$Port)

    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in ($connections | Sort-Object OwningProcess -Unique)) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction SilentlyContinue
        $commandLine = if ($owner) { [string]$owner.CommandLine } else { '' }

        # Only stop an existing process belonging to this project.
        if ($commandLine -match 'mercado|bit\.bit_interface|yandex') {
            Write-Host "Stopping old project process $($connection.OwningProcess) on port $Port..."
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
        } else {
            throw "Port $Port is already used by another process: PID $($connection.OwningProcess)."
        }
    }
}

function Wait-ListeningPort {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 60,
        [switch]$Optional
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            return $true
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    if ($Optional) {
        Write-Warning "Optional service port $Port did not open. Check $LogRoot."
        return $false
    }

    throw "Required service port $Port did not open within $TimeoutSeconds seconds. Check $LogRoot."
}

Write-Host '=== Starting local workbench ==='
Write-Host "Project: $ProjectRoot"

# These variables are inherited only by the services started by this script.
$env:BIT_RUNTIME_ROLE = 'server'
$env:BIT_DB_MODE = 'mysql'
$env:BIT_INTERFACE_DB_MODE = 'direct'
$env:BIT_DB_DIRECT_DISABLED = '0'
$env:MYSQL_HOST = $DbHost
$env:MYSQL_PORT = $DbPort
$env:MYSQL_USER = $DbUser
$env:MYSQL_PASSWORD = $DbPassword
$env:MYSQL_DATABASE = $DbName
$env:MYSQL_POOL_MAX_CONNECTIONS = '24'
$env:PYTHONUNBUFFERED = '1'

$python = Get-PythonPath
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $ProjectRoot
} else {
    "$ProjectRoot;$($env:PYTHONPATH)"
}

if (-not (Test-LocalMysql)) {
    if (-not (Test-Path -LiteralPath $MysqldExe)) {
        throw "Local MySQL is not running and mysqld.exe was not found: $MysqldExe"
    }
    if (-not (Test-Path -LiteralPath $MysqlIni)) {
        throw "MySQL config was not found: $MysqlIni"
    }

    Write-Host 'Local MySQL is not ready. Starting it...'
    Start-Process `
        -FilePath $MysqldExe `
        -ArgumentList "--defaults-file=$MysqlIni" `
        -WorkingDirectory $MysqlRoot `
        -WindowStyle Hidden | Out-Null
    Wait-LocalMysql
}

Write-Host 'Local MySQL is ready: 127.0.0.1:3306 / mercado'

Stop-ProjectPort -Port 5000
Stop-ProjectPort -Port 8011

$stdoutLog = Join-Path $LogRoot 'workbench.stdout.log'
$stderrLog = Join-Path $LogRoot 'workbench.stderr.log'

Write-Host 'Starting workbench service...'
$service = Start-Process `
    -FilePath $python `
    -ArgumentList @('-m', 'bit.bit_interface', '--role', 'server', '--db-host', $DbHost) `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden `
    -PassThru

Write-Host "Main service PID: $($service.Id)"
Wait-ListeningPort -Port 5000 -TimeoutSeconds 60 | Out-Null
$yandexPython = Join-Path $ProjectRoot 'yandex\.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $yandexPython) {
    Wait-ListeningPort -Port 8011 -TimeoutSeconds 30 -Optional | Out-Null
} else {
    Write-Warning 'Yandex helper environment is not installed; main workbench is still available.'
}

Write-Host ''
Write-Host 'Startup complete.'
Write-Host 'URL: http://127.0.0.1:5000/login'
Write-Host "Logs: $LogRoot"
