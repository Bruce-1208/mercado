"""Offline transaction tests for purchase-cost driven ERP proceeds."""
import copy
import json
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from bit import bit_mysql, purchase_net_proceeds as sync


def order(order_id="101", title="同名产品", quantity=2, **extra):
    return {"order_id": order_id, "token_id": 7, "workflow_status": "找货",
            "title": "不能用于匹配的摘要 等 2 件商品", "quantity": 999,
            "raw_json": {"order_items": [{"item": {"title": title}, "quantity": quantity}]},
            **extra}


def product(product_id=1, title="同名产品", **extra):
    return {"id": product_id, "title": title, "source_item_id": f"MLM{product_id}",
            "source_type": "collected", "net_proceeds_usd": Decimal("2.5"),
            "source_snapshot_json": "{}", "profitability_source": "estimated",
            "owner_token_ids": [7], **extra}


def link(link_id=1, title="同名产品", **extra):
    return {"id": link_id, "title": title, "token_id": 7, "store_name": "授权店铺",
            "item_id": f"MLM{link_id}", "net_proceeds_usd": Decimal("3"),
            "net_proceeds_manual": 0, "is_current": 1, **extra}


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.sql, self.params, self.rowcount = "", [], 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, list(params or [])
        self.connection.calls.append((sql, params))
        self.rowcount = 0
        if sql.startswith("UPDATE `erp_mercadolibre_products`"):
            amount, source, checked_at, snapshot, updated_at, row_id = params
            row = next(row for row in self.connection.products if row["id"] == row_id)
            row.update(net_proceeds_usd=amount, profitability_source=source,
                       profitability_updated_at=checked_at, source_snapshot_json=snapshot,
                       updated_at=updated_at, profitability_error=None)
            self.rowcount = 1
        elif sql.startswith("UPDATE `erp_mercadolibre_store_links`"):
            amount, updated_at, row_id = params
            row = next(row for row in self.connection.links if row["id"] == row_id)
            row.update(net_proceeds_usd=amount, net_proceeds_manual=1, updated_at=updated_at)
            self.rowcount = 1
        elif sql.startswith("UPDATE `mercado_synced_orders` AS synced"):
            assignments = re.findall(r"synced\.`(\w+)` = %s", sql)
            order_ids = params[len(assignments):]
            for row in self.connection.orders:
                if row["order_id"] in order_ids:
                    row.update(zip(assignments, params))
                    self.rowcount += 1
        elif sql.startswith("UPDATE `mercado_synced_orders` SET `workflow_status`"):
            for row in self.connection.orders:
                if row["order_id"] == params[1]:
                    row["workflow_status"] = params[0]
                    self.rowcount += 1

    def fetchone(self):
        if "FROM `erp_mercadolibre_exchange_rates`" in self.sql:
            return self.connection.rate
        if "FROM `erp_mercadolibre_exchange_rate_daily`" in self.sql:
            return self.connection.daily_rate
        return None

    def fetchall(self):
        if "SELECT synced.`order_id`" in self.sql:
            return copy.deepcopy([row for row in self.connection.orders if row["order_id"] in self.params])
        if self.sql.startswith("SELECT `id`, `title`, `source_item_id`"):
            if "AND (1 = 0)" in self.sql:
                return []
            allowed = {value for value in self.params if isinstance(value, int)}
            return copy.deepcopy([row for row in self.connection.products
                                  if row["title"].strip() in self.params
                                  and (not allowed or allowed.intersection(row["owner_token_ids"]))])
        if self.sql.startswith("SELECT `id`, `title`, `token_id`"):
            if "AND 1 = 0" in self.sql:
                return []
            allowed = {value for value in self.params if isinstance(value, int)}
            return copy.deepcopy([row for row in self.connection.links
                                  if row["title"].strip() in self.params and row["is_current"]
                                  and (not allowed or row["token_id"] in allowed)])
        return []

    def executemany(self, sql, params):
        self.connection.calls.append((sql, params))
        if self.connection.fail_logs:
            raise RuntimeError("audit insert failed")
        self.connection.logs.extend(copy.deepcopy(params))


class Connection:
    def __init__(self, orders=None, products=None, links=None, rate=None, daily_rate=None):
        self.orders = orders if orders is not None else [order()]
        self.products = products if products is not None else [product()]
        self.links = links if links is not None else [link()]
        self.rate = rate if rate is not None else {"rate": Decimal("7"), "source_created_at": date.today().isoformat(),
                                                 "refreshed_at": f"{date.today().isoformat()} 08:00:00"}
        self.daily_rate = daily_rate
        self.calls, self.logs = [], []
        self.fail_logs = False
        self.commits = self.rollbacks = self.closes = 0
        self.original = copy.deepcopy((self.orders, self.products, self.links, self.logs))

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1
        self.calls.append(("COMMIT", None))

    def begin(self):
        self.calls.append(("BEGIN", None))

    def rollback(self):
        self.rollbacks += 1
        self.orders, self.products, self.links, self.logs = copy.deepcopy(self.original)

    def close(self):
        self.closes += 1


@pytest.fixture
def run_save(monkeypatch):
    invalidations = []
    monkeypatch.setattr(bit_mysql, "_ensure_mercado_synced_orders_table", lambda cursor: None)
    monkeypatch.setattr(bit_mysql, "_ensure_mercado_store_tokens_table", lambda cursor: None)
    monkeypatch.setattr("bit.yandex_order_sync.store_source_sql", lambda cursor: "`mercado_store_tokens`")
    monkeypatch.setattr(sync.products, "ensure_collection_tables", lambda cursor: cursor.execute("DDL products"))
    monkeypatch.setattr(sync.links, "ensure_store_link_table", lambda cursor: cursor.execute("DDL links"))
    monkeypatch.setattr(sync.rates, "ensure_profitability_cache_tables", lambda cursor: cursor.execute("DDL rates"))
    monkeypatch.setattr(sync, "invalidate_caches", lambda: invalidations.append(True))

    def run(connection=None, **kwargs):
        connection = connection or Connection()
        monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_options: connection)
        result = bit_mysql.bulk_update_mercado_orders(
            [row["order_id"] for row in connection.orders], purchase_order="采购 001",
            operator_id=9, operator_name="采购员", **kwargs,
        )
        return result, connection, invalidations
    return run


@pytest.mark.parametrize("ratio, expected", [(150, "7.5000"), (200, "10.0000")])
def test_each_ratio_uses_per_unit_cny_cost_and_exact_titles(run_save, ratio, expected):
    connection = Connection(products=[product(1, " 同名产品 "), product(2), product(3, "其他商品")],
                            links=[link(1), link(2, token_id=8), link(3, is_current=0)])
    result, connection, invalidations = run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=ratio)
    aggregate = result["net_proceeds_sync"]
    assert aggregate["ratio"] == ratio
    assert aggregate["product_matched"] == aggregate["product_changed"] == 2
    assert aggregate["store_link_matched"] == aggregate["store_link_changed"] == 2
    assert connection.products[0]["net_proceeds_usd"] == Decimal(expected)
    assert connection.products[2]["net_proceeds_usd"] == Decimal("2.5")
    assert connection.links[2]["net_proceeds_usd"] == Decimal("3")
    assert connection.links[0]["net_proceeds_manual"] == 1
    assert invalidations == [True] and connection.commits == 2 and connection.rollbacks == 0
    event = next(row for row in connection.logs if row[1] == "purchase_net_proceeds_updated")
    detail = json.loads(event[7])["net_proceeds_sync"]
    assert detail["quantity"] == 2 and detail["unit_cost_cny"] == "35.0000"
    assert detail["net_proceeds_usd"] == expected
    assert detail["exchange_rate"]["cny_per_usd"] == "7"
    assert detail["targets"][0]["old_net_proceeds_usd"] == "2.5"
    assert detail["targets"][0]["new_net_proceeds_usd"] == expected
    statements = [sql for sql, _ in connection.calls]
    last_ddl = max(index for index, sql in enumerate(statements) if sql.startswith("DDL"))
    first_lock = min(index for index, sql in enumerate(statements) if "FOR UPDATE" in sql)
    assert last_ddl < first_lock
    assert last_ddl < statements.index("BEGIN") < first_lock
    assert not any(sql.startswith("DDL") for sql in statements[first_lock:])


def test_restricted_scope_applies_to_products_and_current_store_links(run_save):
    connection = Connection(products=[product(1), product(2, owner_token_ids=[8])],
                            links=[link(1), link(2, token_id=8), link(3, is_current=0)])
    result, connection, _ = run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio="150", allowed_token_ids=[7])
    assert result["net_proceeds_sync"]["product_matched"] == 1
    assert result["net_proceeds_sync"]["store_link_matched"] == 1
    assert connection.products[1]["net_proceeds_usd"] == Decimal("2.5")
    assert connection.links[1]["net_proceeds_usd"] == Decimal("3")
    product_sql = next(sql for sql, _ in connection.calls if sql.startswith("SELECT `id`, `title`, `source_item_id`"))
    assert "BINARY TRIM(`title`)" in product_sql and "own_records" in product_sql and "own_links" in product_sql
    link_sql = next(sql for sql, _ in connection.calls if sql.startswith("SELECT `id`, `title`, `token_id`"))
    assert "`is_current` = 1" in link_sql and "`token_id` IN (%s)" in link_sql


@pytest.mark.parametrize("ratio", [None, "", "   "])
def test_no_opt_in_keeps_legacy_queries_and_result(run_save, ratio):
    result, connection, invalidations = run_save(Connection(orders=[order(raw_json={})]), purchase_cost="0", purchase_net_proceeds_ratio=ratio)
    assert result == {"matched": 1, "changed": 1}
    assert not any("erp_mercadolibre" in sql or sql.startswith("DDL") for sql, _ in connection.calls)
    assert not any(row[1] == "purchase_net_proceeds_updated" for row in connection.logs)
    assert invalidations == []


@pytest.mark.parametrize("ratio", [0, 151, "nan", "Infinity", "-Infinity", "150%", "garbage"])
def test_rejects_invalid_ratio_before_connect(run_save, ratio):
    connection = Connection()
    with pytest.raises(ValueError, match="150% 或 200%"):
        run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=ratio)
    assert connection.calls == []


@pytest.mark.parametrize("cost", [None, "", 0, "-1", "nan", "Infinity", "garbage"])
def test_opt_in_requires_supplied_positive_finite_cost(run_save, cost):
    connection = Connection()
    with pytest.raises(ValueError, match="大于 0"):
        run_save(connection, purchase_cost=cost, purchase_net_proceeds_ratio=150)
    assert connection.calls == []


def test_same_title_multiple_order_lines_add_quantities_and_fallback_items(run_save):
    payload = {"items": [{"title": " 同名产品 ", "count": 1}, {"title": "同名产品", "quantity": 3}]}
    result, _, _ = run_save(Connection(orders=[order(raw_json=json.dumps(payload))]),
                            purchase_cost="140", purchase_net_proceeds_ratio=200)
    detail = result["net_proceeds_sync"]["results"][0]
    assert detail["quantity"] == 4 and detail["net_proceeds_usd"] == "10.0000"


@pytest.mark.parametrize("payload", [
    {}, {"order_items": [{"item": {"title": "同名产品"}}]},
    {"order_items": [{"item": {"title": "同名产品"}, "quantity": 0}]},
    {"order_items": [{"item": {"title": "同名产品"}, "quantity": "1.5"}]},
    {"order_items": [{"item": {}, "quantity": 1}]},
    {"order_items": [{"item": {"title": "同名产品"}, "quantity": 1}, {"item": {"title": "其他商品"}, "quantity": 1}]},
])
def test_missing_or_ambiguous_order_identity_rolls_back(run_save, payload):
    connection = Connection(orders=[order(raw_json=payload)])
    with pytest.raises(ValueError):
        run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert connection.commits == 1 and connection.rollbacks == 1
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


def test_conflicting_same_title_targets_in_bulk_save_are_rejected(run_save):
    connection = Connection(orders=[order("101", quantity=1), order("102", quantity=2)])
    with pytest.raises(ValueError, match="不同净收益"):
        run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert connection.commits == 1 and connection.rollbacks == 1 and connection.logs == []
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


def test_same_target_for_duplicate_order_titles_updates_once_and_logs_each(run_save):
    connection = Connection(orders=[order("101"), order("102")])
    result, connection, _ = run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    aggregate = result["net_proceeds_sync"]
    assert aggregate["product_matched"] == aggregate["store_link_matched"] == 1
    assert len(aggregate["results"]) == 2
    assert [row[0] for row in connection.logs if row[1] == "purchase_net_proceeds_updated"] == ["101", "102"]
    assert sum(sql.startswith("UPDATE `erp_mercadolibre_products`") for sql, _ in connection.calls) == 1


def test_no_same_title_matches_is_visible_in_event(run_save):
    result, connection, _ = run_save(Connection(products=[], links=[]), purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert result["net_proceeds_sync"]["unmatched_titles"] == ["同名产品"]
    event = next(row for row in connection.logs if row[1] == "purchase_net_proceeds_updated")
    assert "无同名" in event[2]
    assert json.loads(event[7])["net_proceeds_sync"]["targets"] == []


def test_same_amount_preserves_zero_changed_but_sets_manual_protection(run_save):
    connection = Connection(products=[product(net_proceeds_usd=Decimal("7.5"))], links=[link(net_proceeds_usd=Decimal("7.5"))])
    result, connection, _ = run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert result["net_proceeds_sync"]["product_changed"] == result["net_proceeds_sync"]["store_link_changed"] == 0
    assert connection.links[0]["net_proceeds_manual"] == 1
    assert connection.products[0]["profitability_source"] == sync.SOURCE


def test_ai_original_and_zying_snapshot_overrides_are_preserved(run_save):
    original = {"source_1688_overrides": {"price": 22, "net_proceeds_usd": 2}, "another_fact": "保留"}
    connection = Connection(products=[product(1, source_type="ai_original", source_snapshot_json=json.dumps(original)),
                                      product(2, source_type="zying")])
    _, connection, _ = run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    saved = json.loads(connection.products[0]["source_snapshot_json"])
    assert saved["source_1688_overrides"] == {"price": 22, "net_proceeds_usd": 7.5}
    assert saved["another_fact"] == "保留"
    assert saved["purchase_net_proceeds"]["net_proceeds_usd"] == "7.5000"
    assert json.loads(connection.products[1]["source_snapshot_json"])["zying_net_proceeds_usd"] == 7.5


def test_audit_failure_rolls_back_orders_products_and_links(run_save):
    connection = Connection()
    connection.fail_logs = True
    with pytest.raises(RuntimeError, match="audit insert failed"):
        run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert connection.commits == 1 and connection.rollbacks == connection.closes == 1
    assert (connection.orders, connection.products, connection.links, connection.logs) == connection.original
    assert any(sql.startswith("UPDATE `erp_mercadolibre_products`") for sql, _ in connection.calls)
    assert any(sql.startswith("UPDATE `mercado_synced_orders` AS") for sql, _ in connection.calls)


@pytest.mark.parametrize("rate", ["0", "-1", "nan", "Infinity", "garbage"])
def test_invalid_exchange_rate_is_rejected_atomically(run_save, rate):
    connection = Connection(rate={"rate": rate, "source_created_at": date.today().isoformat()})
    with pytest.raises(ValueError, match="汇率"):
        run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert connection.rollbacks == 1 and connection.commits == 1
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


def test_stale_rate_uses_recent_daily_snapshot(run_save):
    old_date = (date.today() - timedelta(days=8)).isoformat()
    daily_date = (date.today() - timedelta(days=2)).isoformat()
    connection = Connection(rate={"rate": "7.8", "source_created_at": old_date},
                            daily_rate={"rate": "7", "rate_date": daily_date})
    result, _, _ = run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    rate = result["net_proceeds_sync"]["results"][0]["exchange_rate"]
    assert rate["rate_date"] == daily_date
    assert rate["source"] == sync.rates.DAILY_EXCHANGE_RATE_TABLE
    assert rate["cny_per_usd"] == "7"


def test_stale_rate_without_daily_fallback_is_rejected(run_save):
    connection = Connection(rate={"rate": "7", "source_created_at": (date.today() - timedelta(days=8)).isoformat()})
    with pytest.raises(ValueError, match="最近 7 天"):
        run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert connection.rollbacks == 1 and connection.commits == 1


def test_empty_authorized_store_scope_never_updates_any_rows(run_save):
    connection = Connection()
    with pytest.raises(ValueError, match="无权"):
        run_save(connection, purchase_cost="70", purchase_net_proceeds_ratio=150, allowed_token_ids=[])
    assert connection.rollbacks == 1 and not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


@pytest.mark.parametrize("cost", ["1e100", "10000000000000000", "9999999999999999.99999"])
def test_cost_column_overflow_is_a_validation_error_before_connect(run_save, cost):
    connection = Connection()
    with pytest.raises(ValueError, match="金额范围"):
        run_save(connection, purchase_cost=cost, purchase_net_proceeds_ratio=150)
    assert connection.calls == []


def test_cost_rounding_matches_saved_cost_and_audited_calculation(run_save):
    result, connection, _ = run_save(purchase_cost="70.00005", purchase_net_proceeds_ratio=150)
    detail = result["net_proceeds_sync"]["results"][0]
    assert connection.orders[0]["purchase_cost"] == Decimal("70.0001")
    assert detail["purchase_cost_cny"] == "70.0001"
    assert detail["unit_cost_cny"] == "35.00005"


def test_postcommit_cache_failure_does_not_report_saved_operation_as_failed(run_save, monkeypatch):
    def fail():
        raise RuntimeError("cache failure")
    monkeypatch.setattr(sync, "invalidate_caches", fail)
    result, connection, _ = run_save(purchase_cost="70", purchase_net_proceeds_ratio=150)
    assert result["net_proceeds_sync"]["product_changed"] == 1
    assert connection.commits == 2 and connection.rollbacks == 0
