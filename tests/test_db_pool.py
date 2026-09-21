from bit import db_pool


def test_pool_reuses_one_bounded_pool_for_matching_config(monkeypatch):
    created = []
    logical_connections = []

    class FakePool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        def connection(self, *, shareable):
            connection = object()
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
    db_pool.close_all_pools()
    assert created[0].closed is True


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
