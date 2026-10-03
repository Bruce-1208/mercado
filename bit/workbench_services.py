"""Supervise a public Web process and one loopback scheduler worker.

Run: python -m bit.workbench_services
Both children use the same checkout, secret file and persistent data paths.
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
    env['BIT_SERVICE_MODE'] = mode
    env['PYTHONUNBUFFERED'] = '1'
    pool_name = 'BIT_WEB_DB_CONNECTIONS' if mode == 'web' else 'BIT_WORKER_DB_CONNECTIONS'
    env['MYSQL_POOL_MAX_CONNECTIONS'] = env.get(pool_name, '6' if mode == 'web' else '8')
    if mode == 'worker':
        env['BIT_WSGI_THREADS'] = env.get('BIT_WORKER_WSGI_THREADS', '16')
    return env


def main():
    lock = InterProcessLock('workbench-services-supervisor', owner='workbench_services')
    if not lock.acquire(timeout=0):
        raise SystemExit('服务管理进程已运行')
    stop = threading.Event()
    def stopping(signum, frame):
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stopping)
    children = {}
    attempts = {'worker':0, 'web':0}
    started_at = {}
    retry_at = {'worker':0, 'web':0}
    try:
        while not stop.is_set():
            for mode in ('worker', 'web'):
                child = children.get(mode)
                if child is not None and child.poll() is not None:
                    lifetime = time.monotonic() - started_at[mode]
                    attempts[mode] = 1 if lifetime > 60 else attempts[mode] + 1
                    if attempts[mode] >= 5:
                        raise RuntimeError(f'{mode} 连续启动失败，请检查日志和已有服务')
                    retry_at[mode] = time.monotonic() + min(30, 2 ** attempts[mode])
                    del children[mode]
                if mode not in children and time.monotonic() >= retry_at[mode]:
                    children[mode] = subprocess.Popen(
                        [sys.executable, '-m', 'bit.workbench_services', '--child'],
                        env=child_environment(mode),
                    )
                    started_at[mode] = time.monotonic()
                    print(f'{mode} 服务启动 pid={children[mode].pid}', flush=True)
            stop.wait(1)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=20)
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
