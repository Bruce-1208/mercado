import pymysql
import pytest

from bit import bit_mysql


class Cursor:
    def __init__(self, database="test", exists=True):
        self.connection = pymysql.connections.Connection(database=database, defer_connect=True)
        self.exists = exists
        self.calls = []
        self.error = False

    def execute(self, sql, params=None):
        self.calls.append(sql)
        if self.error:
            raise RuntimeError("database unavailable")

    def fetchone(self):
        return {"Field": "enabled"} if self.exists else None


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.setattr(bit_mysql, "_column_check_cache", {})
    monkeypatch.setenv("MYSQL_SCHEMA_CHECK_TTL", "60")


def ensure(cursor):
    return bit_mysql._ensure_column(cursor, "stores", "enabled", "INT")


def test_column_checks_are_cached_across_connections_but_not_databases(monkeypatch):
    now = [100]
    monkeypatch.setattr(bit_mysql.time, "monotonic", lambda: now[0])
    first, second, other = Cursor(), Cursor(), Cursor("other")
    ensure(first)
    ensure(second)
    ensure(other)
    assert len(first.calls) == 1
    assert not second.calls
    assert len(other.calls) == 1
    now[0] = 161
    ensure(second)
    assert len(second.calls) == 1


def test_failed_check_is_retried_and_successful_ddl_is_cached():
    cursor = Cursor(exists=False)
    cursor.error = True
    with pytest.raises(RuntimeError):
        ensure(cursor)
    cursor.error = False
    assert ensure(cursor) is True
    assert ensure(cursor) is False
    assert sum(sql.startswith("ALTER") for sql in cursor.calls) == 1


def test_cache_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MYSQL_SCHEMA_CHECK_TTL", "0")
    cursor = Cursor()
    ensure(cursor)
    ensure(cursor)
    assert len(cursor.calls) == 2
