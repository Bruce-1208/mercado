$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$StartupScript = Join-Path $ProjectRoot 'start_local_workbench.ps1'
if (-not (Test-Path -LiteralPath $StartupScript -PathType Leaf)) {
    throw "Workbench startup script was not found: $StartupScript"
}

$TaskName = 'ZeshunMercadoWorkbench'
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$PowerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$ActionArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -KeepAlive' -f $StartupScript
$Action = New-ScheduledTaskAction `
    -Execute $PowerShellPath `
    -Argument $ActionArguments `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description '登录后启动本机 MySQL、Mercado 工作台和后台自动同步任务' `
    -Force | Out-Null

Write-Host "已设置当前 Windows 用户登录后自动启动工作台：$TaskName" -ForegroundColor Green
Write-Host '下一次登录时会先检查/启动本机 MySQL，再启动工作台和订单、店铺链接等后台调度。'
Write-Host '若工作台进程异常退出，任务计划程序会在 1 分钟后重启。'
Write-Host '如需现在启动，请先确认不会中断正在运行的工作台，再运行：'
Write-Host "Start-ScheduledTask -TaskName $TaskName"
