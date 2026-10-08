"""Offline safety checks for the explicit store-link index maintenance command."""

import pytest

from scripts import optimize_store_link_indexes as maintenance


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, values=None):
        self.connection.calls.append((sql, values))
        if sql.startswith("SELECT DATABASE()"):
            self.rows = [{"database_name": self.connection.database, "server_version": self.connection.version}]
        elif "information_schema.TABLES" in sql:
            self.rows = [{"ENGINE": self.connection.engine, "TABLE_ROWS": 850000, "DATA_LENGTH": 123, "INDEX_LENGTH": 456}] if self.connection.table_exists else []
        elif "information_schema.COLUMNS" in sql:
            self.rows = [{"COLUMN_NAME": column} for column in self.connection.columns]
        elif "information_schema.STATISTICS" in sql:
            self.rows = [
                {"INDEX_NAME": name, "SEQ_IN_INDEX": n, "COLUMN_NAME": column,
                 "SUB_PART": None, "INDEX_TYPE": "BTREE", "IS_VISIBLE": "YES"}
                for name, columns in self.connection.indexes.items()
                for n, column in enumerate(columns, 1)
            ]
        elif sql.startswith("EXPLAIN "):
            self.rows = [{"key": maintenance.INDEX if maintenance.INDEX in self.connection.indexes else None, "rows": 123}]
        elif sql.startswith("SELECT `token_id`"):
            self.rows = [{"token_id": 7}]
        elif sql.startswith("ALTER TABLE"):
            if self.connection.ddl_error:
                raise self.connection.ddl_error
            if not self.connection.omit_created_index:
                self.connection.indexes[maintenance.INDEX] = maintenance.COLUMNS
            self.rows = []
        elif sql.startswith("SET SESSION lock_wait_timeout"):
            self.rows = []
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self):
        self.database = "mercado"
        self.version = "8.0.43"
        self.engine = "InnoDB"
        self.table_exists = True
        self.columns = (*maintenance.COLUMNS, "id")
        self.indexes = {}
        self.calls = []
        self.ddl_error = None
        self.omit_created_index = False
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


def test_default_inspects_and_explains_without_schema_changes():
    connection = Connection()
    report = maintenance.optimize(connection, expected_database="mercado", token_ids=(9, 7, 9))
    assert report["status"] == "planned"
    assert [plan["query"] for plan in report["explain_before"]] == ["sites", "summary", "categories"]
    assert all(sql.startswith(("SELECT ", "EXPLAIN ")) for sql, _values in connection.calls)
    explains = [(sql, values) for sql, values in connection.calls if sql.startswith("EXPLAIN ")]
    assert all(values == (7, 9) for _sql, values in explains)
    assert all("FORCE INDEX" not in sql for sql, _values in explains)


def test_apply_creates_one_online_index_and_verifies_definition():
    connection = Connection()
    emitted = []
    report = maintenance.optimize(connection, expected_database="mercado", apply=True, emit_plan=lambda plan: emitted.append(dict(plan)))
    ddls = [sql for sql, _values in connection.calls if sql.startswith("ALTER TABLE")]
    assert ddls == [maintenance.DDL]
    assert "ALGORITHM=INPLACE, LOCK=NONE" in ddls[0]
    assert report["status"] == "applied"
    assert emitted[0]["status"] == "planned"
    assert "explain_after" in report
    assert ("SET SESSION lock_wait_timeout=%s", (5,)) in connection.calls


def test_compatible_existing_index_is_skipped():
    connection = Connection()
    connection.indexes[maintenance.INDEX] = (*maintenance.COLUMNS, "id")
    report = maintenance.optimize(connection, expected_database="mercado", apply=True)
    assert report["status"] == "already_present"
    assert report["existing_index"] == maintenance.INDEX
    assert not any(sql.startswith(("ALTER ", "SET ")) for sql, _values in connection.calls)


def test_equivalent_index_under_other_name_is_refused_without_duplicate_ddl():
    connection = Connection()
    connection.indexes["equivalent_existing_index"] = maintenance.COLUMNS
    with pytest.raises(maintenance.MaintenanceError, match="will not add a duplicate"):
        maintenance.optimize(connection, expected_database="mercado", apply=True)
    assert not any(sql.startswith(("ALTER ", "SET ")) for sql, _values in connection.calls)


@pytest.mark.parametrize("invalid", ["invisible", "partial", "hash", "reordered"])
def test_invisible_partial_or_incompatible_indexes_are_not_accepted(invalid):
    index = {
        "name": maintenance.INDEX, "columns": list(maintenance.COLUMNS),
        "prefix_lengths": [None] * len(maintenance.COLUMNS),
        "type": "BTREE", "visible": True,
    }
    if invalid == "invisible":
        index["visible"] = False
    elif invalid == "partial":
        index["prefix_lengths"][-1] = 8
    elif invalid == "hash":
        index["type"] = "HASH"
    else:
        index["columns"][0], index["columns"][1] = index["columns"][1], index["columns"][0]
    with pytest.raises(maintenance.MaintenanceError, match="different or invisible"):
        maintenance._matching_index({maintenance.INDEX: index})


def test_incompatible_index_name_is_refused():
    connection = Connection()
    connection.indexes[maintenance.INDEX] = ("token_id", "is_current")
    with pytest.raises(maintenance.MaintenanceError, match="different or invisible"):
        maintenance.optimize(connection, expected_database="mercado", apply=True)
    assert not any(sql.startswith("ALTER ") for sql, _values in connection.calls)


def test_failed_online_ddl_does_not_retry_or_use_blocking_fallback():
    connection = Connection()
    connection.ddl_error = RuntimeError(1846, "LOCK=NONE is unsupported")
    with pytest.raises(RuntimeError):
        maintenance.optimize(connection, expected_database="mercado", apply=True)
    assert [sql for sql, _values in connection.calls if sql.startswith("ALTER ")] == [maintenance.DDL]


@pytest.mark.parametrize("change", ["wrong_database", "missing_table", "wrong_engine", "missing_column", "mariadb"])
def test_preflight_failures_never_run_ddl(change):
    connection = Connection()
    if change == "wrong_database":
        connection.database = "another_database"
    elif change == "missing_table":
        connection.table_exists = False
    elif change == "wrong_engine":
        connection.engine = "MyISAM"
    elif change == "missing_column":
        connection.columns = ("token_id", "id")
    else:
        connection.version = "10.11.6-MariaDB"
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.optimize(connection, expected_database="mercado", apply=True)
    assert not any(sql.startswith("ALTER ") for sql, _values in connection.calls)


def test_apply_must_confirm_created_index():
    connection = Connection()
    connection.omit_created_index = True
    with pytest.raises(maintenance.MaintenanceError, match="expected definition"):
        maintenance.optimize(connection, expected_database="mercado", apply=True)


def test_cli_suppresses_driver_secrets_and_closes_connection(monkeypatch, capsys):
    import pymysql
    from bit import db_pool

    connection = Connection()
    connection.ddl_error = RuntimeError(1846, "password=DO_NOT_PRINT user=PRIVATE_USER")
    monkeypatch.setattr(db_pool, "default_connection_config", lambda: {"database": "mercado", "password": "DO_NOT_PRINT"})
    monkeypatch.setattr(pymysql, "connect", lambda **_config: connection)
    assert maintenance.main(["--apply"]) == 1
    output = capsys.readouterr().out
    assert "DO_NOT_PRINT" not in output
    assert "PRIVATE_USER" not in output
    assert '"mysql_error_code": 1846' in output
    assert connection.closed
