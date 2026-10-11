"""Token reads in parallel communication requests must not run migrations."""
import pytest
from bit import bit_mysql


class Connection:
    def __init__(self, row, error=None):
        self.row, self.error = row, error
        self.statements = []
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        self.statements.append((sql, params))
        if self.error:
            error, self.error = self.error, None
            raise error

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


@pytest.mark.parametrize("row,include_disabled", [
    ({"id": 7, "enabled": 1}, False),
    ({"id": 7, "enabled": 0}, True),
    (None, False),
])
def test_token_read_does_no_schema_or_legacy_writes(monkeypatch, row, include_disabled):
    connection = Connection(row)
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kw: connection)
    def forbidden(*args):
        pytest.fail("a hot token read must not run schema migration")
    monkeypatch.setattr(bit_mysql, "_ensure_mercado_store_tokens_table", forbidden)
    monkeypatch.setattr(bit_mysql, "_ensure_mercado_store_site_settings_table", forbidden)
    assert bit_mysql.get_mercado_store_token(7, include_disabled) == row
    assert len(connection.statements) == 1
    assert connection.statements[0][0].startswith("SELECT")
    assert connection.closed


@pytest.mark.parametrize("code", [1146, 1142])
def test_only_missing_table_initializes_schema(monkeypatch, code):
    connection = Connection(None, bit_mysql.pymysql.err.ProgrammingError(code, "test"))
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kw: connection)
    initialized = []
    monkeypatch.setattr(bit_mysql, "_ensure_mercado_store_tokens_table", lambda cur: initialized.append(cur))
    if code == 1146:
        assert bit_mysql.get_mercado_store_token(7) is None
        assert initialized == [connection]
        assert len(connection.statements) == 2
    else:
        with pytest.raises(bit_mysql.pymysql.err.ProgrammingError):
            bit_mysql.get_mercado_store_token(7)
        assert not initialized
    assert connection.closed


def test_disabled_store_is_still_rejected_and_connection_released(monkeypatch):
    connection = Connection({"id": 7, "enabled": 0})
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kw: connection)
    with pytest.raises(ValueError, match="店铺已关闭"):
        bit_mysql.get_mercado_store_token(7)
    assert connection.closed
