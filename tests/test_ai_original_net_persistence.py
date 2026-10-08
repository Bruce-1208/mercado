"""Defaults must be durable, while manual economics remain authoritative."""

from copy import deepcopy
from decimal import Decimal
import json
import re

import pytest

from erp import mercadolibre_collection_store as store


class ProductDB:
    def __init__(self, row=None):
        self.row = deepcopy(row)
        self.queries = []
        self.rowcount = 1
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        query = " ".join(query.split())
        self.queries.append((query, params))
        if query.startswith("INSERT"):
            columns, raw_values = re.search(
                r"\((.*?)\) VALUES \((.*?)\) ON DUPLICATE", query,
            ).groups()
            fields = re.findall(r"`([^`]+)`", columns)
            parameters = iter(params)
            inserted = {
                field: next(parameters) if token.strip() == "%s"
                else token.strip().strip("'")
                for field, token in zip(fields, raw_values.split(","))
            }
            if self.row is None:
                self.row = {"id": 7, **inserted}
            elif self.row.get("source_type") == "ai_original":
                previous = json.loads(self.row.get("source_snapshot_json") or "{}")
                if previous.get("ai_original", {}).get("status") != "completed":
                    self.row["source_snapshot_json"] = inserted["source_snapshot_json"]
                if self.row.get("net_proceeds_usd") is None:
                    self.row["net_proceeds_usd"] = inserted["net_proceeds_usd"]
                if self.row.get("currency_id") == "CNY":
                    self.row["price"] = inserted["price"]
        elif query.startswith("UPDATE") and self.row:
            assignments = re.findall(
                r"`([^`]+)` = (%s|IF\(`net_proceeds_usd` IS NULL, %s, `net_proceeds_usd`\))",
                query,
            )
            for (field, expression), value in zip(assignments, params):
                if expression == "%s" or self.row.get(field) is None:
                    self.row[field] = value

    def fetchone(self):
        return deepcopy(self.row)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_schema_ddl(monkeypatch):
    monkeypatch.setattr(store, "ensure_collection_tables", lambda cursor: None)


def source_product(**changes):
    return {
        "source_url": "https://detail.1688.com/offer/907173727091.html",
        "title": "收纳盒", "price": 12,
        "images": ["https://cbu01.alicdn.com/product.jpg"],
        "variations": [{"sku_id": "small", "price": 12},
                       {"sku_id": "large", "price": 25}],
        **changes,
    }


def existing_product(*, net=None, overrides=None, status="pending", **changes):
    snapshot = {
        "original_1688": {"price": 12, "variations": [{"price": 25}]},
        "ai_original": {"status": status},
    }
    if overrides is not None:
        snapshot["source_1688_overrides"] = overrides
    return {
        "id": 7, "source_type": "ai_original", "source_item_id": "1688907173727091",
        "currency_id": "CNY", "price": Decimal("12"), "weight_g": Decimal("250"),
        "net_proceeds_usd": net, "source_snapshot_json": json.dumps(snapshot), **changes,
    }


def generate(db, **changes):
    snapshot = json.loads(db.row["source_snapshot_json"])
    snapshot["ai_original"]["status"] = "completed"
    return store.update_ai_original_product(7, {
        "source_snapshot_json": snapshot, "currency_id": "USD", **changes,
    }, connection_factory=lambda: db)


@pytest.mark.parametrize("price,variations,expected", [
    (12, [{"price": 12}, {"price": 25}], 5),
    ("18.55", [], 4),
    (None, [], None),
])
def test_collection_persists_formula_default(price, variations, expected):
    db = ProductDB()
    saved = store.upsert_ai_original_product(
        source_product(price=price, variations=variations), connection_factory=lambda: db,
    )
    assert saved["net_proceeds_usd"] == expected
    assert saved["suggested_net_proceeds_usd"] == expected
    assert db.committed and db.closed and not db.rolled_back
    assert "FOR UPDATE" in db.queries[0][0]
    insert = next(query for query, _ in db.queries if query.startswith("INSERT"))
    assert "`net_proceeds_usd` IS NULL" in insert


@pytest.mark.parametrize("existing_net,expected", [(None, 5), (Decimal("19.5"), 19.5)])
def test_recollection_only_fills_missing_net(existing_net, expected):
    db = ProductDB(existing_product(net=existing_net))
    saved = store.upsert_ai_original_product(source_product(), connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] == expected


def test_completed_recollection_uses_preserved_snapshot_default():
    db = ProductDB(existing_product(status="completed"))
    saved = store.upsert_ai_original_product(
        source_product(variations=[{"price": 100}]), connection_factory=lambda: db,
    )
    assert saved["net_proceeds_usd"] == saved["suggested_net_proceeds_usd"] == 5
    assert saved["original_1688"]["variations"] == [{"price": 25}]


@pytest.mark.parametrize("currency", ["CNY", "USD"])
@pytest.mark.parametrize("purchase_price,expected", [("18.55", 4), ("100", 16), (None, None)])
def test_purchase_price_edit_synchronizes_ai_net_and_survives_stale_worker_save(currency, purchase_price, expected):
    db = ProductDB(existing_product(net=Decimal("19.5"), status="completed", currency_id=currency))
    stale_snapshot = json.loads(db.row["source_snapshot_json"])
    saved = store.update_1688_product(7, {"price": purchase_price}, connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] == saved["source_net_proceeds_usd"] == expected
    assert "net_proceeds_usd" not in saved["source_1688_overrides"]
    saved = store.update_ai_original_product(7, {"source_snapshot_json": stale_snapshot}, connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] == expected
    assert saved["source_purchase_price_cny"] == (float(purchase_price) if purchase_price is not None else None)


@pytest.mark.parametrize("net", ["14.75", None])
def test_explicit_source_net_edit_synchronizes_generated_ai_product(net):
    db = ProductDB(existing_product(net=Decimal("19.5"), status="completed", currency_id="USD"))
    saved = store.update_1688_product(7, {"net_proceeds_usd": net}, connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] == saved["source_net_proceeds_usd"] == (float(net) if net is not None else None)
    assert generate(db)["net_proceeds_usd"] == saved["net_proceeds_usd"]


@pytest.mark.parametrize("source_net", ["14.75", None])
def test_purchase_price_edit_keeps_explicit_source_net(source_net):
    db = ProductDB(existing_product(net=Decimal(source_net) if source_net is not None else None,
                                    overrides={"net_proceeds_usd": source_net}))
    saved = store.update_1688_product(7, {"price": "100"}, connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] == (float(source_net) if source_net is not None else None)


def test_recollection_synchronizes_changed_source_net():
    db = ProductDB(existing_product(net=Decimal("19.5")))
    saved = store.upsert_ai_original_product(source_product(variations=[{"price": 100}]), connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] == saved["source_net_proceeds_usd"] == 16


@pytest.mark.parametrize("operation", ["collect", "generate"])
@pytest.mark.parametrize("overrides,expected", [
    ({"price": 30}, 6),
    ({"price": None}, None),
    ({"net_proceeds_usd": None}, None),
    ({"price": 30, "net_proceeds_usd": 17}, 17),
])
def test_source_overrides_take_precedence(operation, overrides, expected):
    db = ProductDB(existing_product(overrides=overrides))
    if operation == "collect":
        saved = store.upsert_ai_original_product(source_product(), connection_factory=lambda: db)
    else:
        saved = generate(db)
    assert saved["source_1688_overrides"] == overrides
    assert saved["net_proceeds_usd"] == expected


@pytest.mark.parametrize("existing_net,expected", [(None, 5), (Decimal("19.5"), 19.5)])
def test_legacy_generation_only_fills_missing_net(existing_net, expected):
    db = ProductDB(existing_product(net=existing_net))
    saved = generate(db)
    assert saved["net_proceeds_usd"] == expected
    assert db.committed
    assert "FOR UPDATE" in db.queries[0][0]
    update = next(query for query, _ in db.queries if query.startswith("UPDATE"))
    if existing_net is None:
        assert "`net_proceeds_usd` = IF(`net_proceeds_usd` IS NULL" in update
    else:
        assert "`net_proceeds_usd` =" not in update


@pytest.mark.parametrize("entered_net", [None, "14.75"])
def test_explicit_listing_net_wins_over_default(entered_net):
    db = ProductDB(existing_product())
    saved = generate(db, net_proceeds_usd=entered_net)
    assert saved["net_proceeds_usd"] == (None if entered_net is None else 14.75)


@pytest.mark.parametrize("platform", ["amazon", "yandex"])
def test_other_platform_generation_never_fills_mercado_net(platform):
    db = ProductDB(existing_product())
    saved = store.update_ai_original_product(7, {
        "marketplace_listing": {"target_platform": platform, "status": "completed"},
    }, connection_factory=lambda: db)
    assert saved["net_proceeds_usd"] is None
    update = next(query for query, _ in db.queries if query.startswith("UPDATE"))
    assert "`net_proceeds_usd` =" not in update


def test_legacy_usd_listing_price_is_not_used_as_1688_purchase_price():
    db = ProductDB(existing_product(currency_id="USD", price=Decimal("100")))
    snapshot = json.loads(db.row["source_snapshot_json"])
    snapshot["original_1688"] = {"title": "无采购价"}
    db.row["source_snapshot_json"] = json.dumps(snapshot)
    assert generate(db)["net_proceeds_usd"] is None


def test_legacy_cny_purchase_column_is_usable_without_snapshot_price():
    db = ProductDB(existing_product())
    snapshot = json.loads(db.row["source_snapshot_json"])
    snapshot["original_1688"] = {"title": "旧商品"}
    db.row["source_snapshot_json"] = json.dumps(snapshot)
    assert generate(db)["net_proceeds_usd"] == 3


def test_legacy_display_price_range_uses_its_highest_price_before_column_fallback():
    db = ProductDB(existing_product(price=Decimal("100")))
    snapshot = json.loads(db.row["source_snapshot_json"])
    snapshot["original_1688"] = {"price": "¥10.00-20.00", "variations": []}
    db.row["source_snapshot_json"] = json.dumps(snapshot)
    assert generate(db)["net_proceeds_usd"] == 4


def test_legacy_unparseable_source_price_falls_back_to_cny_purchase_column():
    db = ProductDB(existing_product())
    snapshot = json.loads(db.row["source_snapshot_json"])
    snapshot["original_1688"] = {"price": "1e2", "variations": []}
    db.row["source_snapshot_json"] = json.dumps(snapshot)
    assert generate(db)["net_proceeds_usd"] == 3
