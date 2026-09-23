#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="${WORKBENCH_PROJECT_ROOT:-$SCRIPT_DIR}"
readonly PORT=5000
readonly RUN_DIR="$PROJECT_ROOT/.run"
readonly LOG_DIR="$PROJECT_ROOT/.logs"
readonly PID_FILE="$RUN_DIR/mercado-workbench.pid"
readonly LOG_FILE="${WORKBENCH_LOG_FILE:-$LOG_DIR/mercado-workbench.log}"
readonly STOP_TIMEOUT="${WORKBENCH_STOP_TIMEOUT:-20}"
readonly START_TIMEOUT="${WORKBENCH_START_TIMEOUT:-30}"

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

is_workbench_process() {
    local pid="$1"
    local command_line

    kill -0 "$pid" 2>/dev/null || return 1
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$command_line" == *"-m bit.bit_interface"* ]]
}

read_pid_file() {
    local pid

    [[ -f "$PID_FILE" ]] || return 1
    pid="$(tr -d '[:space:]' < "$PID_FILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$pid"
}

find_legacy_pid() {
    local pid

    command -v lsof >/dev/null 2>&1 || return 1
    while IFS= read -r pid; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        if is_workbench_process "$pid"; then
            printf '%s\n' "$pid"
            return 0
        fi
    done < <(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u)
    return 1
}

wait_for_exit() {
    local pid="$1"
    local elapsed=0

    while kill -0 "$pid" 2>/dev/null; do
        (( elapsed >= STOP_TIMEOUT )) && return 1
        sleep 1
        ((elapsed += 1))
    done
}

stop_service() {
    local pid=""

    pid="$(read_pid_file || true)"
    if [[ -n "$pid" ]] && ! is_workbench_process "$pid"; then
        printf '[提示] PID 文件已失效，忽略 PID %s。\n' "$pid"
        pid=""
    fi
    [[ -n "$pid" ]] || pid="$(find_legacy_pid || true)"

    if [[ -z "$pid" ]]; then
        printf '[提示] 未发现运行中的工作台服务。\n'
        rm -f -- "$PID_FILE"
        return
    fi

    printf '[停止] 正在停止工作台服务（PID %s）...\n' "$pid"
    kill -TERM "$pid"
    if ! wait_for_exit "$pid"; then
        printf '[停止] 等待 %s 秒后仍未退出，正在强制停止...\n' "$STOP_TIMEOUT"
        kill -KILL "$pid" 2>/dev/null || true
        wait_for_exit "$pid" || die "无法停止 PID $pid"
    fi
    rm -f -- "$PID_FILE"
    printf '[停止] 服务已停止。\n'
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

port_is_ready() {
    if command -v curl >/dev/null 2>&1; then
        curl --silent --show-error --output /dev/null \
            --connect-timeout 1 --max-time 2 "http://127.0.0.1:$PORT/"
        return
    fi
    command -v lsof >/dev/null 2>&1 && \
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

start_service() {
    local python_cmd="$1"
    local pid elapsed=0

    mkdir -p -- "$RUN_DIR" "$LOG_DIR"
    cd -- "$PROJECT_ROOT"

    printf '[启动] 使用 %s 启动工作台...\n' "$python_cmd"
    nohup "$python_cmd" -m bit.bit_interface --role server \
        >>"$LOG_FILE" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"

    while (( elapsed < START_TIMEOUT )); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f -- "$PID_FILE"
            printf '\n最近的服务日志：\n' >&2
            tail -n 30 -- "$LOG_FILE" >&2 || true
            die "工作台进程启动后意外退出"
        fi
        if port_is_ready; then
            printf '[成功] 工作台已启动：http://127.0.0.1:%s（PID %s）\n' "$PORT" "$pid"
            printf '[日志] %s\n' "$LOG_FILE"
            return
        fi
        sleep 1
        ((elapsed += 1))
    done

    kill -TERM "$pid" 2>/dev/null || true
    rm -f -- "$PID_FILE"
    printf '\n最近的服务日志：\n' >&2
    tail -n 30 -- "$LOG_FILE" >&2 || true
    die "服务未在 $START_TIMEOUT 秒内监听端口 $PORT"
}

main() {
    local python_cmd

    [[ -f "$PROJECT_ROOT/bit/bit_interface.py" ]] || \
        die "项目目录不正确，未找到 bit/bit_interface.py：$PROJECT_ROOT"
    is_positive_integer "$STOP_TIMEOUT" || die "WORKBENCH_STOP_TIMEOUT 必须是正整数"
    is_positive_integer "$START_TIMEOUT" || die "WORKBENCH_START_TIMEOUT 必须是正整数"

    python_cmd="$(select_python)"
    printf 'Mercado Workbench 服务重启\n'
    printf '[项目] %s\n' "$PROJECT_ROOT"
    printf '[数据库] %s:%s\n' "$MYSQL_HOST" "$MYSQL_PORT"
    stop_service
    start_service "$python_cmd"
}

main "$@"
