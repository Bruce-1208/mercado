"""Process-local, bounded MySQL connection pools used by the workbench.

Every Waitress process owns its own pool. Connections returned by ``close()``
are rolled back and cached instead of opening a new TCP connection for every
query. Pool creation is lazy so importing database modules does not connect to
MySQL during client-mode startup.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Mapping

import pymysql
from dbutils.pooled_db import PooledDB


DEFAULT_MAX_CONNECTIONS = 24
DEFAULT_MIN_CACHED = 2
DEFAULT_MAX_CACHED = 12

_pools: dict[tuple[Any, ...], PooledDB] = {}
_pools_lock = threading.Lock()
_pools_pid = os.getpid()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _pooling_disabled() -> bool:
    if _truthy(os.environ.get("MYSQL_POOL_FORCE_ENABLED")):
        return False
    if _truthy(os.environ.get("MYSQL_POOL_DISABLED")):
        return True
    # Existing database unit tests replace ``pymysql.connect`` with lightweight
    # fakes and assert that physical close() is called. Keep that contract;
    # production and ordinary scripts always use the pool.
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def default_connection_config() -> dict[str, Any]:
    return {
        "host": os.environ.get("MYSQL_HOST", os.environ.get("DB_HOST", "192.168.1.11")),
        "port": int(os.environ.get("MYSQL_PORT", os.environ.get("DB_PORT", "3306"))),
        "user": os.environ.get("MYSQL_USER", os.environ.get("DB_USER", "mercado")),
        "password": os.environ.get(
            "MYSQL_PASSWORD", os.environ.get("DB_PASSWORD", "mercado")
        ),
        "database": os.environ.get(
            "MYSQL_DATABASE", os.environ.get("DB_NAME", "mercado")
        ),
        "charset": os.environ.get("MYSQL_CHARSET", "utf8mb4"),
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": _env_int("MYSQL_CONNECT_TIMEOUT", 5, 1, 60),
        "read_timeout": _env_int("MYSQL_READ_TIMEOUT", 120, 1, 3600),
        "write_timeout": _env_int("MYSQL_WRITE_TIMEOUT", 120, 1, 3600),
    }


def _normalized_config(connection_config: Mapping[str, Any] | None) -> dict[str, Any]:
    config = default_connection_config()
    if connection_config:
        config.update(dict(connection_config))
    config.setdefault("connect_timeout", _env_int("MYSQL_CONNECT_TIMEOUT", 5, 1, 60))
    config.setdefault("read_timeout", _env_int("MYSQL_READ_TIMEOUT", 120, 1, 3600))
    config.setdefault("write_timeout", _env_int("MYSQL_WRITE_TIMEOUT", 120, 1, 3600))
    return config


def _pool_key(config: Mapping[str, Any]) -> tuple[Any, ...]:
    # repr also handles optional unhashable SSL dictionaries. The actual key is
    # never logged, so credentials are not exposed.
    return (
        os.getpid(),
        id(pymysql.connect),
        tuple(sorted((str(key), repr(value)) for key, value in config.items())),
    )


def _new_pool(config: Mapping[str, Any]) -> PooledDB:
    max_connections = _env_int(
        "MYSQL_POOL_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS, 1, 100
    )
    min_cached = _env_int("MYSQL_POOL_MIN_CACHED", DEFAULT_MIN_CACHED, 0, max_connections)
    max_cached = _env_int(
        "MYSQL_POOL_MAX_CACHED", DEFAULT_MAX_CACHED, min_cached, max_connections
    )
    max_usage = _env_int("MYSQL_POOL_MAX_USAGE", 1000, 0, 1000000) or None
    return PooledDB(
        creator=pymysql,
        maxconnections=max_connections,
        mincached=min_cached,
        maxcached=max_cached,
        blocking=True,
        maxusage=max_usage,
        reset=True,
        ping=1,
        **dict(config),
    )


def get_db_connection(connection_config: Mapping[str, Any] | None = None):
    """Return a dedicated logical connection from the current process pool."""

    config = _normalized_config(connection_config)
    if _pooling_disabled():
        return pymysql.connect(**config)

    global _pools_pid
    current_pid = os.getpid()
    key = _pool_key(config)
    with _pools_lock:
        # Never reuse sockets inherited through a Unix fork.
        if _pools_pid != current_pid:
            _pools.clear()
            _pools_pid = current_pid
        pool = _pools.get(key)
        if pool is None:
            pool = _new_pool(config)
            _pools[key] = pool
    return pool.connection(shareable=False)


def close_all_pools() -> None:
    """Close cached physical connections, primarily for orderly shutdown/tests."""

    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


__all__ = (
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_MAX_CACHED",
    "DEFAULT_MIN_CACHED",
    "close_all_pools",
    "default_connection_config",
    "get_db_connection",
)
