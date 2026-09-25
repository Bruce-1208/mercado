$ErrorActionPreference = 'Stop'

$TaskName = 'ZeshunMercadoWorkbench'
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host "登录启动任务不存在：$TaskName"
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已移除登录启动任务：$TaskName。当前已启动的工作台不会因此停止。" -ForegroundColor Green
