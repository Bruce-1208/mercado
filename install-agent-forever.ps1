$ErrorActionPreference = "Stop"

$agentDir = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$configPath = Join-Path $agentDir "local-agent.json"
$exePath = Join-Path $agentDir "MercadoLocalAgent.exe"
$sourcePath = Join-Path $agentDir "local_agent.py"

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "找不到 $configPath。请把脚本放在解压后的泽顺 Agent 安装包目录中运行。"
}

if (Test-Path -LiteralPath $exePath -PathType Leaf) {
    $agentExecutable = $exePath
    $agentArguments = '--config "{0}"' -f $configPath
} elseif (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
    $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "源码版 Agent 需要 Python 3。请先安装 Python 3，或从泽顺控制台下载正式 Windows Agent 安装包。"
    }

    $pythonPath = (& $pythonCommand.Source -3 -c "import sys; print(sys.executable)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $pythonPath) {
        throw "无法找到 Python 3。"
    }
    $agentExecutable = Join-Path (Split-Path -Parent $pythonPath) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $agentExecutable -PathType Leaf)) {
        throw "找不到 pythonw.exe：$agentExecutable"
    }

    $requirementsPath = Join-Path $agentDir "requirements-agent.txt"
    if (Test-Path -LiteralPath $requirementsPath -PathType Leaf) {
        & $pythonCommand.Source -3 -m pip install -r $requirementsPath
        if ($LASTEXITCODE -ne 0) {
            throw "Agent Python 依赖安装失败。"
        }
    }
    $agentArguments = '"{0}" --config "{1}"' -f $sourcePath, $configPath
} else {
    throw "找不到 MercadoLocalAgent.exe 或 local_agent.py。请将脚本放在解压后的泽顺 Agent 安装包目录中运行。"
}

$taskName = "ZeshunMercadoLocalAgent"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$dataDir = Join-Path $env:LOCALAPPDATA "Zeshun\MercadoLocalAgent"
$null = New-Item -ItemType Directory -Path $dataDir -Force
$stopPath = Join-Path $dataDir "agent-supervisor.stop"
$supervisorPath = Join-Path $dataDir "agent-supervisor.ps1"
$powerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

@'
param([Parameter(Mandatory = $true)][string]$AgentDirectory)

$ErrorActionPreference = "Continue"
$agentDir = (Resolve-Path -LiteralPath $AgentDirectory).Path
$configPath = Join-Path $agentDir "local-agent.json"
$dataDir = Join-Path $env:LOCALAPPDATA "Zeshun\MercadoLocalAgent"
$stopPath = Join-Path $dataDir "agent-supervisor.stop"
$logPath = Join-Path $dataDir "agent-supervisor.log"

function Write-SupervisorLog([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

$exePath = Join-Path $agentDir "MercadoLocalAgent.exe"
$sourcePath = Join-Path $agentDir "local_agent.py"
if (Test-Path -LiteralPath $exePath -PathType Leaf) {
    $agentExecutable = $exePath
    $agentArguments = '--config "{0}"' -f $configPath
} elseif (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
    $pythonPath = (& py.exe -3 -c "import sys; print(sys.executable)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $pythonPath) {
        throw "无法找到 Python 3。"
    }
    $agentExecutable = Join-Path (Split-Path -Parent $pythonPath) "pythonw.exe"
    $agentArguments = '"{0}" --config "{1}"' -f $sourcePath, $configPath
} else {
    throw "找不到泽顺 Agent 可执行文件。"
}

Write-SupervisorLog "守护进程已启动，Agent 目录：$agentDir"
while (-not (Test-Path -LiteralPath $stopPath)) {
    $agentProcess = $null
    try {
        $agentProcess = Start-Process -FilePath $agentExecutable -ArgumentList $agentArguments -WorkingDirectory $agentDir -PassThru
        Write-SupervisorLog "已启动 Agent，PID=$($agentProcess.Id)"
        while (-not $agentProcess.HasExited) {
            if (Test-Path -LiteralPath $stopPath) {
                Stop-Process -Id $agentProcess.Id -Force -ErrorAction SilentlyContinue
                break
            }
            Start-Sleep -Seconds 2
            $agentProcess.Refresh()
        }
        if (-not (Test-Path -LiteralPath $stopPath)) {
            $agentProcess.Refresh()
            Write-SupervisorLog "Agent 已退出（退出码 $($agentProcess.ExitCode)），5 秒后重新启动。"
        }
    } catch {
        Write-SupervisorLog "启动 Agent 失败：$($_.Exception.Message)；5 秒后重试。"
    }

    for ($i = 0; $i -lt 5; $i++) {
        if (Test-Path -LiteralPath $stopPath) { break }
        Start-Sleep -Seconds 1
    }
}
Write-SupervisorLog "收到卸载或停止请求，守护进程退出。"
'@ | Set-Content -LiteralPath $supervisorPath -Encoding UTF8

# Stop a previously installed instance before replacing its scheduled task.
$null = New-Item -ItemType File -Path $stopPath -Force
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue

$actionArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -AgentDirectory "{1}"' -f $supervisorPath, $agentDir
$action = New-ScheduledTaskAction -Execute $powerShellPath -Argument $actionArguments -WorkingDirectory $agentDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "泽顺本机 Agent 登录启动与异常恢复" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "泽顺本机 Agent 已设置为登录后持续运行。Agent 退出或崩溃时会自动重启。" -ForegroundColor Green
Write-Host "请保持 Windows 用户已登录、电脑未休眠，并保持比特浏览器客户端运行。" -ForegroundColor Yellow
Write-Host "如需卸载自动启动，请运行同目录下的 uninstall-agent.ps1。" -ForegroundColor Gray
