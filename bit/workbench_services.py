"""Supervise Web and four independently restartable loopback task services.

Run: python -m bit.workbench_services
Children share durable data and session secrets, but not pools or schedulers.
"""
import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from bit.bit_runtime_lock import InterProcessLock
from bit.service_topology import GROUPS, DEFAULT_CONNECTIONS, group_port, validate_topology


def child_environment(mode):
    env = os.environ.copy()
    # Keep private product-source paths persistent even when launchd/systemd
    # does not inherit the interactive shell's environment. Explicit env wins.
    config_file = Path(__file__).resolve().parents[1] / '.private/mercado-data/service-env.json'
    if config_file.is_file():
        configured = json.loads(config_file.read_text(encoding='utf-8'))
        if not isinstance(configured, dict):
            raise ValueError('Private Mercado service configuration must be an object')
        for key in ('ZESHUN_MERCADO_EXTENSION_DIR', 'ZESHUN_MERCADO_SESSION_FILE',
                    'ZESHUN_MERCADO_NODE', 'ZESHUN_MERCADO_CACHE_DIR', 'ZESHUN_MERCADO_RENEW_SECONDS'):
            value = configured.get(key)
            if isinstance(value, str) and value.strip() and not env.get(key):
                env[key] = value
    env['BIT_RUNTIME_ROLE'] = 'server'
    env['BIT_SERVICE_MODE'] = 'web' if mode == 'web' else 'worker'
    env['BIT_WORKER_ISOLATION'] = '1'
    env['BIT_WORKER_GROUP'] = 'worker' if mode == 'web' else mode
    if mode != 'web':
        env['BIT_LISTEN_PORT'] = str(group_port(mode))
    env['PYTHONUNBUFFERED'] = '1'
    env['MYSQL_POOL_MAX_CONNECTIONS'] = env.get(f'BIT_{mode.upper()}_DB_CONNECTIONS', str(DEFAULT_CONNECTIONS[mode]))
    env['MYSQL_POOL_MIN_CACHED'] = '0'
    env['MYSQL_POOL_MAX_CACHED'] = str(min(2, int(env['MYSQL_POOL_MAX_CONNECTIONS'])))
    env['MYSQL_POOL_ACQUIRE_TIMEOUT'] = env.get(f'BIT_{mode.upper()}_DB_ACQUIRE_TIMEOUT', '5')
    if mode != 'web':
        env['BIT_WSGI_THREADS'] = env.get(f'BIT_{mode.upper()}_WSGI_THREADS', '4')
    limits = {
        'MERCADO_SYNC_STORE_BUDGET': '2', 'MERCADO_SYNC_ORDER_RESERVE': '1',
        'MERCADO_ORDER_STORE_WORKERS': '2', 'MERCADO_ORDER_PRINT_STORE_WORKERS': '2',
        'MERCADO_ORDER_STATUS_WORKERS': '2', 'MERCADO_API_BACKFILL_WORKERS': '2',
        'MERCADO_STORE_LINK_STORE_WORKERS': '1', 'MERCADO_STORE_LINK_DETAIL_WORKERS': '2',
        'MERCADO_PROFIT_REFRESH_WORKERS': '1',
        'MERCADO_INFRACTION_STORE_WORKERS': '1', 'MERCADO_INFRACTION_DETAIL_WORKERS': '2',
        'MERCADO_PROHIBITED_STORE_WORKERS': '1', 'MERCADO_PROHIBITED_DETAIL_WORKERS': '2',
    }
    for key, value in limits.items():
        env[key] = env.get(f'BIT_{mode.upper()}_{key}', value)
    return env


def main():
    modes = (*GROUPS, 'web')
    environments = {mode: child_environment(mode) for mode in modes}
    validate_topology(environments)
    lock = InterProcessLock('workbench-services-supervisor', owner='workbench_services')
    if not lock.acquire(timeout=0):
        raise SystemExit('服务管理进程已运行')
    stop = threading.Event()
    def stopping(signum, frame):
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stopping)
    log_dir = Path(os.environ.get('BIT_SERVICE_LOG_DIR', str(Path(__file__).resolve().parents[1] / '.logs/services')))
    log_dir.mkdir(parents=True, exist_ok=True)
    children = {}
    attempts = {mode: 0 for mode in modes}
    started_at = {}
    retry_at = {mode: 0 for mode in modes}
    try:
        while not stop.is_set():
            for mode in modes:
                child = children.get(mode)
                if child is not None and child.poll() is not None:
                    lifetime = time.monotonic() - started_at[mode]
                    attempts[mode] = 1 if lifetime > 60 else attempts[mode] + 1
                    delay = min(60, 2 ** min(attempts[mode], 6))
                    print(f'{mode} 服务退出，{delay} 秒后独立重启', flush=True)
                    retry_at[mode] = time.monotonic() + delay
                    del children[mode]
                if mode not in children and time.monotonic() >= retry_at[mode]:
                    # Separate logs make pool saturation and task failures attributable.
                    with (log_dir / f'{mode}.log').open('ab') as output:
                        children[mode] = subprocess.Popen(
                            [sys.executable, '-m', 'bit.workbench_services', '--child'],
                            env=environments[mode], stdout=output, stderr=subprocess.STDOUT,
                        )
                    started_at[mode] = time.monotonic()
                    print(f'{mode} 服务启动 pid={children[mode].pid} db_limit={environments[mode]["MYSQL_POOL_MAX_CONNECTIONS"]} log={log_dir / (mode + ".log")}', flush=True)
            stop.wait(1)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        shutdown_deadline = time.monotonic() + 20
        for child in children.values():
            try:
                child.wait(timeout=max(0, shutdown_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        lock.release()


if __name__ == '__main__':
    if sys.argv[1:] == ['--child']:
        # Canonical import keeps task state shared with bit_db_api callbacks.
        from bit.bit_interface import run_interface_main
        raise SystemExit(0 if run_interface_main() else 1)
    if sys.argv[1:]:
        raise SystemExit('Usage: python -m bit.workbench_services')
    main()
