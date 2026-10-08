from datetime import datetime, timedelta, timezone

import pytest

from bit import bit_mysql
from bit import mercado_reputation as api


@pytest.mark.parametrize("active, expected", [(True, "active"), (False, None)])
def test_second_listing_page_failure_retains_positive_evidence(monkeypatch, active, expected):
    def fetch(_token, path, *, params, **_kwargs):
        if path.endswith("/search"):
            if params["offset"]:
                raise api.MercadoReputationError("第二页不可用", status_code=403)
            return {"results": [f"MLM{i}" for i in range(50)], "paging": {"total": 500}}
        assert path == "/items"
        return [
            {"status_code": 200, "id": value,
             "body": {"status": "active" if active and value == "MLM0" else "paused"}}
            for value in params["ids"].split(",")
        ]

    monkeypatch.setattr(api, "_fetch_json", fetch)
    sample = api._sample_site_listing_status("token", "site-seller", http=None, timeout=5, sample_size=100)

    assert sample["listing_sample_status"] == expected
    assert sample["listing_sample_count"] == 50
    assert sample["listing_sample_expected_count"] == 100
    assert sample["listing_sample_error"] == "第二页不可用"


def test_incomplete_listing_data_preserves_explicit_restriction():
    row = {"account_status": "restricted", "site_status_display": "永久封禁",
           "suspension_days": 10, "suspension_until": "2027-01-01"}

    api._apply_listing_sample_status(row, {"listing_sample_status": None})

    assert row["account_status"] == "restricted"
    assert row["site_status_display"] == "永久封禁"
    assert row["site_status_official_display"] == "永久封禁"
    assert row["site_status_official_account_status"] == "restricted"
    assert row["suspension_days"] == 10
    assert row["suspension_until"] == "2027-01-01"


@pytest.mark.parametrize("active, expected", [(True, "active"), (False, None)])
def test_malformed_second_listing_page_retains_positive_evidence(monkeypatch, active, expected):
    def fetch(_token, path, *, params, **_kwargs):
        if path.endswith("/search"):
            if params["offset"]:
                return {"results": None}
            return {"results": [f"MLM{i}" for i in range(50)], "paging": {"total": 500}}
        return [{"id": item, "status": "active" if active and item == "MLM0" else "paused"}
                for item in params["ids"].split(",")]
    monkeypatch.setattr(api, "_fetch_json", fetch)
    sample = api._sample_site_listing_status("token", "site-seller", http=None, timeout=5, sample_size=100)
    assert sample["listing_sample_status"] == expected
    assert sample["listing_sample_count"] == 50
    assert sample["listing_sample_error"] == "站点最新刊登接口返回格式错误"


@pytest.mark.parametrize("payload", [{}, {"paging": {"total": "unknown"}},
                                     {"paging": {"total": -1}}])
def test_invalid_order_total_is_not_reported_as_zero(monkeypatch, payload):
    monkeypatch.setattr(api, "_fetch_json", lambda *_args, **_kwargs: payload)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with pytest.raises(api.MercadoReputationError, match="有效订单总数"):
        api._order_total("token", "seller", "MLM", now - timedelta(days=7), now,
                         http=None, timeout=5)


@pytest.mark.parametrize("failed_period, expected_current, expected_previous", [
    ("previous", 17, None), ("current", None, 9),
])
def test_seven_day_count_is_independent_of_comparison_period(
    monkeypatch, failed_period, expected_current, expected_previous,
):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    calls = []

    def count(_token, seller, site, start, end, **_kwargs):
        assert (seller, site) == ("site-seller", "MLM")
        period = "current" if end == now else "previous"
        calls.append((period, start, end))
        if period == failed_period:
            raise api.MercadoReputationError("订单暂不可用", status_code=403)
        return 17 if period == "current" else 9

    monkeypatch.setattr(api, "_fetch_json", lambda *_args, **_kwargs: {"status": {"site_status": "active"}})
    monkeypatch.setattr(api, "_order_total", count)
    monkeypatch.setattr(api, "_rights_holder_counts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(api, "_infraction_total", lambda *_args, **_kwargs: 0)
    rows = [{"user_id": "site-seller", "site_id": "MLM"}]

    api.enrich_reputation_with_official_data(rows, "token", now=now)

    assert rows[0]["orders_current_7d"] == expected_current
    assert rows[0]["orders_previous_7d"] == expected_previous
    assert rows[0]["gradient_rate_percent"] is None
    assert calls == [
        ("previous", now - timedelta(days=14), now - timedelta(days=7)),
        ("current", now - timedelta(days=7), now),
    ]


@pytest.mark.parametrize("current_orders", [None, 0, 23])
def test_reputation_snapshot_persists_site_order_count_and_keeps_other_sites(monkeypatch, current_orders):
    inserted = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def executemany(self, sql, rows):
            assert "七天单量" in sql
            assert sql.count("%s") == 17
            inserted.extend(rows)

    class Connection:
        def cursor(self): return Cursor()
        def commit(self): pass
        def rollback(self): pytest.fail("保存声誉数据不应回滚")
        def close(self): pass

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **_kwargs: Connection())
    monkeypatch.setattr(bit_mysql, "_ensure_column", lambda *_args: None)
    monkeypatch.setattr(bit_mysql, "_next_collection_submit_time", lambda *_args: "new-snapshot")
    monkeypatch.setattr(bit_mysql, "_record_collection_snapshot", lambda *_args: None)
    monkeypatch.setattr(bit_mysql, "_active_collection_snapshot_rows", lambda rows, **_kwargs: rows)
    monkeypatch.setattr(bit_mysql, "_latest_reputation_snapshot_rows", lambda *_args: [
        {"店铺名": "店铺", "站点": "墨西哥", "七天单量": 99, "系统告警": "上次告警"},
        {"店铺名": "店铺", "站点": "巴西", "七天单量": 8},
    ])
    collected = ["店铺", "墨西哥", "绿色", 100, "0%", "0%", "0%", "持平", "0%",
                 None, "now", "[]", "正常", 0, 0]
    if current_orders is not None:
        collected.append(current_orders)

    result = bit_mysql.inset_reputation_info([collected], merge_latest=True)

    assert result == 2
    rows = {row[1]: row for row in inserted}
    assert rows["墨西哥"][16] == current_orders
    assert rows["巴西"][16] == 8
    assert rows["墨西哥"][9] == "告警 API 读取失败，以下为上次记录：\n上次告警"
    assert all(len(row) == 17 and row[15] == "new-snapshot" for row in inserted)


@pytest.mark.parametrize("sample,display", [
    ({"listing_sample_status": "active", "listing_sample_error": "部分读取失败"}, "有在售商品（账号权限待确认）"),
    ({"listing_sample_status": "inactive"}, "待确认（抽样未见在售商品）"),
    ({"listing_sample_status": "inactive", "listing_sample_expected_count": 0}, "待确认（暂无商品）"),
    ({"listing_sample_status": None, "listing_sample_error": "部分读取失败"}, "待确认（刊登读取异常）"),
    ({}, "待确认（数据不足）"),
])
def test_account_status_requires_evidence_and_preserves_diagnostics(sample, display):
    row = {}
    api._apply_listing_sample_status(row, sample)
    assert row["site_status_display"] == display
    assert row.get("listing_sample_error") == sample.get("listing_sample_error")


def test_latest_fifty_uses_multiget_endpoint_and_detects_last_active_item(monkeypatch):
    detail_batches = []

    def fetch(_token, path, *, params, **_kwargs):
        if path.endswith("/search"):
            assert params == {"limit": 50, "offset": 0, "orders": "start_time_desc"}
            return {"results": [f"MLM{i}" for i in range(50)], "paging": {"total": 200}}
        assert path == "/items"
        ids = params["ids"].split(",")
        detail_batches.append(len(ids))
        return [{"code": 200, "body": {"id": item, "status": "active" if item == "MLM49" else "paused"}}
                for item in ids]

    monkeypatch.setattr(api, "_fetch_json", fetch)
    sample = api._sample_site_listing_status("token", "seller", http=None, timeout=5)
    row = {}
    api._apply_listing_sample_status(row, sample)
    assert detail_batches == [20, 20, 10]
    assert row["site_status_display"] == "有在售商品（账号权限待确认）"


@pytest.mark.parametrize("permissions, expected", [
    ({"sell": {"allow": False}}, "restricted"),
    ({"list": {"allow": False}}, "unknown"),
    ({"sell": {"allow": True}, "list": {"allow": True}}, "normal"),
])
@pytest.mark.parametrize("sample_status", ["active", "inactive", None])
def test_explicit_permissions_override_listing_samples(permissions, expected, sample_status):
    row = api._site_status({"status": {"site_status": "active", **permissions}})
    original_display = row["site_status_display"]
    api._apply_listing_sample_status(row, {"listing_sample_status": sample_status})
    assert row["account_status"] == expected
    assert row["site_status_display"] == original_display


def test_orders_and_public_active_do_not_prove_current_account_health():
    row = api._site_status({"status": {"site_status": "active"}})
    row["orders_current_7d"] = 100
    api._apply_listing_sample_status(row, {})
    assert row["account_status"] == "unknown"
    assert row["site_status_display"] == "待确认（数据不足）"
