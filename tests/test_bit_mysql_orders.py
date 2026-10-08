import sqlite3
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from bit import bit_mysql
from bit.bit_mysql import insert_orders


def test_zying_identifier_query_keeps_scope_dates_and_large_pages():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {"total": 2001}
    cursor.fetchall.return_value = [{"id": "2001", "order_number": "pack1", "pack_id": "pack1"}]
    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
        patch("bit.bit_mysql._ensure_mercado_store_tokens_table"),
        patch("bit.bit_mysql._ensure_mercado_store_site_settings_table"),
    ):
        result = bit_mysql.list_orders(start_date="2026-10-01", end_date="2026-10-08",
            store_ids=[10], allowed_store_ids=[10, 20], salesperson="业务员甲",
            page=2, page_size=2000, sync_identifiers_only=True, include_summary=False)
    assert result["total"] == 2001 and result["page_size"] == 2000
    count, query = cursor.execute.call_args_list
    sql, params = query.args
    assert params == ["2026-09-30 16:00:00", "2026-10-08 16:00:00", 10, 10, 20, "业务员甲", 2000, 2000]
    assert "INNER JOIN `mercado_store_tokens`" in sql
    assert sql.count("synced.`token_id` IN") == 2
    assert "site_settings.`salesperson` IN" in sql
    assert "yandex:%%" in sql
    assert "ORDER BY synced.`date_created` DESC, synced.`order_id` DESC" in sql
    assert all(word not in sql for word in ("exchange_rate", "TEMPORARY", "shipment_costs", "store_links"))
    connection.close.assert_called_once()


def test_zying_identifier_query_fails_closed_and_rejects_unsupported_filters():
    with patch("bit.bit_mysql.pymysql.connect") as connect:
        assert bit_mysql.list_orders(sync_identifiers_only=True, allowed_store_ids=[])["rows"] == []
        with pytest.raises(ValueError, match="仅支持"):
            bit_mysql.list_orders(sync_identifiers_only=True, country="墨西哥")
        connect.assert_not_called()


def _order_row(order_id, order_number):
    return [order_id, order_number, *([None] * 21)]


def test_insert_orders_deduplicates_and_upserts_sales_numbers():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    old_row = _order_row(137492285, "sale-1")
    new_row = _order_row(137492286, "sale-1")
    other_row = _order_row(200, "other")

    with patch("bit.bit_mysql.pymysql.connect", return_value=connection):
        result = insert_orders([old_row, new_row, other_row])

    sql, written_rows = cursor.executemany.call_args.args
    assert result == 2
    assert written_rows == [new_row, other_row]
    assert "ON DUPLICATE KEY UPDATE" in sql
    connection.commit.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_insert_orders_rolls_back_and_reraises_database_errors():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.executemany.side_effect = RuntimeError("database error")

    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        pytest.raises(RuntimeError, match="database error"),
    ):
        insert_orders([_order_row(200, "order")])

    connection.rollback.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_freight_quote_is_saved_before_actual_shipment_costs_exist():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{
        "token_id": 7, "shipping_id": "ship-1", "seller_cost": Decimal("4.6"),
        "currency_id": "USD", "payload_json": '{"senders":[{"cost":4.6}]}',
        "checked_at": "2026-10-07 10:00:00", "shipment_status": "ready_to_ship",
    }]
    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
        patch(
            "erp.mercadolibre_shipping_rate_cards.OfficialShippingRateCardStore.list_rates",
            side_effect=AssertionError("订单标价应读取 API 缓存，不能读取重量费率表"),
        ),
        patch(
            "bit.bit_mysql.save_mercado_shipment_costs",
            return_value={"shipments": 1, "orders": 1},
        ) as save_costs,
        patch(
            "bit.bit_mysql._build_mercado_order_weight_quote",
            side_effect=AssertionError("订单标价不能按商品重量计算"),
        ),
    ):
        result = bit_mysql.refresh_mercado_order_quoted_freight()

    candidate_sql = cursor.execute.call_args_list[0].args[0]
    assert "mercado_shipment_costs" in candidate_sql
    assert "costs.`seller_cost` IS NOT NULL" in candidate_sql
    assert result["quoted_shipments"] == 1
    assert result["updated_orders"] == 1
    token_id, entries = save_costs.call_args.args
    assert token_id == 7
    assert entries[0]["seller_cost"] == Decimal("4.6")
    assert entries[0]["payload"] == {"senders": [{"cost": 4.6}]}
    assert entries[0]["from_cache"] is True
    assert entries[0]["update_actual"] is False


def _shipment_order(order_id="101", *, status="ready_to_ship", weight="350", **extra):
    return {
        "order_id": order_id, "shipping_id": "ship-1", "status": status,
        "workflow_status": "", "status_detail": "{}", "raw_json": "{}",
        "weight": weight, "quoted_freight_source": "", "quoted_freight_local": None,
        "quoted_freight_usd": None, **extra,
    }


def _save_shipment_costs(orders, *, cost="4.6", currency="USD", **entry_options):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = orders
    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
    ):
        result = bit_mysql.save_mercado_shipment_costs(7, [{
            "shipping_id": "ship-1", "seller_cost": cost, "currency_id": currency,
            "payload": {"senders": [{"cost": cost}]},
            "checked_at": "2026-10-07 10:00:00", **entry_options,
        }])
    connection.commit.assert_called_once_with()
    connection.close.assert_called_once_with()
    return result, cursor


def _cost_updates(cursor, field):
    return [
        row
        for call in cursor.executemany.call_args_list
        if "UPDATE `mercado_synced_orders`" in call.args[0]
        and f"SET `{field}` =" in call.args[0]
        for row in call.args[1]
    ]


@pytest.mark.parametrize("cost", ["4.6", "0"])
def test_unshipped_api_cost_is_a_quote_and_actual_freight_is_empty(cost):
    result, cursor = _save_shipment_costs([_shipment_order()], cost=cost)

    quote = _cost_updates(cursor, "quoted_freight_usd")
    actual = _cost_updates(cursor, "freight")
    assert quote == [(Decimal(cost), Decimal(cost), "USD", "2026-10-07 10:00:00", "101", 7)]
    assert actual == [(None, "", "", None, "101", 7)]
    assert result == {"shipments": 1, "orders": 1}


def test_api_quote_keeps_its_original_currency():
    _result, cursor = _save_shipment_costs([_shipment_order()], cost="31.99", currency="brl")

    assert _cost_updates(cursor, "quoted_freight_usd") == [
        (None, Decimal("31.99"), "BRL", "2026-10-07 10:00:00", "101", 7),
    ]
    assert _cost_updates(cursor, "freight")[0][0] is None


@pytest.mark.parametrize("status", ["shipped", "delivered", "not_delivered"])
def test_shipped_api_cost_updates_actual_and_preserves_the_api_quote(status):
    result, cursor = _save_shipment_costs([_shipment_order(
        status=status, quoted_freight_source="shipment_costs",
        quoted_freight_local=Decimal("4.6"), quoted_freight_usd=Decimal("4.6"),
    )], cost="8.2", shipment_status=status)

    assert _cost_updates(cursor, "quoted_freight_usd") == []
    assert _cost_updates(cursor, "freight") == [
        (Decimal("8.2"), "USD", "shipment_costs", "2026-10-07 10:00:00", "101", 7),
    ]
    assert result == {"shipments": 1, "orders": 1}


@pytest.mark.parametrize("entry_options", [
    {"from_cache": True, "shipment_status": "ready_to_ship"},
    {"from_cache": True, "shipment_status": "", "error": "API request failed"},
    {"shipment_status": "", "error": "API request failed"},
])
def test_unshipped_cached_cost_is_not_promoted_to_actual_after_shipping(entry_options):
    _result, cursor = _save_shipment_costs([_shipment_order(
        status="shipped", quoted_freight_source="shipment_costs",
        quoted_freight_local=Decimal("4.6"), quoted_freight_usd=Decimal("4.6"),
    )], **entry_options)

    assert _cost_updates(cursor, "quoted_freight_usd") == []
    assert _cost_updates(cursor, "freight") == []


def test_failed_api_response_does_not_write_quote_or_actual():
    result, cursor = _save_shipment_costs(
        [_shipment_order(status="shipped")], cost=None, error="API request failed",
    )

    assert _cost_updates(cursor, "quoted_freight_usd") == []
    assert _cost_updates(cursor, "freight") == []
    assert result == {"shipments": 1, "orders": 0}


@pytest.mark.parametrize("status", ["ready_to_ship", "shipped"])
def test_merged_order_freight_shares_preserve_the_api_shipment_total(status):
    _result, cursor = _save_shipment_costs([
        _shipment_order("101", status=status, weight="100"),
        _shipment_order("102", status=status, weight="200"),
        _shipment_order("103", status=status, weight="300"),
    ], cost="6.71", shipment_status=status)

    quote = _cost_updates(cursor, "quoted_freight_usd")
    assert sum((row[1] for row in quote), Decimal("0")) == Decimal("6.71")
    assert len(quote) == 3
    assert all(row[1] >= 0 for row in quote)
    actual = _cost_updates(cursor, "freight")
    if status == "shipped":
        assert sum((row[0] for row in actual), Decimal("0")) == Decimal("6.71")
    else:
        assert all(row[0] is None for row in actual)


def test_fresh_merged_shipment_cost_marks_dispatch_without_exposing_unshipped_order_actual():
    result, cursor = _save_shipment_costs([
        _shipment_order("101", status="shipped", weight="100"),
        _shipment_order("102", status="ready_to_ship", weight="100"),
    ], cost="6.71")

    cached = next(
        call.args[1][0] for call in cursor.executemany.call_args_list
        if "INSERT INTO `mercado_shipment_costs`" in call.args[0]
    )
    assert cached[-1] == "shipped"
    actual = {row[-2]: row[0] for row in _cost_updates(cursor, "freight")}
    assert actual == {"101": Decimal("3.3550"), "102": None}
    quote = _cost_updates(cursor, "quoted_freight_usd")
    assert sum((row[1] for row in quote), Decimal("0")) == Decimal("6.71")
    assert result == {"shipments": 1, "orders": 2}


@pytest.mark.parametrize(
    "query_function",
    [
        bit_mysql.list_mercado_pending_shipment_cost_rows,
        bit_mysql.list_mercado_pending_order_image_rows,
    ],
)
def test_historical_backfill_queries_exclude_disabled_stores(query_function):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []

    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
        patch("bit.bit_mysql._ensure_mercado_store_tokens_table"),
    ):
        query_function()

    sql = cursor.execute.call_args.args[0]
    assert "mercado_store_tokens" in sql
    assert "token.`enabled` = 1" in sql


def test_purchase_tracking_order_lookup_reads_selected_orders_in_one_query():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {"order_id": "20002", "purchase_order": "TB-2"},
        {"order_id": "20001", "purchase_order": "1688-1"},
    ]

    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
        patch("bit.bit_mysql._ensure_mercado_store_tokens_table"),
    ):
        result = bit_mysql.get_mercado_purchase_tracking_orders(
            ["20001", "20002", "missing"]
        )

    assert result == [
        {"order_id": "20001", "purchase_order": "1688-1"},
        {"order_id": "20002", "purchase_order": "TB-2"},
    ]
    sql, params = cursor.execute.call_args.args
    assert "mercado_store_tokens" in sql
    assert params == ["20001", "20002", "missing"]
    connection.close.assert_called_once_with()


def test_order_details_are_limited_before_expensive_joins():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [{"total": 1000}, {"amount": 42}]
    cursor.fetchall.return_value = []
    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
        patch("bit.bit_mysql._ensure_mercado_store_tokens_table"),
        patch("bit.bit_mysql._ensure_mercado_store_site_settings_table"),
        patch("erp.mercadolibre_profitability_cache.ensure_profitability_cache_tables"),
        patch("erp.mercadolibre_store_link_store.ensure_store_link_table"),
    ):
        result = bit_mysql.list_orders(page=3, page_size=50, country="墨西哥")

    queries = [call.args for call in cursor.execute.call_args_list]
    page_query = next(args for args in queries if args[0].startswith(
        "CREATE TEMPORARY TABLE `tmp_mercado_order_page` AS"
    ))
    assert page_query[1] == ["墨西哥", 50, 100]
    assert "FROM `tmp_mercado_order_filter_source`" in page_query[0]
    assert "ORDER BY scoped.`ordered_at` DESC" in page_query[0]
    assert page_query[0].endswith("scoped.`id` DESC LIMIT %s OFFSET %s")
    detail_query = next(args[0] for args in queries if args[0].startswith("SELECT order_source.*"))
    assert "FROM `tmp_mercado_order_page` AS selected_page" in detail_query
    assert "STRAIGHT_JOIN `mercado_synced_orders` AS synced" in detail_query
    rate_query = next(args[0] for args in queries if "CREATE TEMPORARY TABLE `tmp_mercado_order_rate_keys` AS" in args[0])
    assert "FROM `tmp_mercado_order_filter_source`" in rate_query
    assert result["total"] == 1000
    assert result["summary"]["amount"] == 42
    assert result["pages"] == 20
    connection.close.assert_called_once_with()


def test_order_detail_and_summary_sources_hide_unshipped_actual_and_weight_quotes():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [{"total": 0}, {}]
    cursor.fetchall.return_value = []
    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
        patch("bit.bit_mysql._ensure_mercado_store_tokens_table"),
        patch("bit.bit_mysql._ensure_mercado_store_site_settings_table"),
        patch("erp.mercadolibre_profitability_cache.ensure_profitability_cache_tables"),
        patch("erp.mercadolibre_store_link_store.ensure_store_link_table"),
    ):
        bit_mysql.list_orders(sort_by="balance")

    queries = [call.args[0] for call in cursor.execute.call_args_list]
    source_queries = [
        next(sql for sql in queries if sql.startswith("SELECT order_source.*")),
        next(sql for sql in queries if sql.startswith(
            "CREATE TEMPORARY TABLE `tmp_mercado_order_filter_source` AS"
        )),
    ]
    # These scalar expressions use the same SQL syntax in MySQL and SQLite.
    # Evaluate the actual emitted conditions to cover both detail and summary rows.
    with sqlite3.connect(":memory:") as database:
        database.create_function("JSON_UNQUOTE", 1, lambda value: value)
        database.execute("""
            CREATE TABLE synced (
                status TEXT, workflow_status TEXT, status_detail TEXT, raw_json TEXT,
                freight_source TEXT, freight REAL, quoted_freight_source TEXT,
                quoted_freight_local REAL, quoted_freight_usd REAL,
                shipping_id TEXT, token_id INTEGER,
                site_id TEXT, total_amount REAL, sale_fee REAL,
                currency_id TEXT, amount_currency_id TEXT, paid_amount REAL,
                freight_currency_id TEXT, quoted_freight_currency_id TEXT
            )
        """)
        database.execute("""
            CREATE TABLE mercado_shipment_costs (
                shipping_id TEXT, token_id INTEGER, shipment_status TEXT
            )
        """)
        database.execute("CREATE TABLE daily_rate (rate REAL)")
        database.execute("INSERT INTO daily_rate VALUES (NULL)")
        database.execute("CREATE TABLE current_rate (rate REAL)")
        database.execute("INSERT INTO current_rate VALUES (NULL)")
        scenarios = [
            ("ready_to_ship", "", "{}", "shipment_costs", "shipment_costs", 4.6, "ready_to_ship", None, 4.6),
            ("paid", "", "{}", "shipment_costs", "shipment_costs", 0, "ready_to_ship", None, 0),
            ("paid", "", "", "shipment_costs", "shipment_costs", 4.6, "ready_to_ship", None, 4.6),
            ("paid", "", "legacy status", "shipment_costs", "shipment_costs", 4.6, "ready_to_ship", None, 4.6),
            ("shipped", "", "{}", "shipment_costs", "shipment_costs", 4.6, "shipped", 8.2, 4.6),
            ("paid", "已发-在途中", "{}", "shipment_costs", "shipment_costs", 4.6, "shipped", 8.2, 4.6),
            ("cancelled", "", '{"shipment_status":"shipped"}', "shipment_costs", "shipment_costs", 4.6, "shipped", 8.2, 4.6),
            ("shipped", "", "{}", "order_shipping_cost", "order_shipping_cost", 4.6, "shipped", None, 4.6),
            ("shipped", "", "{}", "shipment_costs", "official_weight_rate_card", 4.6, "shipped", 8.2, None),
            # Legacy rows already have freight, but it was fetched before dispatch.
            ("shipped", "", "{}", "shipment_costs", "shipment_costs", 4.6, "ready_to_ship", None, 4.6),
            ("shipped", "", "{}", "shipment_costs", "shipment_costs", 4.6, None, None, 4.6),
            ("ready_to_ship", "", "{}", "shipment_costs", "shipment_costs", None, "ready_to_ship", None, None),
        ]
        for sql in source_queries:
            assert "'$.shipping_cost'" not in sql
            expressions = []
            for name in ("freight_local", "quoted_freight_local"):
                suffix = f" AS `{name}`,"
                expressions.append(next(
                    line.strip().removesuffix(suffix)
                    for line in sql.splitlines() if line.strip().endswith(suffix)
                ))
            for status, workflow, detail, actual_source, quote_source, quote, cache_stage, expected_actual, expected_quote in scenarios:
                database.execute("DELETE FROM synced")
                database.execute("DELETE FROM mercado_shipment_costs")
                database.execute("INSERT INTO synced VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                    status, workflow, detail, '{"shipping_cost":999}',
                    actual_source, 8.2, quote_source, quote, quote, "ship-1", 7,
                    "MLM", 20, 3, "USD", "USD", 20, "USD", "USD",
                ))
                database.execute("INSERT INTO mercado_shipment_costs VALUES (?, ?, ?)", (
                    "ship-1", 7, cache_stage,
                ))
                actual, quoted = database.execute(
                    f"SELECT {expressions[0]}, {expressions[1]} FROM synced "
                    "LEFT JOIN mercado_shipment_costs AS actual_costs "
                    "ON actual_costs.shipping_id = synced.shipping_id "
                    "AND actual_costs.token_id = synced.token_id "
                    "AND actual_costs.shipment_status IN "
                    "('shipped','delivered','not_delivered','returning','returned')"
                ).fetchone()
                assert actual == expected_actual
                assert quoted == expected_quote
                if sql.startswith("SELECT order_source.*"):
                    suffix = " AS `balance`,"
                    balance_expression = next(
                        line.strip().removesuffix(suffix)
                        for line in sql.splitlines() if line.strip().endswith(suffix)
                    )
                    balance = database.execute(
                        f"SELECT {balance_expression} FROM synced "
                        "LEFT JOIN mercado_shipment_costs AS actual_costs "
                        "ON actual_costs.shipping_id = synced.shipping_id "
                        "AND actual_costs.token_id = synced.token_id "
                        "AND actual_costs.shipment_status IN "
                        "('shipped','delivered','not_delivered','returning','returned') "
                        "CROSS JOIN daily_rate CROSS JOIN current_rate"
                    ).fetchone()[0]
                    deduction = expected_actual if expected_actual is not None else expected_quote
                    assert balance == (round(20 - 3 - deduction, 2) if deduction is not None else None)

    summary_sql = next(sql for sql in queries if sql.startswith("SELECT COALESCE(SUM(ROUND("))
    assert "scoped.`freight_local`" in summary_sql
    assert "shipping_cost" not in summary_sql
    # Aggregate and balance sorting must apply the same actual-then-quote policy.
    balance_aggregate = summary_sql.split(" AS `freight`, ")[1].split(" AS `balance`, ")[0]
    page_sql = next(sql for sql in queries if sql.startswith("CREATE TEMPORARY TABLE `tmp_mercado_order_page` AS"))
    sort_expression = page_sql.split("ORDER BY ", 1)[1].split(" DESC, ", 1)[0]
    with sqlite3.connect(":memory:") as database:
        database.execute("""CREATE TABLE scoped (
            data_origin TEXT, total_amount REAL, sale_fee REAL, currency_id TEXT,
            platform_currency_id TEXT, paid_amount REAL, freight_local REAL,
            freight_currency_id TEXT, quoted_freight_local REAL, quoted_freight_currency_id TEXT
        )""")
        database.execute("CREATE TABLE daily_rate (rate REAL)")
        database.execute("INSERT INTO daily_rate VALUES (NULL)")
        database.execute("CREATE TABLE current_rate (rate REAL)")
        database.execute("INSERT INTO current_rate VALUES (0.2)")
        for currency, actual, quote, expected in [
            ("USD", None, 4.6, 12.4), ("USD", 8.2, 4.6, 8.8),
            ("USD", 0, 4.6, 17), ("USD", None, 0, 17),
            ("USD", None, None, 0), ("MXN", None, 23, -3.6),
        ]:
            database.execute("DELETE FROM scoped")
            database.execute("INSERT INTO scoped VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("token", 20, 3, currency, currency, 20, actual, currency, quote, currency))
            balance = database.execute(
                f"SELECT {balance_aggregate} FROM scoped CROSS JOIN daily_rate CROSS JOIN current_rate"
            ).fetchone()[0]
            assert balance == expected
            sort_value = database.execute(
                f"SELECT {sort_expression} FROM scoped CROSS JOIN daily_rate CROSS JOIN current_rate"
            ).fetchone()[0]
            assert sort_value == (None if actual is None and quote is None else expected)


def test_order_number_migration_cleans_duplicates_and_adds_unique_key():
    cursor = MagicMock()
    cursor.fetchone.side_effect = [None, {"acquired": 1}, None, None]
    bit_mysql._ensure_orders_unique_number(cursor)
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("PARTITION BY (NULLIF(TRIM(`编号`), '') IS NULL)" in sql for sql in statements)
    assert any("GENERATED ALWAYS AS (NULLIF(TRIM(`编号`), '')) STORED" in sql for sql in statements)
    assert any("ADD UNIQUE KEY `uniq_orders_number` (`_order_number_key`)" in sql for sql in statements)
    assert "RELEASE_LOCK" in statements[-1]


def test_order_number_migration_is_skipped_when_unique_key_exists():
    cursor = MagicMock()
    cursor.fetchone.return_value = {"Key_name": "uniq_orders_number"}
    bit_mysql._ensure_orders_unique_number(cursor)
    assert cursor.execute.call_count == 1


def test_order_number_normalization_and_missing_number_fallback():
    assert bit_mysql._deduplicate_order_rows([
        _order_row("old", 123.0), _order_row("new", " 123 "),
        _order_row("fallback", None), _order_row("fallback", ""),
    ]) == [_order_row("new", "123"), _order_row("fallback", None)]


def test_reupload_updates_every_field_including_internal_id():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    with patch("bit.bit_mysql.pymysql.connect", return_value=connection):
        insert_orders([_order_row("new-internal-id", "same-sales-number")])
    sql = cursor.executemany.call_args.args[0].split("ON DUPLICATE KEY UPDATE")[1]
    for name in bit_mysql.ORDER_COLUMN_NAMES:
        assert f"`{name}` = VALUES(`{name}`)" in sql


@pytest.mark.parametrize('sort_by,freight_filter,expect_finance', [
    ('ordered_at', '', False), ('cost', '', False),
    ('profit', '', True), ('ordered_at', 'actual_higher', True),
])
def test_fast_order_page_preserves_financial_filters_and_defers_only_summary(sort_by, freight_filter, expect_finance):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'total': 123}
    cursor.fetchall.return_value = []
    with (
        patch('bit.bit_mysql.pymysql.connect', return_value=connection),
        patch('bit.bit_mysql._ensure_mercado_synced_orders_table'),
        patch('bit.bit_mysql._ensure_mercado_store_tokens_table'),
        patch('bit.bit_mysql._ensure_mercado_store_site_settings_table'),
        patch('erp.mercadolibre_profitability_cache.ensure_profitability_cache_tables'),
        patch('erp.mercadolibre_store_link_store.ensure_store_link_table'),
    ):
        result = bit_mysql.list_orders(include_summary=False, sort_by=sort_by,
            freight_variance=freight_filter, allowed_store_ids=[7], transfer_warehouse='shahu')
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    source = next(q for q in queries if q.startswith('CREATE TEMPORARY TABLE `tmp_mercado_order_filter_source`'))
    assert ('JOIN `mercado_shipment_costs`' in source) is expect_finance
    assert 'synced.`token_id` IN (%s)' in source
    assert 'synced.`transfer_warehouse` = %s' in source
    assert not any(q.startswith('SELECT COALESCE(SUM(ROUND(') for q in queries)
    assert any(q.startswith('SELECT order_source.*') and 'JOIN `mercado_shipment_costs`' in q for q in queries)
    assert result['total'] == 123
    assert result['page_size'] == 50
    assert result['summary'] is None and result['summary_pending'] is True
    connection.close.assert_called_once()
