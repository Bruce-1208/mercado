from datetime import datetime, timedelta, timezone

import pytest

from erp.mercadolibre_clearance_rules import (
    evaluate_clearance,
    evaluate_workflow,
    is_semi_managed_group,
)


NOW = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)


def evidence(**changes):
    row = {
        "group_name": "半托管",
        "status": "active",
        "available_quantity": 10,
        "sales_7d": 0,
        "sales_14d": 0,
        "sales_complete": True,
        "visits_30d": 301,
        "visits_complete": True,
        "first_inbound_at": NOW - timedelta(days=21),
        "inbound_history_confirmed": True,
        "restock_after_stockout": False,
        "last_restock_at": None,
    }
    row.update(changes)
    return row


@pytest.mark.parametrize("name", ["半托管", " 半托管 ", "半托管一组", "墨西哥半托管组"])
def test_configured_semi_managed_groups(name):
    assert is_semi_managed_group(name)


@pytest.mark.parametrize("name", [None, "", "自营", "非半托管", "非 半托管", "非-半托管组"])
def test_other_and_explicit_negative_groups(name):
    assert not is_semi_managed_group(name)


def test_never_infers_scope_from_store_name():
    result = evaluate_clearance(evidence(group_name="自营", store_name="半托管店铺"), NOW)
    assert result["status"] == "not_applicable"


@pytest.mark.parametrize("changes", [{"status": "paused"}, {"status": "closed"}, {"available_quantity": 0}])
def test_non_sale_or_empty_inventory_does_not_alert(changes):
    assert evaluate_clearance(evidence(**changes), NOW)["status"] == "not_applicable"


@pytest.mark.parametrize("changes", [{"status": "paused"}, {"available_quantity": 0}])
def test_failed_inventory_cannot_use_cached_inactive_or_empty_stock_as_scope_evidence(changes):
    result = evaluate_clearance(evidence(inventory_complete=False, **changes), NOW)
    assert result["status"] == "pending_data"
    assert "inventory_complete" in result["missing_fields"]


@pytest.mark.parametrize("sales_7d,sales_14d,reasons", [(0, 7, 1), (1, 6, 1), (0, 0, 2)])
def test_either_sales_trigger_is_sufficient(sales_7d, sales_14d, reasons):
    result = evaluate_clearance(evidence(sales_7d=sales_7d, sales_14d=sales_14d), NOW)
    assert result["status"] == "risk"
    assert len(result["trigger_reasons"]) == reasons
    assert result["missing_fields"] == []


def test_exact_sales_threshold_is_normal_without_exemption_evidence():
    result = evaluate_clearance(evidence(
        sales_7d=1, sales_14d=7, visits_30d=None, visits_complete=False,
        first_inbound_at=None, inbound_history_confirmed=False, restock_after_stockout=None,
    ), NOW)
    assert result["status"] == "normal"
    assert result["missing_fields"] == []


@pytest.mark.parametrize("days,status", [(20, "exempt"), (21, "risk")])
def test_new_product_exact_day_boundary(days, status):
    result = evaluate_clearance(evidence(first_inbound_at=NOW - timedelta(days=days)), NOW)
    assert result["status"] == status


@pytest.mark.parametrize("days,status", [(13, "exempt"), (14, "risk")])
def test_stockout_replenishment_exact_day_boundary(days, status):
    result = evaluate_clearance(evidence(
        restock_after_stockout=True, last_restock_at=NOW - timedelta(days=days),
    ), NOW)
    assert result["status"] == status


def test_ordinary_recent_restock_is_not_an_exemption():
    result = evaluate_clearance(evidence(last_restock_at=NOW, restock_after_stockout=False), NOW)
    assert result["status"] == "risk"


@pytest.mark.parametrize("visits,status", [(0, "exempt"), (300, "exempt"), (301, "risk")])
def test_low_exposure_boundary_uses_full_30_day_total(visits, status):
    assert evaluate_clearance(evidence(visits_30d=visits), NOW)["status"] == status


@pytest.mark.parametrize("changes,missing", [
    ({"sales_complete": False}, "sales_complete"),
    ({"sales_7d": None}, "sales_7d"),
    ({"sales_14d": None}, "sales_14d"),
    ({"visits_complete": False}, "visits_complete"),
    ({"visits_30d": None}, "visits_30d"),
    ({"first_inbound_at": None}, "first_inbound_at"),
    ({"inbound_history_confirmed": False}, "inbound_history_confirmed"),
    ({"restock_after_stockout": None}, "restock_after_stockout"),
    ({"restock_after_stockout": True, "last_restock_at": None}, "last_restock_at"),
])
def test_missing_critical_evidence_cannot_be_definite_risk(changes, missing):
    result = evaluate_clearance(evidence(**changes), NOW)
    assert result["status"] == "pending_data"
    assert missing in result["missing_fields"]


def test_proven_exemption_survives_missing_sales_and_other_exemptions():
    result = evaluate_clearance(evidence(
        sales_7d=None, sales_14d=None, sales_complete=False,
        first_inbound_at=None, inbound_history_confirmed=False,
        restock_after_stockout=None, visits_30d=300,
    ), NOW)
    assert result["status"] == "exempt"
    assert "sales_7d" in result["missing_fields"]
    assert result["exemption_reasons"]


def test_unknown_inventory_stays_pending_even_with_proven_exemption():
    result = evaluate_clearance(evidence(available_quantity=None, visits_30d=0), NOW)
    assert result["status"] == "pending_data"
    assert "available_quantity" in result["missing_fields"]


@pytest.mark.parametrize("bad_date", ["nonsense", "2026-02-30", NOW + timedelta(seconds=1)])
def test_invalid_or_future_inbound_is_unknown(bad_date):
    result = evaluate_clearance(evidence(first_inbound_at=bad_date), NOW)
    assert result["status"] == "pending_data"
    assert "first_inbound_at" in result["missing_fields"]


@pytest.mark.parametrize("field,bad_value", [
    ("sales_7d", -1), ("sales_7d", True), ("sales_14d", "0"),
    ("visits_30d", 0.5), ("available_quantity", -1),
])
def test_invalid_counts_are_not_zero_filled(field, bad_value):
    result = evaluate_clearance(evidence(**{field: bad_value}), NOW)
    assert result["status"] == "pending_data"
    assert field in result["missing_fields"]


def test_conflicting_sales_windows_are_pending():
    result = evaluate_clearance(evidence(sales_7d=8, sales_14d=6), NOW)
    assert result["status"] == "pending_data"
    assert not result["trigger_reasons"]


def test_unconfirmed_low_traffic_does_not_prove_exemption():
    result = evaluate_clearance(evidence(visits_30d=0, visits_complete=False), NOW)
    assert result["status"] == "pending_data"
    assert result["exemption_reasons"] == []


def test_date_only_is_shanghai_midnight_and_output_is_utc():
    clock = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
    result = evaluate_clearance(evidence(first_inbound_at="2026-09-09"), clock)
    assert result["status"] == "risk"  # exactly 21 Shanghai calendar days
    assert result["checked_at"] == "2026-09-29T16:00:00Z"


def test_risk_does_not_invent_platform_notice_or_deadline():
    result = evaluate_workflow({}, "risk", NOW)
    assert result["stage"] == "none"
    assert result["deadline_at"] is None
    assert result["days_remaining"] is None


def test_notice_has_three_day_deadline_in_merchant_calendar():
    result = evaluate_workflow({"notice_at": "2026-09-29"}, "risk", NOW)
    assert result["stage"] == "notice"
    assert result["deadline_at"] == "2026-10-01T16:00:00Z"
    assert result["days_remaining"] == 2


def test_expired_notice_requests_platform_verification_without_inventing_promotion():
    result = evaluate_workflow({"notice_at": NOW - timedelta(days=3)}, "risk", NOW)
    assert result["stage"] == "notice_due"
    assert "核实" in result["summary"]


@pytest.mark.parametrize("age,stage", [(20, "promotion"), (21, "promotion_due")])
def test_promotion_deadline_at_day_twenty_one(age, stage):
    result = evaluate_workflow({"promotion_start_at": NOW - timedelta(days=age)}, "risk", NOW)
    assert result["stage"] == stage
    assert result["days_remaining"] == 21 - age


@pytest.mark.parametrize("alert_status", ["normal", "exempt"])
def test_recovered_promotion_does_not_claim_a_return_order(alert_status):
    result = evaluate_workflow({"promotion_start_at": NOW - timedelta(days=21)}, alert_status, NOW)
    assert result["stage"] == "promotion_complete"
    assert "核实" in result["summary"]


@pytest.mark.parametrize("alert_status", ["risk", "normal", "exempt", "pending_data"])
def test_actual_return_order_deadline_wins_over_sales_or_promotion(alert_status):
    result = evaluate_workflow({
        "return_order_at": NOW - timedelta(days=4),
        "promotion_start_at": NOW - timedelta(days=30),
    }, alert_status, NOW)
    assert result["stage"] == "pickup_due"
    assert result["days_remaining"] == 1


def test_overdue_pickup_warns_about_destruction_risk_without_claiming_destruction():
    result = evaluate_workflow({"return_order_at": NOW - timedelta(days=7)}, "risk", NOW)
    assert result["stage"] == "pickup_overdue"
    assert result["days_remaining"] == -2
    assert "销毁风险" in result["summary"]
    assert "已销毁" not in result["summary"]


def test_invalid_platform_dates_do_not_create_workflow():
    result = evaluate_workflow({
        "notice_at": "invalid", "promotion_start_at": "2026-02-30",
        "return_order_at": NOW + timedelta(days=1),
    }, "risk", NOW)
    assert result["stage"] == "none"


def test_future_confirmed_promotion_is_scheduled():
    result = evaluate_workflow({"promotion_start_at": NOW + timedelta(days=1)}, "risk", NOW)
    assert result["stage"] == "promotion_scheduled"
    assert result["days_remaining"] == 1
