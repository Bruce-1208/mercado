"""Create user-downloadable local Agent installation packages."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path


SUPPORTED_AGENT_PLATFORMS = frozenset(("windows", "macos"))


def normalize_agent_platform(value):
    normalized = str(value or "windows").strip().lower()
    aliases = {
        "win": "windows",
        "win32": "windows",
        "darwin": "macos",
        "mac": "macos",
        "osx": "macos",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_AGENT_PLATFORMS:
        raise ValueError("Agent 下载平台仅支持 windows 或 macos")
    return normalized


def _windows_start_script(has_executable):
    setup = (
        ""
        if has_executable
        else 'py -3 -c "import requests" 2>nul || py -3 -m pip install -r "%~dp0requirements-agent.txt"\n'
    )
    command = (
        'start "" "%~dp0MercadoLocalAgent.exe" --config "%~dp0local-agent.json"'
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
    executable_setup = (
        '$executable = Join-Path $agentDir "MercadoLocalAgent.exe"'
        if has_executable
        else '''$python = (& py.exe -3 -c "import sys; print(sys.executable)").Trim()
$executable = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "未找到 pythonw.exe：$executable"
}'''
    )
    argument_setup = (
        '$arguments = "--config ```"$agentDir\\local-agent.json```""'
        if has_executable
        else '$arguments = "```"$agentDir\\local_agent.py```" --config ```"$agentDir\\local-agent.json```""'
    ).replace("```", "`")
    return f"""$ErrorActionPreference = "Stop"
$agentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
{executable_setup}
{"" if has_executable else "& py.exe -3 -m pip install -r (Join-Path $agentDir 'requirements-agent.txt')"}
{argument_setup}
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments -WorkingDirectory $agentDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "ZeshunMercadoLocalAgent" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "泽顺本机比特浏览器自动化 Agent" -Force | Out-Null
Start-ScheduledTask -TaskName "ZeshunMercadoLocalAgent"
Write-Host "泽顺本机 Agent 已安装并启动。" -ForegroundColor Green
"""


def _windows_uninstall_script():
    return '''$ErrorActionPreference = "Stop"
$taskName = "ZeshunMercadoLocalAgent"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Write-Host "泽顺本机 Agent 登录启动任务已移除。" -ForegroundColor Green
'''


def _windows_unblock_script():
    return r'''$ErrorActionPreference = "Stop"
$agentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-ChildItem -LiteralPath $agentDir -Recurse -File | Unblock-File
Write-Host "Agent files have been unblocked. You can run start-agent.bat now." -ForegroundColor Green
'''


def _macos_run_script(has_executable):
    if has_executable:
        command = (
            'chmod +x "$AGENT_DIR/MercadoLocalAgent"\n'
            'exec "$AGENT_DIR/MercadoLocalAgent" '
            '--config "$AGENT_DIR/local-agent.json"'
        )
    else:
        command = '''if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 Python 3，请先安装 Python 3 后重试。" >&2
    exit 1
fi
VENV_DIR="$AGENT_DIR/.agent-venv"
if [ ! -x "$VENV_DIR/bin/python3" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python3" -c "import requests" >/dev/null 2>&1 || \\
    "$VENV_DIR/bin/python3" -m pip install -r "$AGENT_DIR/requirements-agent.txt"
exec "$VENV_DIR/bin/python3" "$AGENT_DIR/local_agent.py" --config "$AGENT_DIR/local-agent.json"'''
    return f"""#!/bin/zsh
set -e
AGENT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$AGENT_DIR"
{command}
"""


def _macos_start_script():
    return """#!/bin/zsh
AGENT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
"$AGENT_DIR/run-agent.sh"
status=$?
if [ "$status" -ne 0 ]; then
    echo
    echo "Agent 启动失败（退出码 $status）。按回车键关闭窗口。"
    read -r
fi
exit "$status"
"""


def _macos_install_script():
    return r'''#!/bin/zsh
set -e
AGENT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LABEL="com.zeshun.mercado-local-agent"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/Zeshun"

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

mkdir -p "$PLIST_DIR" "$LOG_DIR"
chmod +x "$AGENT_DIR/run-agent.sh" "$AGENT_DIR/start-agent.command"
agent_dir_xml="$(xml_escape "$AGENT_DIR")"
log_dir_xml="$(xml_escape "$LOG_DIR")"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$agent_dir_xml/run-agent.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$agent_dir_xml</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$log_dir_xml/MercadoLocalAgent.log</string>
    <key>StandardErrorPath</key>
    <string>$log_dir_xml/MercadoLocalAgent.log</string>
</dict>
</plist>
EOF

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "泽顺本机 Agent 已安装并启动。"
echo "运行日志：$LOG_DIR/MercadoLocalAgent.log"
'''


def _macos_uninstall_script():
    return r'''#!/bin/zsh
set -e
LABEL="com.zeshun.mercado-local-agent"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"
echo "泽顺本机 Agent 登录启动项已卸载。Agent 身份和业务数据仍保留。"
'''


def _executable_candidates(project_root, target_platform):
    if target_platform == "windows":
        configured = str(os.environ.get("BIT_LOCAL_AGENT_EXECUTABLE") or "").strip()
        candidates = (
            Path(configured).expanduser() if configured else None,
            project_root / "dist" / "MercadoLocalAgent.exe",
            project_root / "dist" / "MercadoLocalAgent" / "MercadoLocalAgent.exe",
        )
    else:
        configured = str(
            os.environ.get("BIT_LOCAL_AGENT_MACOS_EXECUTABLE") or ""
        ).strip()
        candidates = (
            Path(configured).expanduser() if configured else None,
            project_root / "dist" / "macos" / "MercadoLocalAgent",
            project_root / "dist" / "MercadoLocalAgent-macos",
        )
    return tuple(path for path in candidates if path is not None)


def _writestr(archive, name, content, *, executable=False):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = ((0o755 if executable else 0o644) & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(
        info,
        content.encode("utf-8") if isinstance(content, str) else content,
    )


def _build_windows_archive(archive, project_root, executable_path):
    has_executable = executable_path is not None
    if has_executable:
        _writestr(
            archive,
            "MercadoLocalAgent.exe",
            executable_path.read_bytes(),
            executable=True,
        )
    else:
        _writestr(
            archive,
            "local_agent.py",
            (project_root / "local_agent.py").read_bytes(),
        )
        _writestr(archive, "requirements-agent.txt", "requests>=2.31,<3\n")
    _writestr(archive, "start-agent.bat", _windows_start_script(has_executable))
    _writestr(archive, "unblock-agent.ps1", _windows_unblock_script())
    _writestr(archive, "install-agent.ps1", _windows_install_script(has_executable))
    _writestr(archive, "uninstall-agent.ps1", _windows_uninstall_script())


def _build_macos_archive(archive, project_root, executable_path):
    has_executable = executable_path is not None
    if has_executable:
        _writestr(
            archive,
            "MercadoLocalAgent",
            executable_path.read_bytes(),
            executable=True,
        )
    else:
        _writestr(
            archive,
            "local_agent.py",
            (project_root / "local_agent.py").read_bytes(),
        )
        _writestr(archive, "requirements-agent.txt", "requests>=2.31,<3\n")
    _writestr(archive, "run-agent.sh", _macos_run_script(has_executable), executable=True)
    _writestr(archive, "start-agent.command", _macos_start_script(), executable=True)
    _writestr(archive, "install-agent.command", _macos_install_script(), executable=True)
    _writestr(archive, "uninstall-agent.command", _macos_uninstall_script(), executable=True)


def _readme(target_platform, has_executable):
    source_notice = (
        "\n当前安装包是 Python 源码测试版，目标电脑还需要 Python 3 和完整的项目运行依赖；"
        f"正式使用请让管理员在服务器配置 {'Windows' if target_platform == 'windows' else 'macOS'} Agent 可执行文件。\n"
        if not has_executable
        else ""
    )
    if target_platform == "windows":
        steps = """1. 解压本安装包到固定目录，不要直接在压缩包内运行。
2. 如果 Windows 提示“应用和浏览器控制已阻止可能不安全的应用”，先右键 ZIP 文件打开“属性”，勾选“解除锁定/Unblock”，应用后重新解压。
3. 如果已经解压，在当前目录打开 PowerShell，执行 `Unblock-File -Path .\\unblock-agent.ps1`，再双击 start-agent.bat。也可以右键运行 unblock-agent.ps1。
4. 双击 start-agent.bat 可立即启动；正式 EXE 只显示运行状态窗口，不会常驻 CMD 窗口；关闭窗口并确认后会停止当前任务并退出 Agent。
5. 右键 install-agent.ps1，选择“使用 PowerShell 运行”，可安装为当前用户登录后自动启动任务。
6. 如需取消登录启动，运行 uninstall-agent.ps1。"""
        remaining_steps = """7. 第一次联网会自动注册，并从泽顺控制台下载经过哈希校验的最新业务代码。
8. 控制台出现这台电脑的名称后，即可选择它执行本机任务。

如果解除文件锁定后仍然被组织的应用控制策略拦截，说明该电脑禁止未签名程序运行；请使用管理员提供的已签名 MercadoLocalAgent.exe，不能通过启动脚本安全地绕过该策略。"""
    else:
        steps = """1. 解压本安装包到固定目录，不要直接在压缩包内运行。
2. 双击 start-agent.command 可立即启动；运行状态窗口会实时显示本机时间和日志，关闭窗口并确认后会停止当前任务并退出 Agent。如果 macOS 拦截，请在“系统设置 → 隐私与安全性”中允许打开。
3. 关闭手动启动的 Agent 后，双击 install-agent.command，可安装为登录后自动启动的 LaunchAgent。
4. 如需取消登录启动，双击 uninstall-agent.command。"""
        remaining_steps = """5. 第一次联网会自动注册，并从泽顺控制台下载经过哈希校验的最新业务代码。
6. 控制台出现这台电脑的名称后，即可选择它执行本机任务。"""
    return f"""泽顺本机 Agent（{'Windows' if target_platform == 'windows' else 'macOS'}）
================

{steps}
{remaining_steps}

注意：必须保持比特浏览器客户端已启动。注册链接具有有效期；若首次注册提示过期，请重新从控制台下载安装包。
{source_notice}
"""


def build_agent_distribution(
    project_root, *, server_url, enrollment_token, target_platform="windows"
):
    project_root = Path(project_root).resolve()
    target_platform = normalize_agent_platform(target_platform)
    executable_path = next(
        (
            path
            for path in _executable_candidates(project_root, target_platform)
            if path.is_file()
        ),
        None,
    )
    has_executable = executable_path is not None
    config = {
        "server_url": str(server_url).rstrip("/"),
        "enrollment_token": str(enrollment_token),
        "name": "",
        "poll_seconds": 10,
        "heartbeat_seconds": 10,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if target_platform == "windows":
            _build_windows_archive(archive, project_root, executable_path)
        else:
            _build_macos_archive(archive, project_root, executable_path)
        _writestr(
            archive,
            "local-agent.json",
            json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        _writestr(archive, "README.txt", _readme(target_platform, has_executable))
    package_format = (
        "windows-exe"
        if has_executable and target_platform == "windows"
        else "macos-executable"
        if has_executable
        else "python-source"
    )
    return {
        "content": buffer.getvalue(),
        "format": package_format,
        "has_executable": has_executable,
        "platform": target_platform,
    }
