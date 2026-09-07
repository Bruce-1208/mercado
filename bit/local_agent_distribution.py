"""Create the user-downloadable local Agent installation package."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path


def _windows_start_script(has_executable):
    setup = (
        ""
        if has_executable
        else 'py -3 -c "import requests" 2>nul || py -3 -m pip install -r "%~dp0requirements-agent.txt"\n'
    )
    command = (
        '"%~dp0MercadoLocalAgent.exe" --config "%~dp0local-agent.json"'
        if has_executable
        else 'py -3 "%~dp0local_agent.py" --config "%~dp0local-agent.json"'
    )
    return f"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
{setup}{command}
if errorlevel 1 pause
"""


def _windows_install_script(has_executable):
    executable = (
        'Join-Path $agentDir "MercadoLocalAgent.exe"'
        if has_executable
        else '(Get-Command py.exe -ErrorAction Stop).Source'
    )
    argument_setup = (
        '$arguments = "--config ```"$agentDir\\local-agent.json```""'
        if has_executable
        else '$arguments = "-3 ```"$agentDir\\local_agent.py```" --config ```"$agentDir\\local-agent.json```""'
    ).replace("```", "`")
    return f"""$ErrorActionPreference = "Stop"
$agentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$executable = {executable}
{"" if has_executable else "& $executable -3 -m pip install -r (Join-Path $agentDir 'requirements-agent.txt')"}
{argument_setup}
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "ZeshunMercadoLocalAgent" -Action $action -Trigger $trigger -Settings $settings -Description "泽顺本机比特浏览器自动化 Agent" -Force | Out-Null
Start-ScheduledTask -TaskName "ZeshunMercadoLocalAgent"
Write-Host "泽顺本机 Agent 已安装并启动。" -ForegroundColor Green
"""


def build_agent_distribution(project_root, *, server_url, enrollment_token):
    project_root = Path(project_root).resolve()
    configured_executable = str(
        os.environ.get("BIT_LOCAL_AGENT_EXECUTABLE") or ""
    ).strip()
    executable_candidates = tuple(
        path
        for path in (
            Path(configured_executable).expanduser() if configured_executable else None,
            project_root / "dist" / "MercadoLocalAgent.exe",
            project_root / "dist" / "MercadoLocalAgent" / "MercadoLocalAgent.exe",
        )
        if path is not None
    )
    executable_path = next((path for path in executable_candidates if path.is_file()), None)
    has_executable = executable_path is not None
    config = {
        "server_url": str(server_url).rstrip("/"),
        "enrollment_token": str(enrollment_token),
        "name": "",
        "poll_seconds": 10,
        "heartbeat_seconds": 10,
    }
    source_notice = (
        "\n当前安装包是 Python 源码测试版，目标电脑还需要完整的项目运行依赖；"
        "正式使用请让管理员在服务器配置 Windows Agent EXE。\n"
        if not has_executable
        else ""
    )
    readme = f"""泽顺本机 Agent
================

1. 解压本安装包到固定目录，不要直接在压缩包内运行。
2. 双击 start-agent.bat 可立即启动。
3. 右键 install-agent.ps1，选择“使用 PowerShell 运行”，可安装为登录后自动启动任务。
4. 第一次联网会自动注册，并从泽顺控制台下载经过哈希校验的最新业务代码。
5. 控制台出现这台电脑的名称后，即可选择它执行本机申诉。

注意：必须保持比特浏览器客户端已启动。注册链接具有有效期；若首次注册提示过期，请重新从控制台下载安装包。
{source_notice}
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if has_executable:
            archive.write(executable_path, "MercadoLocalAgent.exe")
        else:
            archive.write(project_root / "local_agent.py", "local_agent.py")
            archive.writestr("requirements-agent.txt", "requests>=2.31,<3\n")
        archive.writestr(
            "local-agent.json",
            json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr("start-agent.bat", _windows_start_script(has_executable))
        archive.writestr("install-agent.ps1", _windows_install_script(has_executable))
        archive.writestr("README.txt", readme.encode("utf-8"))
    return {
        "content": buffer.getvalue(),
        "format": "windows-exe" if has_executable else "python-source",
        "has_executable": has_executable,
    }
