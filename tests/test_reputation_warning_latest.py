"""Account-risk warnings retain a single complete, most recent notice."""
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

import pytest

from bit import bit_reputation_info as reputation
from bit import reputation_browser_sync as browser


def test_latest_notice_keeps_explanation_and_ignores_suspension_deadline():
    older = "Selling suspended\nSeptember 20, 2026, at 02:10\nSuspended until October 15, 2026."
    latest = "Review your listing\nOctober 1, 2026, at 09:30\nThe brand requires authorization.\nPlease submit the complete documents."
    assert reputation._latest_account_risk_message(
        [older, latest], now=datetime(2026, 10, 1, 12),
    ) == latest


@pytest.mark.parametrize("date", ["1 de octubre de 2026", "1 de outubro de 2026", "2026-10-01T09:30:00Z"])
def test_latest_notice_supports_localized_and_iso_dates(date):
    latest = {"title": "Listing warning", "description": "Keep this entire explanation.", "created_at": date}
    result = reputation._latest_account_risk_message(
        ["Old restriction\nSeptember 20, 2026", latest], now=datetime(2026, 10, 1, 12),
    )
    assert result == f"Listing warning\nKeep this entire explanation.\n{date}"


def test_missing_or_equal_dates_preserve_page_order():
    assert reputation._latest_account_risk_message(["First notice\nFull body", "Second notice"]) == "First notice\nFull body"
    assert reputation._latest_account_risk_message([
        "First notice\nToday, at 09:30", "Second notice\nToday, at 09:30",
    ], now=datetime(2026, 10, 1, 12)) == "First notice\nToday, at 09:30"


def test_localized_notice_uses_its_explicit_year():
    assert reputation._latest_account_risk_message([
        "Older notice\n1 de junio de 2025", "Newer notice\n1 de enero de 2026",
    ], now=datetime(2026, 10, 1, 12)) == "Newer notice\n1 de enero de 2026"


def test_whole_page_fallback_separates_cards_and_keeps_latest_body():
    page_text = (
        "Account risk\nRestrictions\n2\nGo to Restrictions\n"
        "Older notice\nSeptember 20, 2026, at 02:10\nOld explanation.\n"
        "Latest notice\nOctober 1, 2026, at 09:30\nFull first paragraph.\nFull second paragraph."
    )
    assert reputation._latest_account_risk_message([page_text], now=datetime(2026, 10, 1, 12)) == (
        "Latest notice\nOctober 1, 2026, at 09:30\nFull first paragraph.\nFull second paragraph."
    )


def test_overlapping_selectors_preserve_full_card():
    full_card = "Listing warning\nOctober 1, 2026\nFull warning explanation."
    assert reputation._normalize_account_risk_messages(["Listing warning", full_card, full_card]) == [full_card]


def test_risk_summary_waits_for_counts_after_heading_loads(monkeypatch):
    texts = iter(["Account risk\nLoading...", "Account risk\nRestrictions 0\nWarnings 1"])
    monkeypatch.setattr(reputation, "_extract_account_risk_page_text", lambda *args: next(texts))
    monkeypatch.setattr(reputation, "_raise_if_mercado_unavailable", lambda **kwargs: None)
    monkeypatch.setattr(reputation.time, "sleep", lambda *args: None)
    assert reputation._wait_account_risk_summary(object()) == "Account risk\nRestrictions 0\nWarnings 1"


def test_browser_selects_latest_across_restrictions_and_warnings(monkeypatch):
    opened = []
    monkeypatch.setattr(browser, "open_site", lambda session, url, site, **kwargs: opened.append((url, site, kwargs)))
    monkeypatch.setattr(reputation, "_extract_visits_from_metrics_api", lambda *args: [{"visits": i} for i in range(8)])
    monkeypatch.setattr(reputation, "_wait_account_risk_summary", lambda *args: "Restrictions 1\nWarnings 1")
    monkeypatch.setattr(reputation, "_get_account_risk_links", lambda *args: [])
    batches = iter([
        ["Old restriction\nSeptember 20, 2026\nAll restricted features."],
        ["Latest warning\nOctober 1, 2026\nComplete warning body."],
    ])
    monkeypatch.setattr(reputation, "_wait_account_risk_details", lambda *args: next(batches))
    row = browser.collect_site(SimpleNamespace(page=object()), "店铺", "墨西哥")
    assert row["system_warning"] == "Latest warning\nOctober 1, 2026\nComplete warning body."
    assert row["error"] == ""
    assert "account_status" not in row
    assert opened[1] == (reputation.SALES_SUMMARY_URL, "墨西哥", {"choose_site": True})
    assert opened[2][0] == reputation.ACCOUNT_RISK_URL


def test_auxiliary_traffic_failure_keeps_only_actual_risk_notice(monkeypatch):
    notice = "Latest warning\nOctober 1, 2026\nComplete warning body."
    monkeypatch.setattr(reputation, "_open_collection_backend_page", lambda *args, **kwargs: {})
    monkeypatch.setattr(reputation, "_collect_account_risk_status", lambda *args, **kwargs: {
        "account_status": "normal", "system_warning": notice,
    })
    def unavailable(*args, **kwargs):
        raise RuntimeError("流量读取失败")
    monkeypatch.setattr(reputation, "get_visits_info", unavailable)
    row = reputation.get_reputation_auxiliary_info("window", "店铺", "墨西哥", driver=object(), select_site=False)
    assert row["system_warning"] == notice
    assert "流量读取失败" in row["error"]


def test_auxiliary_risk_failure_does_not_replace_previous_warning(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("风险页读取失败")
    monkeypatch.setattr(reputation, "_open_collection_backend_page", unavailable)
    monkeypatch.setattr(reputation, "get_visits_info", lambda *args, **kwargs: list(range(8)))
    row = reputation.get_reputation_auxiliary_info("window", "店铺", "墨西哥", driver=object(), select_site=False)
    assert "system_warning" not in row
    assert "风险页读取失败" in row["error"]


@pytest.mark.parametrize("status,display", [("normal", "正常"), ("restricted", "封禁"), ("unknown", "状态未确认")])
def test_risk_cards_cannot_override_listing_sample_verdict(status, display):
    api_row = {"store_name": "店铺", "site_id": "MLM", "account_status": status,
               "site_status_display": display, "site_status_source": "official_listing_api"}
    database_row = ["店铺", "墨西哥", "绿色", 1, "0%", "0%", "0%", "", "", None, "now", "[]", display, 0, 0, 7]
    reputation._merge_api_auxiliary_rows([api_row], [database_row], [{
        "store_name": "店铺", "site": "墨西哥", "account_status": "permanently_suspended",
        "site_status_display": "永久封禁（已 10 天）", "suspension_days": 10,
        "system_warning": "A complete warning", "visits": "[1,2,3]",
    }])
    assert api_row["account_status"] == status
    assert api_row["site_status_display"] == database_row[12] == display
    assert database_row[9] == "A complete warning"
    assert database_row[11] == "[1,2,3]"


def test_api_collection_passes_seven_day_count_to_persistence():
    assert reputation._api_reputation_database_row("店铺", {"orders_current_7d": 0}, "now")[15] == 0
    assert reputation._api_reputation_database_row("店铺", {}, "now")[15] is None


@pytest.mark.parametrize("orders", [None, 0, 17])
def test_database_hydration_retains_seven_day_count(monkeypatch, orders):
    from bit import bit_interface as web
    monkeypatch.setattr(web, "_api_reputation_state", web._api_reputation_default_state())
    monkeypatch.setattr(web, "_api_reputation_database_hydration_attempted", False)
    monkeypatch.setattr(web, "_api_reputation_logs", [])
    monkeypatch.setattr(web, "_attach_reputation_token_ids", lambda data: data)
    monkeypatch.setattr(web, "_attach_latest_reputation_infraction_counts", lambda data: data)
    monkeypatch.setattr(web, "db_get_latest_reputation_info", lambda: {
        "latest_submit_time": "2026-10-01 09:30:00",
        "rows": [{"店铺名": "店铺", "站点": "墨西哥", "七天单量": orders, "总单量": 999}],
    })
    assert web._hydrate_api_reputation_from_database()
    assert web._api_reputation_snapshot()["rows"][0]["orders_current_7d"] == orders


def test_excel_export_includes_site_seven_day_count(monkeypatch):
    from bit import bit_interface as web
    from openpyxl import load_workbook
    user = {"id": 17, "username": "tester", "role_key": "super_admin", "permissions": ["*"],
            "access_version": 1, "is_active": True, "is_platform_admin": True}
    monkeypatch.setattr(web.app, "testing", True)
    monkeypatch.setattr(web, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(web, "get_workbench_user", lambda **kwargs: user)
    monkeypatch.setattr(web, "_attach_reputation_token_ids", lambda data: data)
    monkeypatch.setattr(web, "_attach_latest_reputation_infraction_counts", lambda data: data)
    monkeypatch.setattr(web, "_filter_store_rows_for_user", lambda data, **kwargs: data)
    monkeypatch.setattr(web, "db_get_latest_reputation_info", lambda: {
        "latest_submit_time": "2026-10-01 09:30:00",
        "rows": [{"店铺名": "店铺", "站点": "墨西哥", "七天单量": 17, "总单量": 999}],
    })
    client = web.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = user
    response = client.get("/api/reputation/latest/export")
    assert response.status_code == 200
    rows = list(load_workbook(BytesIO(response.data), read_only=True).active.values)
    assert rows[1][rows[0].index("七天单量")] == 17
    assert rows[1][rows[0].index("总单量")] == 999
