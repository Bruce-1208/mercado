"""Offline reproduction: DBUtils retries a MySQL statement timeout three times.

No real database sockets are opened. Each fake execute raises the same PyMySQL
OperationalError(3024) as the workbench log, while recording pool occupancy.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pymysql
from dbutils.pooled_db import PooledDB


def run_case(*, failures=None, begin=False):
    events = []
    attempts = []
    pool = None
    next_connection_id = 0

    class Cursor:
        def __init__(self, connection_id):
            self.connection_id = connection_id

        def execute(self, sql, params=None):
            attempts.append({
                "attempt": len(attempts) + 1,
                "physical_connection_id": self.connection_id,
                "active_logical_pool_connections": pool._connections,
                "statement_budget_ms": 8000,
                "error_code": 3024,
            })
            raise pymysql.OperationalError(
                3024,
                "Query execution was interrupted, maximum statement execution time exceeded",
            )

        def close(self):
            events.append({"event": "cursor_close", "connection_id": self.connection_id})

    class Connection:
        def __init__(self, connection_id):
            self.connection_id = connection_id

        def cursor(self, *args, **kwargs):
            events.append({"event": "cursor_open", "connection_id": self.connection_id})
            return Cursor(self.connection_id)

        def ping(self, *args, **kwargs):
            pass

        def rollback(self):
            pass

        def commit(self):
            pass

        def begin(self):
            pass

        def close(self):
            events.append({"event": "physical_close", "connection_id": self.connection_id})

    def fake_connect(*args, **kwargs):
        nonlocal next_connection_id
        next_connection_id += 1
        events.append({"event": "physical_open", "connection_id": next_connection_id})
        return Connection(next_connection_id)

    driver = SimpleNamespace(
        connect=fake_connect,
        threadsafety=1,
        OperationalError=pymysql.OperationalError,
        InterfaceError=pymysql.InterfaceError,
        InternalError=pymysql.InternalError,
    )
    options = dict(
        creator=driver, maxconnections=6, mincached=0, maxcached=4,
        blocking=False, maxusage=1000, reset=True, ping=1,
    )
    if failures is not None:
        options["failures"] = failures
    pool = PooledDB(**options)
    connection = pool.connection(shareable=False)
    if begin:
        connection.begin()
    try:
        connection.cursor().execute("SELECT /*+ MAX_EXECUTION_TIME(8000) */ COUNT(*) FROM fake_products")
    except pymysql.OperationalError as error:
        observed_error = error.args[0]
    else:
        raise AssertionError("Expected the fake statement timeout")
    active_before_close = pool._connections
    connection.close()
    active_after_close = pool._connections
    pool.close()
    return {
        "network_connections_opened": 0,
        "observed_error_code": observed_error,
        "execute_attempts": attempts,
        "aggregate_statement_budget_ms": sum(item["statement_budget_ms"] for item in attempts),
        "active_logical_connections_before_close": active_before_close,
        "active_logical_connections_after_close": active_after_close,
        "events": events,
    }


results = {
    "default_pool_failures": run_case(),
    "exclude_operational_error_from_retry": run_case(
        failures=(pymysql.InterfaceError, pymysql.InternalError),
    ),
    "explicit_transaction": run_case(begin=True),
}
assert len(results["default_pool_failures"]["execute_attempts"]) == 3
assert results["default_pool_failures"]["aggregate_statement_budget_ms"] == 24000
assert all(item["active_logical_pool_connections"] == 1 for item in
           results["default_pool_failures"]["execute_attempts"])
assert results["default_pool_failures"]["active_logical_connections_before_close"] == 1
assert results["default_pool_failures"]["active_logical_connections_after_close"] == 0
assert len(results["exclude_operational_error_from_retry"]["execute_attempts"]) == 1
assert len(results["explicit_transaction"]["execute_attempts"]) == 1
print(json.dumps(results, ensure_ascii=False, indent=2))
