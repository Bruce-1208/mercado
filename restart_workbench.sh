#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd -- "${WORKBENCH_PROJECT_ROOT:-$SCRIPT_DIR}" && pwd -P)"
readonly PORT=5000
readonly WORKER_PORT="${BIT_WORKER_PORT:-5001}"
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
    for port in "$PORT" "$WORKER_PORT"; do
        [[ -z "$(listener_pids "$port")" ]] || die "端口 $port 仍被占用，未启动新服务"
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

start_service() {
    local python_cmd="$1" pid="" elapsed=0 web_pid worker_pid
    if [[ -n "$launch_target" ]]; then
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
        nohup "$python_cmd" -m bit.workbench_services </dev/null >>"$LOG_FILE" 2>&1 &
        pid=$!
    fi
    while (( elapsed < START_TIMEOUT )); do
        if [[ -n "$launch_target" ]]; then
            pid="$(launchctl print "$launch_target" 2>/dev/null | awk '/^[[:space:]]*pid = / {print $3; exit}')"
        fi
        if [[ -n "$pid" ]] && process_alive "$pid"; then
            web_pid="$(child_listener "$pid" "$PORT" || true)"
            worker_pid="$(child_listener "$pid" "$WORKER_PORT" || true)"
            if [[ -n "$web_pid" && -n "$worker_pid" ]] &&
                curl --noproxy '*' --fail --silent --output /dev/null --connect-timeout 1 --max-time 2 "http://127.0.0.1:$PORT/"; then
                # Recheck ownership after HTTP, so an old listener cannot report success.
                if [[ "$(child_listener "$pid" "$PORT" || true)" == "$web_pid" &&
                      "$(child_listener "$pid" "$WORKER_PORT" || true)" == "$worker_pid" ]]; then
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
    stop_service
    tail -n 30 -- "$LOG_FILE" >&2 || true
    die "新 Web/worker 未在 $START_TIMEOUT 秒内就绪；已停止本次启动，请检查日志"
}

main() {
    local python_cmd launch_plist="" launch_target="" config
    [[ -f "$PROJECT_ROOT/bit/workbench_services.py" ]] || die "项目目录不正确：$PROJECT_ROOT"
    is_positive_integer "$STOP_TIMEOUT" || die "WORKBENCH_STOP_TIMEOUT 必须是正整数"
    is_positive_integer "$START_TIMEOUT" || die "WORKBENCH_START_TIMEOUT 必须是正整数"
    is_positive_integer "$WORKER_PORT" && (( WORKER_PORT >= 1024 && WORKER_PORT <= 65535 && WORKER_PORT != PORT )) || die "BIT_WORKER_PORT 无效"
    command -v lsof >/dev/null || die "需要安装 lsof"
    command -v curl >/dev/null || die "需要安装 curl"
    python_cmd="$(select_python)"
    mkdir -p -- "$RUN_DIR" "$LOG_DIR"
    # Atomic directory lock also works on macOS without flock. Never steal it.
    mkdir "$RUN_DIR/restart.lock" 2>/dev/null || die "已有重启正在执行；若上次被强制终止，请检查后移除 $RUN_DIR/restart.lock"
    trap 'rmdir "$RUN_DIR/restart.lock" 2>/dev/null || true' EXIT
    cd -- "$PROJECT_ROOT"
    if [[ "$(uname -s)" == Darwin && -f "$HOME/Library/LaunchAgents/com.zeshun.mercado-workbench.plist" ]]; then
        launch_plist="$HOME/Library/LaunchAgents/com.zeshun.mercado-workbench.plist"
        config="$("$python_cmd" - "$launch_plist" "$PROJECT_ROOT" "$WORKER_PORT" <<'PYCONFIG'
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
        printf '[数据库] %s:%s\n' "$MYSQL_HOST" "$MYSQL_PORT"
    fi
    printf '[项目] %s\n' "$PROJECT_ROOT"
    stop_service
    start_service "$python_cmd"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
