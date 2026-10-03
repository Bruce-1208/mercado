from datetime import datetime, timedelta, timezone

import pytest

from erp.mercadolibre_clearance_store import ClearanceStore, LOCAL, stamp, validate_facts


NOW = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)


def scope(*keys):
    return {key: {"store_name": f"半托管店铺 {key[0]}"} for key in keys}


def snapshot(link_id=1, sku_key="MLM1:1", token_id=11, site_id="MLM", **changes):
    row = {
        "link_id": link_id, "sku_key": sku_key, "token_id": token_id,
        "site_id": site_id, "item_id": f"{site_id}{link_id}",
        "seller_sku": f"sku-{sku_key}", "variation_id": sku_key.split(":")[-1],
        "title": f"商品 {link_id}", "store_name": f"半托管店铺 {token_id}",
        "group_name": "半托管", "status": "active", "available_quantity": 10,
        "sales_7d": 0, "sales_14d": 0, "sales_complete": True,
        "inventory_complete": True,
    }
    row.update(changes)
    return row


def verified(now=NOW, **changes):
    facts = {
        "first_inbound_at": "2026-01-01", "inbound_history_confirmed": True,
        "restock_after_stockout": False, "visits_30d": 301,
        "visits_as_of": now.astimezone(LOCAL).date().isoformat(),
    }
    facts.update(changes)
    return facts


@pytest.fixture
def store(tmp_path):
    return ClearanceStore(tmp_path / "clearance.sqlite3")


def seed(store, rows, facts=None, now=NOW):
    by_token = {}
    for row in rows:
        by_token.setdefault(row["token_id"], []).append(row)
    for token_id, token_rows in by_token.items():
        store.replace_store(token_id, token_rows, now=now)
    scopes = scope(*{(row["token_id"], row["site_id"]) for row in rows})
    for row in rows:
        store.save_facts(row["link_id"], row["sku_key"], facts or verified(now), scopes, now=now)
    return scopes


def test_empty_scope_never_reads_or_writes_another_store(store):
    seed(store, [snapshot()])
    result = store.list_alerts({}, now=NOW)
    assert result["rows"] == []
    assert result["total"] == 0
    assert result["stores"] == []
    assert all(value == 0 for value in result["counts"].values())
    with pytest.raises(KeyError):
        store.save_facts(1, "MLM1:1", {"notice_at": "2026-09-30"}, {}, now=NOW)


def test_current_token_site_and_variation_permissions_are_isolated(store):
    rows = [snapshot(), snapshot(sku_key="MLM1:2"),
            snapshot(link_id=2, sku_key="MLB2:1", site_id="MLB"),
            snapshot(link_id=3, sku_key="MLM3:1", token_id=22)]
    seed(store, rows)
    allowed = scope((11, "MLM"))
    result = store.list_alerts(allowed, now=NOW)
    assert {row["sku_key"] for row in result["rows"]} == {"MLM1:1", "MLM1:2"}
    store.save_facts(1, "MLM1:1", {"visits_30d": 300}, allowed, now=NOW)
    assert store.facts(1, "MLM1:1")["visits_30d"] == 300
    assert store.facts(1, "MLM1:2")["visits_30d"] == 301
    with pytest.raises(KeyError):
        store.save_facts(2, "MLB2:1", {"visits_30d": 300}, allowed, now=NOW)
    with pytest.raises(KeyError):
        store.save_facts(3, "MLM3:1", {"visits_30d": 300}, allowed, now=NOW)


def test_many_scopes_avoid_sqlite_expression_depth_failure(store):
    seed(store, [snapshot()])
    allowed = scope(*((token_id, "MLM") for token_id in range(1, 1201)))
    assert store.list_alerts(allowed, now=NOW)["total"] == 1


@pytest.mark.parametrize("payload", [
    {"unknown": 1}, {"visits_30d": -1}, {"visits_30d": True},
    {"visits_30d": 2147483648}, {"visits_30d": "0"},
    {"inbound_history_confirmed": 1}, {"restock_after_stockout": "false"},
    {"first_inbound_at": "2026-02-30"}, {"notice_at": "2026-10-01"},
    {"return_order_at": "2026-10-01"},
])
def test_facts_reject_unknown_fields_bad_types_and_future_received_dates(payload):
    with pytest.raises(ValueError):
        validate_facts(payload, now=NOW)


def test_fact_partial_updates_preserve_existing_and_allow_clearing(store):
    scopes = seed(store, [snapshot()])
    store.save_facts(1, "MLM1:1", {"notice_at": "2026-09-29"}, scopes, now=NOW)
    assert store.facts(1, "MLM1:1")["visits_30d"] == 301
    assert store.facts(1, "MLM1:1")["first_inbound_at"] == "2026-01-01"
    store.save_facts(1, "MLM1:1", {"notice_at": None}, scopes, now=NOW)
    assert store.facts(1, "MLM1:1")["notice_at"] is None


@pytest.mark.parametrize("payload", [
    {"inbound_history_confirmed": True},
    {"restock_after_stockout": True},
    {"visits_30d": 10},
    {"first_inbound_at": "2026-09-29", "last_restock_at": "2026-09-28"},
])
def test_confirmed_facts_require_their_related_dates(store, payload):
    store.replace_store(11, [snapshot()], now=NOW)
    with pytest.raises(ValueError):
        store.save_facts(1, "MLM1:1", payload, scope((11, "MLM")), now=NOW)
    assert store.facts(1, "MLM1:1") == {}


def test_visits_observation_can_be_future_only_for_scheduled_promotion():
    assert validate_facts({"promotion_start_at": "2026-10-01"}, NOW) == {"promotion_start_at": "2026-10-01"}
    assert validate_facts({"visits_30d": 0, "restock_after_stockout": None}, NOW)["visits_30d"] == 0


def test_filtered_counts_and_total_cover_all_pages(store):
    rows = [snapshot(link_id=value, sku_key=f"MLM{value}:1") for value in range(1, 6)]
    scopes = seed(store, rows)
    store.save_facts(2, "MLM2:1", {"visits_30d": 300}, scopes, now=NOW)
    first = store.list_alerts(scopes, alert_status="risk", page=1, page_size=2, now=NOW)
    second = store.list_alerts(scopes, alert_status="risk", page=2, page_size=2, now=NOW)
    assert first["total"] == second["total"] == 4
    assert first["counts"] == second["counts"] == {"risk": 4, "pending_data": 0, "exempt": 1, "normal": 0}
    assert len(first["rows"]) == len(second["rows"]) == 2
    assert {r["sku_key"] for r in first["rows"]}.isdisjoint({r["sku_key"] for r in second["rows"]})
    searched = store.list_alerts(scopes, search="MLM2", now=NOW)
    assert searched["total"] == 1
    assert searched["counts"]["exempt"] == 1
    assert store.list_alerts(scopes, search="%", now=NOW)["total"] == 0


def test_out_of_stock_rows_are_excluded_including_after_store_fetch_failure(store):
    scopes = seed(store, [snapshot(available_quantity=0)])
    assert store.list_alerts(scopes, now=NOW)["total"] == 0
    store.mark_failed(11, now=NOW)
    assert store.list_alerts(scopes, now=NOW)["total"] == 0


def test_actual_failed_snapshot_cannot_be_revived_by_editing_facts(store):
    scopes = seed(store, [snapshot()])
    store.mark_failed(11, now=NOW + timedelta(minutes=1))
    result = store.save_facts(1, "MLM1:1", {"notice_at": "2026-09-30"}, scopes,
                              now=NOW + timedelta(minutes=2))
    assert result["alert"]["status"] == "pending_data"
    assert store.list_alerts(scopes, now=NOW + timedelta(minutes=2))["counts"]["risk"] == 0
    store.replace_store(11, [snapshot()], now=NOW + timedelta(minutes=3))
    assert store.list_alerts(scopes, now=NOW + timedelta(minutes=3))["counts"]["risk"] == 1


def test_stale_snapshot_is_downgraded_without_losing_facts(store):
    scopes = seed(store, [snapshot()])
    later = NOW + timedelta(hours=27)
    result = store.list_alerts(scopes, now=later)
    assert result["total"] == 1
    assert result["counts"]["pending_data"] == 1
    assert result["rows"][0]["alert"]["status"] == "pending_data"
    assert store.facts(1, "MLM1:1")["first_inbound_at"] == "2026-01-01"
    changed = store.save_facts(1, "MLM1:1", {"visits_as_of": "2026-10-01"}, scopes, now=later)
    assert changed["alert"]["status"] == "pending_data"


def test_next_calendar_day_fact_edit_does_not_refresh_prior_sales_windows(store):
    before_midnight = datetime(2026, 9, 30, 15, 50, tzinfo=timezone.utc)
    after_midnight = before_midnight + timedelta(minutes=20)
    scopes = seed(store, [snapshot()], now=before_midnight)
    assert store.list_alerts(scopes, now=after_midnight)["counts"]["pending_data"] == 1
    changed = store.save_facts(1, "MLM1:1", {
        "visits_as_of": "2026-10-01", "visits_30d": 301,
    }, scopes, now=after_midnight)
    assert changed["alert"]["status"] == "pending_data"
    assert store.list_alerts(scopes, now=after_midnight)["counts"]["risk"] == 0


def test_current_date_visits_expire_after_new_successful_scan(store):
    scopes = seed(store, [snapshot()])
    later = NOW + timedelta(days=1)
    store.replace_store(11, [snapshot()], now=later)
    result = store.list_alerts(scopes, now=later)
    assert result["rows"][0]["alert"]["status"] == "pending_data"
    assert "visits_complete" in result["rows"][0]["alert"]["missing_fields"]
    changed = store.save_facts(1, "MLM1:1", {"visits_as_of": "2026-10-01"}, scopes, now=later)
    assert changed["alert"]["status"] == "risk"


def test_replacement_rolls_back_when_generator_fails(store):
    scopes = seed(store, [snapshot()])

    def failed_source():
        yield snapshot(link_id=2, sku_key="MLM2:1")
        raise RuntimeError("page 2 failed")

    with pytest.raises(RuntimeError):
        store.replace_store(11, failed_source(), now=NOW)
    assert [r["sku_key"] for r in store.list_alerts(scopes, now=NOW)["rows"]] == ["MLM1:1"]


def test_replacement_rolls_back_when_a_row_has_mismatched_token(store):
    scopes = seed(store, [snapshot()])
    with pytest.raises(ValueError):
        store.replace_store(11, [snapshot(link_id=2, sku_key="MLM2:1"),
                                 snapshot(link_id=3, sku_key="MLM3:1", token_id=22)], now=NOW)
    assert [r["sku_key"] for r in store.list_alerts(scopes, now=NOW)["rows"]] == ["MLM1:1"]


def test_replenishment_invalidates_old_confirmation_until_explicit_reverification(store):
    scopes = seed(store, [snapshot()])
    store.replace_store(11, [snapshot(available_quantity=0)], now=NOW + timedelta(hours=1))
    refill_at = NOW + timedelta(hours=2)
    store.replace_store(11, [snapshot()], now=refill_at)
    changed = store.save_facts(1, "MLM1:1", {"visits_30d": 301}, scopes,
                              now=refill_at + timedelta(minutes=1))
    assert changed["alert"]["status"] == "pending_data"
    assert "restock_after_stockout" in changed["alert"]["missing_fields"]
    verified_again = store.save_facts(1, "MLM1:1", {"restock_after_stockout": False}, scopes,
                                     now=refill_at + timedelta(minutes=2))
    assert verified_again["alert"]["status"] == "risk"


def test_site_scoped_refresh_and_failure_preserve_other_site(store):
    scopes = seed(store, [snapshot(), snapshot(link_id=2, sku_key="MLB2:1", site_id="MLB")])
    store.replace_store(11, [snapshot(sales_7d=1, sales_14d=7)],
                        now=NOW + timedelta(minutes=1), site_ids={"MLM"})
    result = store.list_alerts(scopes, now=NOW + timedelta(minutes=1))
    assert result["counts"]["normal"] == 1
    assert result["counts"]["risk"] == 1
    store.mark_failed(11, now=NOW + timedelta(minutes=2), site_ids={"MLM"})
    assert store.list_alerts(scope((11, "MLB")), now=NOW + timedelta(minutes=2))["last_error"] == ""
    assert store.list_alerts(scope((11, "MLB")), now=NOW + timedelta(minutes=2))["counts"]["risk"] == 1
    assert store.list_alerts(scope((11, "MLM")), now=NOW + timedelta(minutes=2))["last_error"]


def test_empty_site_scope_is_a_noop_and_empty_collected_site_removes_only_that_site(store):
    scopes = seed(store, [snapshot(), snapshot(link_id=2, sku_key="MLB2:1", site_id="MLB")])
    store.replace_store(11, [], now=NOW, site_ids=set())
    store.mark_failed(11, now=NOW, site_ids=set())
    assert store.list_alerts(scopes, now=NOW)["counts"]["risk"] == 2
    store.replace_store(11, [], now=NOW, site_ids={"MLM"})
    result = store.list_alerts(scopes, now=NOW)
    assert [row["site_id"] for row in result["rows"]] == ["MLB"]
    assert store.last_success_date(11, {"MLM"}) == "2026-09-30"


def test_site_mismatch_rolls_back_and_keeps_the_previous_snapshot(store):
    scopes = seed(store, [snapshot(), snapshot(link_id=2, sku_key="MLB2:1", site_id="MLB")])
    with pytest.raises(ValueError):
        store.replace_store(11, [snapshot(link_id=3, sku_key="MLB3:1", site_id="MLB")], now=NOW, site_ids={"MLM"})
    assert store.list_alerts(scopes, now=NOW)["total"] == 2


def test_daily_success_requires_all_current_sites_not_only_one_manual_scan(store):
    store.replace_store(11, [], now=NOW, site_ids={"MLM"})
    assert store.last_success_date(11, {"MLM", "MLB"}) == ""
    store.replace_store(11, [], now=NOW - timedelta(days=1), site_ids={"MLB"})
    assert store.last_success_date(11, {"MLM", "MLB"}) == "2026-09-29"
    store.replace_store(11, [], now=NOW, site_ids={"MLB"})
    assert store.last_success_date(11, {"MLM", "MLB"}) == "2026-09-30"
    store.mark_failed(11, now=NOW, site_ids={"MLB"})
    assert store.last_success_date(11, {"MLM", "MLB"}) == ""


def test_partial_source_errors_remain_pending_and_retry_without_invalidating_good_site(store):
    scopes = seed(store, [snapshot(), snapshot(link_id=2, sku_key="MLB2:1", site_id="MLB")])
    store.replace_store(11, [snapshot(sales_complete=False, sales_7d=None, sales_14d=None,
                                     source_errors=["完整订单数据读取失败"])], now=NOW, site_ids={"MLM"})
    assert store.last_success_date(11, {"MLM"}) == ""
    assert store.last_success_date(11, {"MLB"}) == "2026-09-30"
    assert store.list_alerts(scope((11, "MLM")), now=NOW)["counts"]["pending_data"] == 1
    assert store.list_alerts(scope((11, "MLB")), now=NOW)["counts"]["risk"] == 1
    assert store.list_alerts(scopes, now=NOW)["last_error"]
