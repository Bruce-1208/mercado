#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd -- "${WORKBENCH_PROJECT_ROOT:-$SCRIPT_DIR}" && pwd -P)"
readonly PORT=5000
readonly WORKER_PORT="${BIT_WORKER_PORT:-5001}"
readonly ORDERS_PORT="${BIT_ORDERS_PORT:-$((WORKER_PORT + 1))}"
readonly STORES_PORT="${BIT_STORES_PORT:-$((WORKER_PORT + 2))}"
readonly MAINTENANCE_PORT="${BIT_MAINTENANCE_PORT:-$((WORKER_PORT + 3))}"
readonly RUN_DIR="$PROJECT_ROOT/.run"
readonly LOG_DIR="$PROJECT_ROOT/.logs"
readonly PID_FILE="$RUN_DIR/mercado-workbench.pid"
LOG_FILE="${WORKBENCH_LOG_FILE:-$LOG_DIR/mercado-workbench.log}"
readonly STOP_TIMEOUT="${WORKBENCH_STOP_TIMEOUT:-20}"
readonly START_TIMEOUT="${WORKBENCH_START_TIMEOUT:-60}"

# The copied local database is the safe default for every workbench restart.
# Set MERCADO_MYSQL_HOST/MERCADO_MYSQL_PORT only when this launcher must use a
# different database explicitly.
export MYSQL_HOST="${MERCADO_MYSQL_HOST:-127.0.0.1}"
export MYSQL_PORT="${MERCADO_MYSQL_PORT:-3306}"

die() {
    printf '[失败] %s\n' "$*" >&2
    exit 1
}

is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

process_alive() {
    local state
    state="$(ps -p "$1" -o stat= 2>/dev/null || true)"
    [[ -n "$state" && "$state" != *Z* ]]
}

is_workbench_process() {
    local command_line cwd
    command_line="$(ps -p "$1" -o command= 2>/dev/null || true)"
    [[ "$command_line" =~ -m[[:space:]]bit\.(bit_interface|workbench_services)([[:space:]]|$) ]] || return 1
    cwd="$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
    [[ "$cwd" == "$PROJECT_ROOT" ]]
}

workbench_pids() {
    local pid
    while read -r pid; do
        if is_workbench_process "$pid"; then
            printf '%s\n' "$pid"
        fi
    done < <(ps -axo pid=,command= | awk '/-m bit[.](bit_interface|workbench_services)( |$)/ {print $1}')
}

listener_pids() {
    lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

wait_for_exit() {
    local elapsed=0
    while process_alive "$1"; do
        (( elapsed >= STOP_TIMEOUT )) && return 1
        sleep 1
        ((elapsed += 1))
    done
}

ensure_process_access() {
    ps -axo pid=,command= >/dev/null 2>&1 ||
        die "无法读取系统进程列表（ps 被权限或沙箱限制）；请在本机终端运行重启脚本，或授予执行权限"
}

with_restart_lock() {
    local python_cmd="$1"
    shift
    mkdir -p -- "$RUN_DIR"
    # Keep this inode: unlinking a flock file allows two independent locks.
    # FD 9 survives exec into Bash; the kernel releases it even after SIGKILL.
    exec "$python_cmd" - "$RUN_DIR/restart.flock" "$@" <<'PYLOCK'
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('[失败] 已有重启正在执行，请等待其完成')
if fd != 9:
    os.dup2(fd, 9)
    os.close(fd)
os.set_inheritable(9, True)
os.execvp(sys.argv[2], sys.argv[2:])
PYLOCK
}

wait_for_port_release() {
    local port="$1" pid pids elapsed=0
    while true; do
        pids="$(listener_pids "$port")"
        [[ -n "$pids" ]] || return 0
        for pid in $pids; do
            # Only stop a verified process from this checkout, never an unrelated listener.
            if is_workbench_process "$pid"; then
                kill -TERM "$pid" 2>/dev/null || true
                if ! wait_for_exit "$pid"; then
                    is_workbench_process "$pid" && kill -KILL "$pid" 2>/dev/null || true
                    wait_for_exit "$pid" || die "无法停止端口 $port 的工作台进程 $pid"
                fi
            fi
        done
        [[ -n "$(listener_pids "$port")" ]] || return 0
        (( elapsed >= STOP_TIMEOUT )) && break
        sleep 1
        ((elapsed += 1))
    done
    printf '[占用详情] 端口 %s\n' "$port" >&2
    for pid in $(listener_pids "$port"); do
        ps -p "$pid" -o pid=,ppid=,command= >&2 || true
        lsof -a -p "$pid" -d cwd >&2 || true
    done
    die "端口 $port 仍被占用，未启动新服务；未强制结束无法确认为本项目的进程，请查看上方占用详情"
}

stop_service() {
    local pid command_line port
    local old_pids
    old_pids="$(workbench_pids)"
    # Unload KeepAlive first, otherwise launchd races us by respawning the service.
    if [[ -n "$launch_target" ]] && launchctl print "$launch_target" >/dev/null 2>&1; then
        launchctl bootout "$launch_target" || die "无法暂停 launchd 服务"
    fi
    # Stop supervisors before children; no supervisor may respawn old children.
    for pid in $old_pids; do
        command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
        if [[ "$command_line" == *"-m bit.workbench_services"* && "$command_line" != *"--child"* ]]; then
            kill -TERM "$pid" 2>/dev/null || true
            wait_for_exit "$pid" || {
                kill -KILL "$pid" 2>/dev/null || true
                wait_for_exit "$pid" || die "无法停止管理进程 $pid"
            }
        fi
    done
    for pid in $old_pids $(workbench_pids); do
        is_workbench_process "$pid" || continue
        printf '[停止] 工作台进程 %s\n' "$pid"
        kill -TERM "$pid" 2>/dev/null || true
        wait_for_exit "$pid" || {
            kill -KILL "$pid" 2>/dev/null || true
            wait_for_exit "$pid" || die "无法停止进程 $pid"
        }
    done
    rm -f -- "$PID_FILE"
    for port in "$PORT" "$WORKER_PORT" "$ORDERS_PORT" "$STORES_PORT" "$MAINTENANCE_PORT"; do
        wait_for_port_release "$port"
    done
}

select_python() {
    local candidate

    if [[ -n "${WORKBENCH_PYTHON:-}" ]]; then
        [[ -x "$WORKBENCH_PYTHON" ]] || die "WORKBENCH_PYTHON 不可执行：$WORKBENCH_PYTHON"
        printf '%s\n' "$WORKBENCH_PYTHON"
        return
    fi

    for candidate in "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/venv/bin/python"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done

    command -v python3 2>/dev/null || command -v python 2>/dev/null || \
        die "未找到 Python；请设置 WORKBENCH_PYTHON"
}

child_listener() {
    local pid parent
    for pid in $(listener_pids "$2"); do
        parent="$(ps -p "$pid" -o ppid= | tr -d '[:space:]')"
        [[ "$parent" == "$1" ]] && is_workbench_process "$pid" || return 1
        printf '%s\n' "$pid"
        return 0
    done
    return 1
}

isolated_children_ready() {
    local port
    for port in "$ORDERS_PORT" "$STORES_PORT" "$MAINTENANCE_PORT"; do
        child_listener "$1" "$port" >/dev/null || return 1
    done
}

start_service() {
    local python_cmd="$1" pid="" elapsed=0 web_pid worker_pid
    if [[ "${system_managed:-0}" == 1 ]]; then
        : # System launchd restarts the existing job after its old PID exits.
    elif [[ -n "$launch_target" ]]; then
        # launchd persists disabled overrides independently of the plist.
        # An explicit restart must enable the job before bootstrap (otherwise
        # launchd reports the misleading "5: Input/output error").
        launchctl enable "$launch_target" || die "无法启用 launchd 服务：$launch_target"
        if ! launchctl bootstrap "${launch_target%/*}" "$launch_plist"; then
            launchctl print-disabled "${launch_target%/*}" >&2 || true
            /usr/bin/plutil -lint "$launch_plist" >&2 || true
            die "无法加载 launchd 服务：$launch_plist（已尝试启用；请查看上方诊断）"
        fi
        launchctl kickstart "$launch_target" || die "无法启动 launchd 服务"
    else
        nohup "$python_cmd" -m bit.workbench_services 9>&- </dev/null >>"$LOG_FILE" 2>&1 &
        pid=$!
    fi
    while (( elapsed < START_TIMEOUT )); do
        if [[ -n "$launch_target" ]]; then
            # launchd can briefly have no readable job while replacing it.
            # Treat that as not ready, not a fatal errexit/pipefail error.
            # Drain the output too: early awk exit can give launchctl SIGPIPE.
            if ! pid="$(launchctl print "$launch_target" 2>/dev/null | awk '/^[[:space:]]*pid = / && !found {print $3; found=1}')"; then
                pid=""
            fi
        fi
        if [[ -n "$pid" ]] && process_alive "$pid"; then
            web_pid="$(child_listener "$pid" "$PORT" || true)"
            worker_pid="$(child_listener "$pid" "$WORKER_PORT" || true)"
            if [[ -n "$web_pid" && -n "$worker_pid" ]] && isolated_children_ready "$pid" &&
                curl --noproxy '*' --fail --silent --output /dev/null --connect-timeout 1 --max-time 2 "http://127.0.0.1:$PORT/"; then
                # Recheck ownership after HTTP, so an old listener cannot report success.
                if [[ "$(child_listener "$pid" "$PORT" || true)" == "$web_pid" &&
                      "$(child_listener "$pid" "$WORKER_PORT" || true)" == "$worker_pid" ]] && isolated_children_ready "$pid"; then
                    printf '%s\n' "$pid" > "$PID_FILE"
                    printf '[成功] 新工作台已启动：http://127.0.0.1:%s\n' "$PORT"
                    printf '[进程] 管理=%s Web=%s Worker=%s\n' "$pid" "$web_pid" "$worker_pid"
                    printf '[日志] %s\n' "$LOG_FILE"
                    return
                fi
            fi
        elif [[ -z "$launch_target" ]]; then
            break
        fi
        sleep 1
        ((elapsed += 1))
    done
    if [[ "${system_managed:-0}" != 1 ]]; then stop_service; fi
    tail -n 30 -- "$LOG_FILE" >&2 || true
    die "新 Web/全部后台服务未在 $START_TIMEOUT 秒内就绪，请检查 .logs/services/ 分组日志"
}

main() {
    local python_cmd launch_plist="" launch_target="" config system_managed=0 old_pid pid old_pids
    [[ -f "$PROJECT_ROOT/bit/workbench_services.py" ]] || die "项目目录不正确：$PROJECT_ROOT"
    is_positive_integer "$STOP_TIMEOUT" || die "WORKBENCH_STOP_TIMEOUT 必须是正整数"
    is_positive_integer "$START_TIMEOUT" || die "WORKBENCH_START_TIMEOUT 必须是正整数"
    is_positive_integer "$WORKER_PORT" && (( WORKER_PORT >= 1024 && WORKER_PORT <= 65535 && WORKER_PORT != PORT )) || die "BIT_WORKER_PORT 无效"
    command -v lsof >/dev/null || die "需要安装 lsof"
    command -v curl >/dev/null || die "需要安装 curl"
    # Fail before unloading launchd or deleting PID state if discovery is denied.
    ensure_process_access
    python_cmd="$(select_python)"
    mkdir -p -- "$RUN_DIR" "$LOG_DIR"
    cd -- "$PROJECT_ROOT"
    # Prefer the installed system daemon. A user LaunchAgent with the same
    # label otherwise races its KeepAlive respawn during every restart.
    if [[ "$(uname -s)" == Darwin && -f /Library/LaunchDaemons/com.zeshun.mercado-workbench.plist ]] &&
        launchctl print system/com.zeshun.mercado-workbench >/dev/null 2>&1; then
        system_managed=1
        launch_target=system/com.zeshun.mercado-workbench
        config="$("$python_cmd" - "$PROJECT_ROOT" "$WORKER_PORT" "$ORDERS_PORT" "$STORES_PORT" "$MAINTENANCE_PORT" <<'PYSYSTEM'
import os, plistlib, sys
from pathlib import Path
with open('/Library/LaunchDaemons/com.zeshun.mercado-workbench.plist', 'rb') as f:
    config = plistlib.load(f)
if Path(config.get('WorkingDirectory', '')).resolve() != Path(sys.argv[1]).resolve():
    raise SystemExit('系统托管项目目录不匹配')
user_file = Path.home() / 'Library/LaunchAgents/com.zeshun.mercado-workbench.plist'
if user_file.exists():
    user_config = plistlib.loads(user_file.read_bytes())
    if Path(user_config.get('WorkingDirectory', '')).resolve() != Path(sys.argv[1]).resolve():
        raise SystemExit('同名用户托管属于其他项目，不能自动停用')
os.environ.update(config.get('EnvironmentVariables', {}))
sys.path.insert(0, sys.argv[1])
from bit.workbench_services import child_environment
from bit.service_topology import GROUPS, group_port, validate_topology
validate_topology({name: child_environment(name) for name in (*GROUPS, 'web')})
if [group_port(name) for name in GROUPS] != [int(value) for value in sys.argv[2:]]:
    raise SystemExit('系统托管端口与重启脚本环境不一致')
print(config.get('StandardOutPath', ''))
PYSYSTEM
)" || die "系统服务配置校验失败"
        LOG_FILE="$config"
        old_pid="$(launchctl print "$launch_target" | awk '/^[[:space:]]*pid = / {print $3; exit}')"
        [[ -n "$old_pid" ]] && is_workbench_process "$old_pid" || die "系统托管未运行本项目管理进程，请稍后重试"
        old_pids="$(workbench_pids)"
        if launchctl print "gui/$(id -u)/com.zeshun.mercado-workbench" >/dev/null 2>&1; then
            launchctl bootout "gui/$(id -u)/com.zeshun.mercado-workbench" || die "无法卸载重复的用户托管"
        fi
        launchctl disable "gui/$(id -u)/com.zeshun.mercado-workbench" || die "无法停用重复的用户托管"
        printf '[托管] %s（保留系统服务，仅重启本项目进程）\n' "$launch_target"
        kill -TERM "$old_pid" || die "无法通知当前管理进程退出"
        # Only the captured old PIDs may be stopped. Never chase launchd's new
        # supervisor or its children through a refreshed process listing.
        for pid in $old_pids; do
            if ! wait_for_exit "$pid"; then
                is_workbench_process "$pid" && kill -KILL "$pid" 2>/dev/null || true
            fi
        done
        start_service "$python_cmd"
        return
    fi
    if [[ "$(uname -s)" == Darwin && -f "$HOME/Library/LaunchAgents/com.zeshun.mercado-workbench.plist" ]]; then
        launch_plist="$HOME/Library/LaunchAgents/com.zeshun.mercado-workbench.plist"
        config="$("$python_cmd" - "$launch_plist" "$PROJECT_ROOT" "$WORKER_PORT" "$ORDERS_PORT" "$STORES_PORT" "$MAINTENANCE_PORT" <<'PYCONFIG'
import plistlib, sys
from pathlib import Path
with open(sys.argv[1], 'rb') as f:
    config = plistlib.load(f)
if Path(config.get('WorkingDirectory', '')).resolve() != Path(sys.argv[2]).resolve():
    raise SystemExit('launchd 项目目录不匹配')
args = config.get('ProgramArguments', [])
if args[-2:] != ['-m', 'bit.workbench_services']:
    raise SystemExit('launchd 启动命令不是 bit.workbench_services，请先更新托管配置')
port = str(config.get('EnvironmentVariables', {}).get('BIT_WORKER_PORT', '5001'))
if port != sys.argv[3]:
    raise SystemExit('launchd 的 BIT_WORKER_PORT 与当前脚本不一致')
for index, name in enumerate(('BIT_ORDERS_PORT', 'BIT_STORES_PORT', 'BIT_MAINTENANCE_PORT'), 1):
    value = str(config.get('EnvironmentVariables', {}).get(name, int(port) + index))
    if value != sys.argv[3 + index]:
        raise SystemExit(f'launchd 的 {name} 与当前脚本不一致')
import os
os.environ.update(config.get('EnvironmentVariables', {}))
sys.path.insert(0, sys.argv[2])
from bit.workbench_services import child_environment
from bit.service_topology import GROUPS, validate_topology
validate_topology({name: child_environment(name) for name in (*GROUPS, 'web')})
print(config['Label'])
print(config.get('StandardOutPath', ''))
print(config.get('StandardErrorPath', ''))
PYCONFIG
)" || die "launchd 配置校验失败"
        launch_target="gui/$(id -u)/$(printf '%s\n' "$config" | sed -n '1p')"
        LOG_FILE="$(printf '%s\n' "$config" | sed -n '2p')"
        printf '[错误日志] %s\n' "$(printf '%s\n' "$config" | sed -n '3p')"
        printf '[托管] %s（沿用其 Python、数据库、环境变量及日志配置）\n' "$launch_target"
    else
        "$python_cmd" -c 'from bit.workbench_services import child_environment; from bit.service_topology import GROUPS, validate_topology; validate_topology({name: child_environment(name) for name in (*GROUPS, "web")})' || die "后台资源配置校验失败"
        printf '[数据库] %s:%s\n' "$MYSQL_HOST" "$MYSQL_PORT"
    fi
    printf '[项目] %s\n' "$PROJECT_ROOT"
    stop_service
    start_service "$python_cmd"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    ensure_process_access
    with_restart_lock "$(select_python)" bash -c 'source "$1"; main' restart "$SCRIPT_DIR/restart_workbench.sh"
fi
