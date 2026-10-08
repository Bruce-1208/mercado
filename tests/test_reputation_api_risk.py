"""Pure API site warnings and current ban evidence, with no browser execution."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bit import mercado_reputation as api, bit_mysql, bit_reputation_info as collector


def notice(site="MLM", date="2026-10-01T09:00:00Z", **extra):
    return {"id": "warning-1", "site_id": site, "user_id": "seller",
            "date_created": date, "reason": "<p>Warning reason</p>",
            "remedy": "<p>First paragraph.</p><p>Second paragraph.</p>", **extra}


def page(rows, total=None):
    return {"infractions": rows, "paging": {"total": len(rows) if total is None else total},
            "sorting_type": "date_created_desc"}


def test_latest_site_warning_pages_past_foreign_sites_and_has_no_history_cutoff(monkeypatch):
    calls = []
    pages = iter([page([notice("MLB", "2026-10-07T10:00:00Z")], 3),
                  page([notice(date="2025-01-01T00:00:00Z"), notice(date="2025-02-01T00:00:00Z")], 3)])
    def fetch(_token, path, **kwargs):
        calls.append((path, kwargs["params"]))
        return next(pages)
    monkeypatch.setattr(api, "_fetch_json", fetch)
    result = api._latest_site_warning("token", "seller", "MLM")
    assert result["system_warning_occurred_at"] == "2025-02-01T00:00:00Z"
    assert result["system_warning"] == "2025-02-01T00:00:00Z\nWarning reason\nFirst paragraph.\nSecond paragraph."
    assert [params["offset"] for _, params in calls] == [0, 1]
    assert all(params == {"sort": "date_created_desc", "limit": 20, "offset": i}
               for i, (_, params) in zip([0, 1], calls))
    assert calls[0][0] == "/marketplace/moderations/infractions/seller"


def test_warning_timestamp_compares_timezone_offsets(monkeypatch):
    monkeypatch.setattr(api, "_fetch_json", lambda *_args, **_kwargs: page([
        notice(date="2026-10-01T09:00:00+08:00", reason="Older"),
        notice(date="2026-10-01T02:00:00Z", reason="Latest"),
    ]))
    assert api._latest_site_warning("token", "seller", "MLM")["system_warning"].splitlines()[1] == "Latest"


@pytest.mark.parametrize("payload", [
    {}, {"infractions": [], "paging": {"total": "invalid"}},
    page(None, 1), page([], 1), page([notice(date="")]),
    page([notice(site_id="", related_item_id="", element_id="")]),
    page([notice(user_id="other-seller")]), page([notice(reason="")]),
    {**page([notice()]), "sorting_type": "date_created_asc"},
])
def test_unconfirmed_warning_payload_is_never_reported_as_normal(monkeypatch, payload):
    monkeypatch.setattr(api, "_fetch_json", lambda *_args, **_kwargs: payload)
    with pytest.raises(api.MercadoReputationError):
        api._latest_site_warning("token", "seller", "MLM")


def test_api_null_empty_response_is_explicit_about_coverage(monkeypatch):
    monkeypatch.setattr(api, "_fetch_json", lambda *_args, **_kwargs: page(None, 0))
    result = api._latest_site_warning("token", "seller", "MLM")
    assert result["system_warning_status"] == "empty"
    assert "仅限接口可获取记录" in result["system_warning"]


def test_related_item_identifies_site_when_site_id_missing(monkeypatch):
    monkeypatch.setattr(api, "_fetch_json", lambda *_args, **_kwargs: page([
        notice(site_id="", related_item_id="MLM12345"),
    ]))
    assert api._latest_site_warning("token", "seller", "MLM")["system_warning_status"] == "success"


@pytest.mark.parametrize("status, expected, banned", [
    ({"site_status": "active"}, "unknown", None),
    ({"sell": {"allow": False}}, "restricted", True),
    ({"site_status": "suspended", "sell": {"allow": True}}, "restricted", True),
    ({"sell": {"allow": True}, "list": {"allow": False}}, "normal", False),
    ({"list": {"allow": False}}, "unknown", None),
    ({"site_status": "inactive"}, "unknown", None),
    ({"site_status": "unexpected"}, "unknown", None),
    ({"sell": {"allow": "false"}}, "unknown", None),
])
def test_ban_verdict_uses_current_explicit_site_evidence(status, expected, banned):
    result = api._site_status({"status": status})
    assert result["account_status"] == expected
    assert result["is_banned"] is banned


@pytest.mark.parametrize("profile", [
    {"id": "other-seller", "site_id": "MLM", "status": {"sell": {"allow": True}}},
    {"id": "seller", "site_id": "MLB", "status": {"sell": {"allow": False}}},
])
def test_foreign_profile_cannot_establish_site_ban_status(monkeypatch, profile):
    monkeypatch.setattr(api, "_fetch_json", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(api, "_rights_holder_counts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(api, "_order_total", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(api, "_infraction_total", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(api, "_latest_site_warning", lambda *_args, **_kwargs: {"system_warning": "Latest"})
    rows = [{"user_id": "seller", "site_id": "MLM"}]
    api.enrich_reputation_with_official_data(rows, "token")
    assert rows[0]["is_banned"] is None
    assert rows[0]["account_status"] == "unknown"
    assert rows[0]["system_warning"] == "Latest"


def test_global_restriction_cannot_override_local_normal():
    local = api._site_status({"status": {"sell": {"allow": True}}})
    parent = api._site_status({"status": {"sell": {"allow": False}}})
    result = api._merge_site_status(local, parent)
    assert result["is_banned"] is False
    assert result["account_status"] == "normal"


def test_warning_failure_does_not_change_ban_or_order_counts(monkeypatch):
    calls = []
    def fetch(_token, path, **kwargs):
        calls.append(path)
        if path.startswith("/users/"):
            return {"id": "seller", "site_id": "MLM", "status": {"sell": {"allow": False}}}
        raise api.MercadoReputationError("forbidden", status_code=403)
    monkeypatch.setattr(api, "_fetch_json", fetch)
    monkeypatch.setattr(api, "_rights_holder_counts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(api, "_order_total", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(api, "_infraction_total", lambda *_args, **_kwargs: 1)
    rows = [{"user_id": "seller", "site_id": "MLM"}]
    api.enrich_reputation_with_official_data(rows, "token", global_user_id="parent")
    assert "/users/parent" not in calls
    assert rows[0]["is_banned"] is True
    assert rows[0]["orders_current_7d"] == 7
    assert rows[0]["system_warning"] is None
    assert rows[0]["system_warning_status"] == "failed"
    assert any("系统告警" in value for value in rows[0]["official_api_errors"])


def test_site_warning_is_persisted_without_status_or_order_errors():
    row = collector._api_reputation_database_row("Store", {
        "system_warning": "2026-10-01T01:00:00Z\nFull warning",
        "site_status_display": "已封禁", "official_api_errors": ["订单读取失败"],
    }, "now")
    assert row[9] == "2026-10-01T01:00:00Z\nFull warning"
    assert row[12] == "已封禁"


def test_persistence_preserves_latest_known_api_notice_and_marks_failures():
    latest = "2026-10-01T09:00:00Z\nComplete warning"
    older = "2025-01-01T09:00:00Z\nOlder warning"
    merge = bit_mysql._merge_reputation_api_warning
    assert merge(older, latest) == latest
    assert merge("暂无 API 告警（仅限接口可获取记录）", latest) == latest
    failed = merge(None, latest)
    assert failed == "告警 API 读取失败，以下为上次记录：\n" + latest
    assert merge(None, failed) == failed
    assert merge(latest, failed) == latest
    assert merge(None, None) == "告警 API 暂无法获取"


def test_browser_dispatch_endpoint_is_disabled(monkeypatch):
    from bit import bit_interface as web
    user = {"id": 17, "username": "admin", "role_key": "super_admin", "permissions": ["*"],
            "access_version": 1, "is_active": True, "is_platform_admin": True}
    monkeypatch.setattr(web, "get_current_workbench_user", lambda: user)
    monkeypatch.setattr(web, "get_workbench_user", lambda **_kwargs: user)
    monkeypatch.setattr(web, "_parse_collection_request", lambda *_args, **_kwargs: pytest.fail("不得派发浏览器任务"))
    client = web.app.test_client()
    with client.session_transaction() as session:
        session["workbench_user"] = user
    response = client.post("/api/reputation/browser-sync", json={"execution_target": "server", "shops": ["Store"]})
    assert response.status_code == 410
    assert "纯 API" in response.json["message"]


def test_legacy_browser_collector_is_disabled():
    from bit import reputation_browser_sync
    with pytest.raises(RuntimeError, match="已停用"):
        reputation_browser_sync.collect([])


def test_reputation_ui_has_no_browser_sync_or_store_notice_merge():
    source = (Path(__file__).parents[1] / "bit/templates/index.html").read_text()
    assert 'id="reputation-browser-sync-btn"' not in source
    assert "runReputationBrowserSync" not in source
    body = source.split("function reputationWarningText(row) {", 1)[1].split("\n        }", 1)[0]
    assert "reputationStoreNotices" not in body
    assert 'row["系统告警"]' in body
