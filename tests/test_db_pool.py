from bit import db_pool


def test_pool_reuses_one_bounded_pool_for_matching_config(monkeypatch):
    created = []
    logical_connections = []

    class FakePool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self._connections = 0
            self._idle_cache = [object()] * kwargs["mincached"]
            self._maxconnections = kwargs["maxconnections"]
            created.append(self)

        def connection(self, *, shareable):
            connection = object()
            self._connections += 1
            logical_connections.append((connection, shareable))
            return connection

        def close(self):
            self.closed = True

    monkeypatch.setenv("MYSQL_POOL_FORCE_ENABLED", "1")
    monkeypatch.delenv("MYSQL_POOL_DISABLED", raising=False)
    monkeypatch.setenv("MYSQL_POOL_MAX_CONNECTIONS", "24")
    monkeypatch.setenv("MYSQL_POOL_MIN_CACHED", "2")
    monkeypatch.setenv("MYSQL_POOL_MAX_CACHED", "12")
    monkeypatch.setattr(db_pool, "PooledDB", FakePool)
    db_pool.close_all_pools()

    first = db_pool.get_db_connection({"host": "db", "database": "mercado"})
    second = db_pool.get_db_connection({"host": "db", "database": "mercado"})

    assert first is logical_connections[0][0]
    assert second is logical_connections[1][0]
    assert len(created) == 1
    assert created[0].kwargs["maxconnections"] == 24
    assert created[0].kwargs["mincached"] == 2
    assert created[0].kwargs["maxcached"] == 12
    assert [shareable for _, shareable in logical_connections] == [False, False]
    assert db_pool.pool_status() == {
        "pool_count": 1,
        "active": 2,
        "idle": 2,
        "physical": 4,
        "max_connections": 24,
        "pools": [{
            "active": 2,
            "idle": 2,
            "physical": 4,
            "max_connections": 24,
            "utilization_percent": 8.3,
        }],
    }
    db_pool.close_all_pools()
    assert created[0].closed is True
    assert db_pool.pool_status()["pool_count"] == 0


def test_default_pool_budget_is_conservative(monkeypatch):
    created = []

    class FakePool:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def connection(self, *, shareable):
            return object()

        def close(self):
            return None

    monkeypatch.setenv("MYSQL_POOL_FORCE_ENABLED", "1")
    for name in (
        "MYSQL_POOL_MAX_CONNECTIONS",
        "MYSQL_POOL_MIN_CACHED",
        "MYSQL_POOL_MAX_CACHED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(db_pool, "PooledDB", FakePool)
    db_pool.close_all_pools()

    db_pool.get_db_connection({"host": "db", "database": "mercado"})

    assert created[0]["maxconnections"] == 12
    assert created[0]["mincached"] == 0
    assert created[0]["maxcached"] == 4
    db_pool.close_all_pools()


def test_pool_capacity_warning_is_rate_limited(monkeypatch, caplog):
    class FakePool:
        def __init__(self, **kwargs):
            self._connections = 0
            self._idle_cache = []
            self._maxconnections = kwargs["maxconnections"]

        def connection(self, *, shareable):
            self._connections += 1
            return object()

        def close(self):
            return None

    monkeypatch.setenv("MYSQL_POOL_FORCE_ENABLED", "1")
    monkeypatch.setenv("MYSQL_POOL_MAX_CONNECTIONS", "2")
    monkeypatch.setenv("MYSQL_POOL_MIN_CACHED", "0")
    monkeypatch.setenv("MYSQL_POOL_MAX_CACHED", "1")
    monkeypatch.setenv("MYSQL_POOL_WARN_PERCENT", "50")
    monkeypatch.setattr(db_pool, "PooledDB", FakePool)
    db_pool.close_all_pools()

    db_pool.get_db_connection({"host": "db", "database": "mercado"})
    db_pool.get_db_connection({"host": "db", "database": "mercado"})

    warnings = [
        record for record in caplog.records
        if "MySQL 连接池容量已达" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "活跃 1，上限 2" in warnings[0].getMessage()
    db_pool.close_all_pools()


def test_pool_can_be_disabled_for_diagnostics(monkeypatch):
    captured = []
    expected = object()
    monkeypatch.delenv("MYSQL_POOL_FORCE_ENABLED", raising=False)
    monkeypatch.setenv("MYSQL_POOL_DISABLED", "1")
    monkeypatch.setattr(
        db_pool.pymysql,
        "connect",
        lambda **kwargs: captured.append(kwargs) or expected,
    )

    connection = db_pool.get_db_connection({"host": "db", "database": "mercado"})

    assert connection is expected
    assert captured[0]["host"] == "db"
    assert captured[0]["database"] == "mercado"
    assert captured[0]["connect_timeout"] == 5
