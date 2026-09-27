from unittest.mock import MagicMock, patch

import pytest

from bit import bit_mysql
from bit.bit_mysql import insert_orders


def _order_row(order_id, order_number):
    return [order_id, order_number, *([None] * 21)]


def test_insert_orders_deduplicates_and_upserts_order_ids():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    old_row = _order_row(137492285, "old")
    new_row = _order_row(137492285, "new")
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
    cursor.fetchall.side_effect = [
        [{"token_id": 7, "shipping_id": "ship-1"}],
        [{
            "order_id": "101", "token_id": 7, "shipping_id": "ship-1",
            "site_id": "MLM", "total_amount": "350", "product_id": "MLM-A",
            "raw_json": {"order_items": [{
                "item": {"id": "MLM-A"}, "quantity": 1,
            }]},
        }],
        [{"token_id": 7, "item_id": "MLM-A", "weight_g": "200"}],
    ]
    with (
        patch("bit.bit_mysql.pymysql.connect", return_value=connection),
        patch("bit.bit_mysql._ensure_mercado_synced_orders_table"),
        patch("erp.mercadolibre_store_link_store.ensure_store_link_table"),
        patch(
            "erp.mercadolibre_shipping_rate_cards.OfficialShippingRateCardStore.list_rates",
            return_value={"rows": [{
                "site_id": "MLM", "rate_kind": "above_threshold",
                "price_min_local": 299, "price_max_local": None,
                "weight_min_g": 0, "weight_max_g": 300,
                "shipping_amount_usd": 4.6,
            }]},
        ),
    ):
        result = bit_mysql.refresh_mercado_order_quoted_freight()

    candidate_sql = cursor.execute.call_args_list[0].args[0]
    assert "freight_source" not in candidate_sql
    assert result["quoted_shipments"] == 1
    sql, updates = cursor.executemany.call_args.args
    assert float(updates[0][0]) == 4.6
    assert updates[0][2] == "official_weight_rate_card"
    assert "`freight` =" not in sql
    assert "`freight_source` =" not in sql


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
