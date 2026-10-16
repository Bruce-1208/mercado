from unittest.mock import MagicMock

import pytest

from bit.order_search_projection import EXPRESSION, inspect, pack_sql
from scripts.optimize_order_search_projection import optimize


def definition(**changes):
    return {"GENERATION_EXPRESSION": EXPRESSION, "DATA_TYPE": "longtext",
            "COLLATION_NAME": "utf8mb4_bin", "EXTRA": "STORED GENERATED", **changes}


@pytest.mark.parametrize("changes", [
    {"GENERATION_EXPRESSION": "raw_json"}, {"DATA_TYPE": "varchar"},
    {"GENERATION_EXPRESSION": EXPRESSION.replace("pack_id", "Pack_id")},
    {"GENERATION_EXPRESSION": EXPRESSION.replace("pack_id", "pa ck_id")},
    {"COLLATION_NAME": "utf8mb4_general_ci"}, {"EXTRA": "VIRTUAL GENERATED"},
])
def test_incompatible_projection_is_rejected(changes):
    cursor = MagicMock()
    cursor.fetchone.return_value = definition(**changes)
    with pytest.raises(ValueError, match="incompatible"):
        inspect(cursor)


def test_dry_run_and_database_guard_never_change_schema():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [{"db": "test_orders"}, None, None]
    assert optimize(connection, database="test_orders")["applied"] is False
    assert not any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list)
    cursor.fetchone.side_effect = [{"db": "unexpected_database"}]
    with pytest.raises(ValueError, match="must match"):
        optimize(connection, database="test_orders", apply=True)
    assert not any("ALTER TABLE" in call.args[0] for call in cursor.execute.call_args_list)


def test_non_mysql_cursor_uses_original_expression_without_ddl():
    cursor = MagicMock()
    assert pack_sql(cursor) == EXPRESSION.replace("`raw_json`", "synced.`raw_json`")
    cursor.execute.assert_not_called()
