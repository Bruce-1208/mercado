import json
from datetime import datetime, timedelta, timezone

import pytest

from bit import clearance_sources as sources


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def summary(token_id=1, group="半托管", enabled=True):
    return {
        "id": token_id, "display_name": f"店铺 {token_id}", "enabled": enabled,
        "site_settings": [{"site_id": "MLM", "group_name": group}],
    }


def order(order_id, *, days=1, status="paid", item_id="MLM100", quantity=1, variation_id=None):
    return {
        "id": order_id, "status": status, "site_id": item_id[:3],
        "date_created": (NOW - timedelta(days=days)).isoformat(),
        "order_items": [{
            "item": {"id": item_id, "variation_id": variation_id},
            "quantity": quantity,
        }],
    }


class Client:
    def __init__(self, item=None, *, inventory_error=False, visits_error=False):
        self.item = item or {
            "id": "MLM100", "site_id": "MLM", "status": "active",
            "available_quantity": 5, "variations": [],
        }
        self.inventory_error = inventory_error
        self.visits_error = visits_error
        self.calls = []

    def get_marketplace_item(self, item_id, *, attributes):
        self.calls.append(("inventory", item_id, attributes))
        if self.inventory_error:
            raise RuntimeError("secret access_token=do-not-display")
        return {**self.item, "id": item_id}

    def request(self, method, path, *, params):
        self.calls.append((method, path, params))
        if self.visits_error:
            raise RuntimeError("secret access_token=do-not-display")
        return {
            "total_visits": 60, "date_from": "2026-08-29T00:00:00Z",
            "date_to": "2026-09-28T00:00:00Z",
        }


def configure(monkeypatch, *, orders=(), client=None, links=None):
    client = client or Client()
    if links is None:
        links = [{
            "link_id": 10, "token_id": 1, "site_id": "MLM", "item_id": "MLM100",
            "title": "商品", "seller_sku": "sku", "status": "active",
            "available_quantity": 99, "remote_json": json.dumps(client.item),
        }]
    monkeypatch.setattr(sources, "_client_for_store", lambda _: (client, {"meli_user_id": "42"}))
    monkeypatch.setattr(sources, "_fetch_orders", lambda *_: iter(orders))
    monkeypatch.setattr(sources, "load_store_links", lambda *_: iter(links))
    return client


def collect(monkeypatch, **kwargs):
    client = configure(monkeypatch, **kwargs)
    token = summary()
    return list(sources.collect_store(token, sources.eligible_scopes([token]), NOW)), client


def test_only_enabled_authorized_exact_half_managed_site_scopes():
    tokens = [summary(), summary(2, "自发货"), summary(3, enabled=False), summary(4)]
    tokens[0]["site_settings"].extend([
        {"site_id": "MLB", "group_name": "自发货"},
        {"site_id": "MLC", "group_name": "半托管"},
    ])
    result = sources.eligible_scopes(tokens, {1, 2, 3})
    assert set(result) == {(1, "MLM"), (1, "MLC")}
    assert result[(1, "MLM")]["store_name"] == "店铺 1"
    assert sources.eligible_scopes(tokens, set()) == {}
    assert sources.eligible_scopes([summary(enabled="false")]) == {}


def test_order_totals_deduplicate_and_apply_both_time_boundaries_and_statuses():
    one = order("1", days=7, quantity=2, variation_id=11)
    rows = [
        one, one, order("2", days=14, quantity=3, variation_id=11),
        order("3", days=14.01, quantity=100, variation_id=11),
        order("4", days=0, quantity=100, variation_id=11),
        order("5", status="cancelled", quantity=100, variation_id=11),
        order("6", status="invalid", quantity=100, variation_id=11),
        order("7", item_id="MLB200", quantity=100, variation_id=11),
        order("8", quantity=4, variation_id=12),
    ]
    totals, ambiguous = sources._order_sales(rows, {"MLM"}, NOW)
    assert totals == {("MLM", "MLM100", "11"): (2, 5), ("MLM", "MLM100", "12"): (4, 4)}
    assert ambiguous == set()


def test_variants_use_current_per_sku_inventory_and_delayed_visits_remain_reference(monkeypatch):
    client = Client({
        "id": "MLM100", "site_id": "MLM", "status": "active",
        "available_quantity": 90,
        "variations": [
            {"id": 11, "available_quantity": 3, "seller_custom_field": "red", "attribute_combinations": [{"name": "颜色", "value_name": "红"}]},
            {"id": 12, "available_quantity": 0, "seller_custom_field": "blue"},
        ],
    })
    rows, client = collect(monkeypatch, client=client, orders=[order("1", quantity=2, variation_id=11)])
    assert [row["sku_key"] for row in rows] == ["variation:11", "variation:12"]
    assert [row["quantity"] for row in rows] == [3, 0]
    assert [row["sales_7d"] for row in rows] == [2, 0]
    assert [row["sales_14d"] for row in rows] == [2, 0]
    assert all(row["inventory_complete"] and row["sales_complete"] for row in rows)
    assert rows[0]["seller_sku"] == "red"
    assert rows[0]["variation"] == "颜色: 红"
    assert rows[0]["visits_api_30d"] == 60
    assert all(row["visits_complete"] is False and row["visits_30d"] is None for row in rows)
    assert all(row["visits_api_shared_by_variants"] for row in rows)
    visits = [call for call in client.calls if call[0] == "GET"]
    assert visits == [("GET", "/items/MLM100/visits/time_window", {"last": 30, "unit": "day", "ending": "2026-09-28"})]


def test_partial_order_pages_never_supply_zero_sales(monkeypatch):
    client = configure(monkeypatch)

    def partial(*_):
        yield order("1", quantity=4)
        raise RuntimeError("secret access_token=do-not-display")

    monkeypatch.setattr(sources, "_fetch_orders", partial)
    token = summary()
    row = list(sources.collect_store(token, sources.eligible_scopes([token]), NOW))[0]
    assert row["sales_complete"] is False
    assert row["sales_7d"] is None and row["sales_14d"] is None
    assert row["pending_data"] is True
    assert row["inventory_complete"] is True
    assert "secret" not in json.dumps(row)


def test_failed_current_inventory_uses_cached_variant_identity_but_never_quantity(monkeypatch):
    client = Client({
        "id": "MLM100", "status": "active", "variations": [
            {"id": 11, "available_quantity": 3}, {"id": 12, "available_quantity": 4},
        ],
    }, inventory_error=True)
    rows, _ = collect(monkeypatch, client=client)
    assert [row["sku_key"] for row in rows] == ["variation:11", "variation:12"]
    assert all(row["quantity"] is None and not row["inventory_complete"] for row in rows)
    assert all(row["pending_data"] for row in rows)
    assert "secret" not in json.dumps(rows)


def test_ambiguous_variant_orders_never_become_zero_variant_sales(monkeypatch):
    client = Client({
        "id": "MLM100", "status": "active", "variations": [
            {"id": 11, "available_quantity": 3}, {"id": 12, "available_quantity": 4},
        ],
    })
    rows, _ = collect(monkeypatch, client=client, orders=[order("1", quantity=4)])
    assert all(not row["sales_complete"] and row["sales_14d"] is None for row in rows)
    assert all("变体" in row["source_errors"][0] for row in rows)


def test_single_sku_and_fresh_inactive_status(monkeypatch):
    rows, _ = collect(monkeypatch, orders=[order("1", quantity=8)])
    assert rows[0]["sku_key"] == "item"
    assert rows[0]["sales_7d"] == 8
    assert rows[0]["quantity"] == 5
    client = Client({"id": "MLM100", "status": "paused", "available_quantity": 0})
    rows, _ = collect(monkeypatch, client=client)
    assert rows[0]["on_sale"] is False and rows[0]["status"] == "paused"
    assert rows[0]["inventory_complete"] is True


def test_sufficient_sales_skip_visits_and_cbt_never_queries_unsupported_endpoint(monkeypatch):
    rows, client = collect(monkeypatch, orders=[order("1", quantity=7)])
    assert rows[0]["sales_14d"] == 7
    assert not [call for call in client.calls if call[0] == "GET"]
    result = sources._visits_reference(client, "CBT123", NOW)
    assert result["visits_api_30d"] is None
    assert "CBT" in result["visits_api_error"]
    assert not [call for call in client.calls if call[0] == "GET"]


def test_visits_errors_are_redacted_and_do_not_confirm_exposure(monkeypatch):
    rows, _ = collect(monkeypatch, client=Client(visits_error=True))
    row = rows[0]
    assert row["visits_api_error"] == "平台商品访问量读取失败"
    assert row["visits_complete"] is False
    assert row["visits_api_30d"] is None
    assert "secret" not in json.dumps(row)


def test_scope_recheck_rejects_other_tokens_or_self_shipping_before_any_source_read(monkeypatch):
    monkeypatch.setattr(sources, "_client_for_store", lambda *_: pytest.fail("unauthorized source read"))
    scopes = sources.eligible_scopes([summary()])
    assert list(sources.collect_store(summary(group="自发货"), scopes, NOW)) == []
    assert list(sources.collect_store(summary(2), scopes, NOW)) == []
    assert list(sources.collect_store(summary(enabled=False), scopes, NOW)) == []


def test_link_keyset_closes_connection_before_yield_and_keeps_site_scope(monkeypatch):
    events = []
    pages = [
        [{"link_id": 2, "item_id": "MLM100"}, {"link_id": 8, "item_id": "MLM200"}],
        [{"link_id": 12, "item_id": "MLM300"}],
    ]

    class Connection:
        def __init__(self, page):
            self.page = page

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, sql, params):
            events.append(("query", sql, params))

        def fetchall(self):
            return self.page

        def close(self):
            events.append(("closed",))

    monkeypatch.setattr(sources, "LINK_BATCH_SIZE", 2)
    monkeypatch.setattr(sources, "_connect", lambda: Connection(pages.pop(0)))
    iterator = sources.load_store_links(9, {"MLM"})
    assert next(iterator)["link_id"] == 2
    assert events[-1] == ("closed",)
    assert [row["link_id"] for row in iterator] == [8, 12]
    queries = [event for event in events if event[0] == "query"]
    assert queries[0][2] == (9, "MLM", 0, 2)
    assert queries[1][2] == (9, "MLM", 8, 2)
    assert all("`is_current` = 1" in event[1] and "`site_id` IN (%s)" in event[1] for event in queries)


def test_malformed_orders_and_missing_variant_inventory_fail_coverage():
    bad = order("1")
    bad["order_items"][0]["quantity"] = None
    with pytest.raises(ValueError):
        sources._order_sales([bad], {"MLM"}, NOW)
    client = Client({"id": "MLM100", "status": "active", "available_quantity": 20})
    link = {"item_id": "MLM100", "site_id": "MLM", "remote_json": {
        "variations": [{"id": 10, "available_quantity": 5}],
    }}
    _, complete = sources._item_inventory(client, link)
    assert complete is False

def test_scan_includes_product_main_image_and_cached_fallback(monkeypatch):
    item = {"id": "MLM100", "status": "active", "available_quantity": 5,
            "pictures": [{"secure_url": "https://example.com/main.jpg"}],
            "thumbnail": "http://example.com/small.jpg"}
    rows, client = collect(monkeypatch, client=Client(item))
    assert rows[0]["thumbnail_url"] == "https://example.com/main.jpg"
    assert "pictures" in client.calls[0][2]
    rows, _ = collect(monkeypatch, client=Client(item, inventory_error=True))
    assert rows[0]["thumbnail_url"] == "https://example.com/main.jpg"
    assert sources._main_image({}, {"thumbnail_url": "https://example.com/cached.jpg"}) == "https://example.com/cached.jpg"


def test_existing_snapshot_images_are_enriched_only_from_matching_current_links(monkeypatch):
    events = []
    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql, params):
            assert "`is_current` = 1" in sql
            assert params == (10, 11)
        def fetchall(self):
            return [{"link_id": 10, "token_id": 1, "site_id": "MLM", "item_id": "MLM100", "thumbnail_url": "https://example.com/main.jpg"},
                    {"link_id": 11, "token_id": 2, "site_id": "MLM", "item_id": "MLM100", "thumbnail_url": "https://example.com/other.jpg"}]
        def close(self): events.append("closed")
    monkeypatch.setattr(sources, "_connect", Connection)
    rows = [{"link_id": link_id, "token_id": 1, "site_id": "MLM", "item_id": "MLM100"} for link_id in (10, 11)]
    sources.fill_missing_images(rows)
    assert rows[0]["thumbnail_url"] == "https://example.com/main.jpg"
    assert "thumbnail_url" not in rows[1]
    assert events == ["closed"]
