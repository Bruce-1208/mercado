from unittest.mock import MagicMock, patch

import pytest

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
