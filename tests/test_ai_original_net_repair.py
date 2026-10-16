import json
from copy import deepcopy
from decimal import Decimal

import pytest

from scripts.repair_ai_original_net_proceeds import missing_net_proceeds, repair


def product(**changes):
    row = {
        "id": 1, "source_type": "ai_original", "net_proceeds_usd": None,
        "source_snapshot_json": json.dumps({"original_1688": {
            "price": 15, "variations": [{"price": 20}],
        }}),
    }
    row.update(changes)
    return row


def test_repair_uses_captured_highest_price_and_preserves_original_row():
    row = product()
    original = deepcopy(row)
    assert missing_net_proceeds(row) == 4
    assert row == original


@pytest.mark.parametrize("changes", [
    {"source_type": "zying"}, {"net_proceeds_usd": 9}, {"net_proceeds_usd": 0},
    {"source_snapshot_json": "{}"},
    {"source_snapshot_json": json.dumps({"original_1688": {"price": 20},
                                       "source_1688_overrides": {"net_proceeds_usd": None}})},
])
def test_repair_skips_existing_net_and_explicit_manual_clearing(changes):
    assert missing_net_proceeds(product(**changes)) is None


def test_repair_uses_manual_purchase_price_instead_of_captured_skus():
    assert missing_net_proceeds(product(source_snapshot_json=json.dumps({
        "original_1688": {"variations": [{"price": 100}]},
        "source_1688_overrides": {"price": 8.4},
    }))) == 2


def test_repair_respects_saved_manual_net_and_does_not_use_usd_sale_price():
    assert missing_net_proceeds(product(source_snapshot_json=json.dumps({
        "original_1688": {"price": 20},
        "source_1688_overrides": {"net_proceeds_usd": 7.5},
    }))) == 7.5
    assert missing_net_proceeds(product(source_snapshot_json="{}", price=100, currency_id="USD")) is None


class Connection:
    def __init__(self, *, mismatch=False):
        self.queries = []
        self.rowcount = 1
        self.committed = self.rolled_back = False
        self.mismatch = mismatch

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchall(self):
        return [product()]

    def fetchone(self):
        return {"net_proceeds_usd": 5 if self.mismatch else 4}

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_repair_defaults_to_preview_and_apply_checks_persisted_value():
    preview = Connection()
    assert repair(preview)["changes"] == [{"id": 1, "before": None, "net_proceeds_usd": 4}]
    assert preview.rolled_back and not preview.committed
    assert all(not sql.startswith("UPDATE") for sql, _ in preview.queries)
    applied = Connection()
    assert repair(applied, apply=True)["applied"]
    assert applied.committed and not applied.rolled_back
    assert "FOR UPDATE" in applied.queries[0][0]
    assert "net_proceeds_usd IS NULL" in applied.queries[1][0]
    assert "review_status='unreviewed'" in applied.queries[1][0]


def test_repair_rolls_back_if_net_readback_differs():
    connection = Connection(mismatch=True)
    with pytest.raises(RuntimeError, match="回读不一致"):
        repair(connection, apply=True)
    assert connection.rolled_back and not connection.committed


def test_repair_accepts_decimal_readback_of_manual_fractional_amount():
    connection = Connection()
    connection.fetchall = lambda: [product(source_snapshot_json=json.dumps({
        "source_1688_overrides": {"net_proceeds_usd": 17.1},
    }))]
    connection.fetchone = lambda: {"net_proceeds_usd": Decimal("17.1000")}
    assert repair(connection, apply=True)["changes"][0]["net_proceeds_usd"] == 17.1
    assert connection.committed and not connection.rolled_back
