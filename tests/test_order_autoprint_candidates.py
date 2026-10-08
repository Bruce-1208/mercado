"""Execute candidate predicates offline, including old wallet schemas."""
import sqlite3

import pytest

from bit import bit_mysql


class CandidateCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.connection.queries.append(sql)
        if "information_schema.COLUMNS" in sql:
            self.rows = [
                {"table_name": table, "column_name": row["name"]}
                for table in params
                for row in self.connection.db.execute(f"PRAGMA table_info(`{table}`)")
            ]
            return
        self.rows = [
            dict(row)
            for row in self.connection.db.execute(sql.replace("%s", "?"), params)
        ]

    def fetchall(self):
        return self.rows


class CandidateDatabase:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.queries = []
        self.closed = False
        self.db.executescript("""
            CREATE TABLE mercado_synced_orders (
                order_id TEXT PRIMARY KEY, shipping_id TEXT, token_id INTEGER,
                shop_name TEXT, site_id TEXT, country TEXT, status TEXT,
                status_label TEXT, date_created TEXT
            );
            CREATE TABLE mercado_store_tokens (
                id INTEGER PRIMARY KEY, access_token TEXT, refresh_token TEXT,
                expires_at TEXT
            );
            CREATE TABLE mercado_order_operation_logs (
                order_id TEXT, action_type TEXT
            );
            INSERT INTO mercado_store_tokens VALUES (7, 'offline', '', NULL);
            INSERT INTO mercado_store_tokens VALUES (8, 'offline', '', NULL);
        """)

    def cursor(self):
        return CandidateCursor(self)

    def close(self):
        self.closed = True

    def wallet_tables(self, *, requests=True, details=True, printed_at=True):
        if requests:
            self.db.execute("CREATE TABLE workbench_print_requests (request_id TEXT, status TEXT)")
        if details:
            printed_column = ", printed_at TEXT" if printed_at else ""
            self.db.execute(
                "CREATE TABLE workbench_print_request_orders "
                f"(request_id TEXT, order_id TEXT{printed_column})"
            )

    def order(self, order_id, *, token_id=7, site_id="MLB", status="paid",
              shipping_id="shipment", date_created="2026-10-07 08:00:00"):
        self.db.execute(
            "INSERT INTO mercado_synced_orders VALUES (?, ?, ?, 'shop', ?, '', ?, '', ?)",
            (order_id, shipping_id, token_id, site_id, status, date_created),
        )

    def wallet_order(self, order_id, status, *, printed_at=None):
        request_id = "request-" + order_id
        self.db.execute("INSERT INTO workbench_print_requests VALUES (?, ?)", (request_id, status))
        self.db.execute("INSERT INTO workbench_print_request_orders VALUES (?, ?, ?)",
                        (request_id, order_id, printed_at))


@pytest.fixture
def candidates(monkeypatch):
    database = CandidateDatabase()
    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: database)
    monkeypatch.setattr(bit_mysql, "_ensure_mercado_synced_orders_table", lambda _cursor: None)
    monkeypatch.setattr(bit_mysql, "_ensure_mercado_store_tokens_table", lambda _cursor: None)
    yield database
    database.db.close()


def candidate_ids(**kwargs):
    return [row["order_id"] for row in bit_mysql.list_mercado_order_print_candidates(
        7, tracking_since="2026-10-05 08:00:00", **kwargs,
    )]


@pytest.mark.parametrize("include_previously_printed", [False, True])
def test_wallet_evidence_prevents_another_charge_when_operation_log_missing(
    candidates, include_previously_printed,
):
    candidates.wallet_tables()
    for order_id in ("printed", "pending", "refunded", "cancelled", "fresh", "legacy"):
        candidates.order(order_id)
    candidates.wallet_order("printed", "settled", printed_at="2026-10-07 08:01:00")
    candidates.wallet_order("pending", "reserved")
    candidates.wallet_order("refunded", "settled")
    candidates.wallet_order("cancelled", "cancelled")
    candidates.db.execute("INSERT INTO mercado_order_operation_logs VALUES ('legacy', 'label_printed')")

    expected = ["cancelled", "fresh", "refunded"]
    if include_previously_printed:
        expected.insert(2, "legacy")
    assert candidate_ids(include_previously_printed=include_previously_printed) == expected
    assert candidates.closed
    # Schema probing does not create or migrate any optional wallet tables.
    assert not any(sql.lstrip().startswith(("CREATE", "ALTER")) for sql in candidates.queries)


@pytest.mark.parametrize("requests,details", [(False, False), (True, False), (False, True)])
def test_legacy_installations_without_wallet_tables_remain_printable(candidates, requests, details):
    candidates.wallet_tables(requests=requests, details=details)
    candidates.order("fresh")
    assert candidate_ids() == ["fresh"]
    candidate_sql = candidates.queries[-1]
    if not details:
        assert "workbench_print_request_orders" not in candidate_sql
    if not requests:
        assert "workbench_print_requests" not in candidate_sql


def test_old_details_without_printed_at_still_protect_pending_requests(candidates):
    candidates.wallet_tables(printed_at=False)
    for order_id in ("fresh", "pending"):
        candidates.order(order_id)
    candidates.db.execute("INSERT INTO workbench_print_requests VALUES ('req', 'reserved')")
    candidates.db.execute("INSERT INTO workbench_print_request_orders VALUES ('req', 'pending')")
    assert candidate_ids(include_previously_printed=True) == ["fresh"]


def test_refunded_pending_request_becomes_retryable_after_cancellation(candidates):
    candidates.wallet_tables()
    candidates.order("retry")
    candidates.wallet_order("retry", "reserved")
    assert candidate_ids() == []
    candidates.db.execute("UPDATE workbench_print_requests SET status='cancelled'")
    assert candidate_ids() == ["retry"]


def test_filters_and_limit_apply_after_wallet_exclusions(candidates):
    candidates.wallet_tables()
    candidates.order("old", date_created="2026-10-05 07:59:59")
    candidates.order("future", date_created="2026-10-07 10:00:01")
    candidates.order("another-shop", token_id=8)
    candidates.order("another-site", site_id="MLM")
    candidates.order("unpaid", status="pending")
    candidates.order("no-shipment", shipping_id="")
    candidates.order("early-printed", date_created="2026-10-06 08:00:00")
    candidates.wallet_order("early-printed", "settled", printed_at="2026-10-06 08:01:00")
    candidates.order("first", status="ready_to_ship", date_created="2026-10-07 09:00:00")
    candidates.order("last", date_created="2026-10-07 10:00:00")
    assert candidate_ids(end_at="2026-10-07 10:00:00", site_ids=["mlb"], limit=1) == ["first"]
