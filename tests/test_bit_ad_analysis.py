from pathlib import Path

import pytest

from bit import bit_ad_analysis as analysis
from mercado_api.client import MercadoLibreClient


class FakeAdsClient:
    def get_product_ads_advertisers(self):
        return [{
            "advertiser_id": 17,
            "site_id": "MLM",
            "advertiser_name": "Seller Ads",
            "account_name": "MLM - Seller",
        }]

    def search_product_ads_campaigns(self, site, advertiser, *, limit, offset):
        assert (site, advertiser) == ("MLM", 17)
        rows = [{
            "id": 41,
            "name": "主推活动",
            "status": "active",
            "budget": 500,
            "currency_id": "MXN",
        }] if offset == 0 else []
        return {"results": rows, "paging": {"total": 1}}

    def search_product_ads_ad_groups_metrics(self, site, advertiser, **params):
        assert (site, advertiser) == ("MLM", 17)
        assert params["campaign_id"] == 41
        assert params["date_from"] == "2026-09-01"
        assert params["date_to"] == "2026-09-17"
        if params["offset"]:
            return {"results": [], "paging": {"total": 2}}
        return {
            "results": [
                {
                    "id": 31,
                    "campaign_id": 41,
                    "ad_group_type": "ITEM",
                    "ad_group_external_id": "MLM123",
                    "title": "传统商品",
                    "status": "ACTIVE",
                    "metrics": {"cost": 20, "total_amount": 100, "clicks": 10, "prints": 1000, "units_quantity": 2},
                },
                {
                    "id": 32,
                    "campaign_id": 41,
                    "ad_group_type": "FAMILY",
                    "ad_group_external_id": "family-1",
                    "title": "多变体商品",
                    "status": "ACTIVE",
                    "metrics": {"cost": 30, "total_amount": 90, "clicks": 15, "prints": 500, "units_quantity": 3},
                },
            ],
            "paging": {"total": 2},
            "metrics_summary": {"cost": 50, "total_amount": 190, "clicks": 25, "prints": 1500, "units_quantity": 5},
        }

    def list_product_ads_ad_group_ads(self, site, group, **params):
        assert (site, group) == ("MLM", 32)
        return {
            "results": [{
                "item_id": "MLM456",
                "title": "蓝色变体",
                "status": "active",
                "metrics": {"cost": 30, "total_amount": 90, "clicks": 15, "prints": 500, "units_quantity": 3},
            }],
            "paging": {"total": 1},
        }


class FakeAdActionClient:
    def __init__(self):
        self.calls = []

    def update_product_ads_ad_group(self, site, ad_group_id, campaign_id, status):
        self.calls.append((site, ad_group_id, campaign_id, status))
        return {"id": ad_group_id, "status": status}


def _install_token_fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        analysis, "AD_ANALYSIS_STATE_PATH", tmp_path / "ad-analysis.json"
    )
    monkeypatch.setattr(
        "bit.bit_mysql.list_mercado_store_tokens",
        lambda: {"rows": [{
            "id": 7,
            "enabled": True,
            "site_settings": [{
                "site_id": "MLM",
                "salesperson": "业务员甲",
                "group_name": "重点店群",
            }],
        }]},
    )
    monkeypatch.setattr(
        "bit.bit_mysql.get_mercado_store_token",
        lambda token_id: {"id": token_id, "display_name": "测试店铺", "access_token": "token"},
    )
    monkeypatch.setattr(
        "bit.bit_store_link_sync._client_and_token",
        lambda token: (FakeAdsClient(), token),
    )
    analysis._cache.clear()


def test_collect_ad_analysis_includes_accounts_item_and_family_links(monkeypatch, tmp_path):
    _install_token_fakes(monkeypatch, tmp_path)

    result = analysis.collect_ad_analysis(
        date_from="2026-09-01", date_to="2026-09-17", force=True
    )

    assert result["summary"]["account_count"] == 1
    assert result["summary"]["link_count"] == 2
    assert result["accounts"][0]["metrics"]["cost"] == 50
    assert result["accounts"][0]["metrics"]["roas"] == pytest.approx(3.8)
    assert {row["item_id"] for row in result["links"]} == {"MLM123", "MLM456"}
    family = next(row for row in result["links"] if row["item_id"] == "MLM456")
    assert family["ad_group_type"] == "FAMILY"
    assert family["campaign_name"] == "主推活动"
    assert family["permalink"].endswith("/MLM-456-_JM")
    assert result["summary"]["currencies"][0]["currency_id"] == "MXN"
    assert result["accounts"][0]["salesperson"] == "业务员甲"
    assert result["links"][0]["group_name"] == "重点店群"


def test_collect_ad_analysis_respects_explicit_empty_token_scope(monkeypatch, tmp_path):
    _install_token_fakes(monkeypatch, tmp_path)

    result = analysis.collect_ad_analysis(
        date_from="2026-09-01", date_to="2026-09-17", token_ids=[], force=True
    )

    assert result["accounts"] == []
    assert result["links"] == []


def test_default_load_returns_last_persisted_snapshot_without_live_calls(monkeypatch, tmp_path):
    _install_token_fakes(monkeypatch, tmp_path)
    refreshed = analysis.collect_ad_analysis(
        date_from="2026-09-01", date_to="2026-09-17", force=True
    )
    monkeypatch.setattr(
        "bit.bit_mysql.list_mercado_store_tokens",
        lambda: pytest.fail("default snapshot load must not query stores"),
    )

    result = analysis.collect_ad_analysis()

    assert result["cached"] is True
    assert result["snapshot_available"] is True
    assert result["generated_at"] == refreshed["generated_at"]
    assert result["summary"]["link_count"] == 2


def test_selected_store_refresh_merges_into_existing_full_snapshot(monkeypatch, tmp_path):
    _install_token_fakes(monkeypatch, tmp_path)
    analysis._persist_snapshot({
        "date_from": "2026-09-01",
        "date_to": "2026-09-17",
        "generated_at": "2026-09-17 08:00:00",
        "accounts": [{"token_id": 8, "store_name": "保留店铺", "currency_id": "MXN", "metrics": {"cost": 3}}],
        "links": [{"token_id": 8, "site_id": "MLM", "item_id": "MLM888", "currency_id": "MXN", "metrics": {"cost": 3}}],
        "errors": [],
        "summary": {},
        "snapshot_available": True,
    })

    result = analysis.collect_ad_analysis(
        date_from="2026-09-01",
        date_to="2026-09-17",
        refresh_token_ids=[7],
        force=True,
    )

    assert {row["token_id"] for row in result["accounts"]} == {7, 8}
    assert {row["item_id"] for row in result["links"]} == {"MLM123", "MLM456", "MLM888"}
    assert result["refreshed_token_ids"] == [7]


def test_ad_analysis_rejects_more_than_ninety_days():
    with pytest.raises(ValueError, match="90 天"):
        analysis._date_range(date_from="2026-01-01", date_to="2026-04-01")


def test_update_product_ads_ad_groups_deduplicates_catalog_rows(monkeypatch):
    client = FakeAdActionClient()
    monkeypatch.setattr(
        "bit.bit_mysql.get_mercado_store_token",
        lambda token_id: {"id": token_id, "access_token": "token"},
    )
    monkeypatch.setattr(
        "bit.bit_store_link_sync._client_and_token",
        lambda token: (client, token),
    )

    result = analysis.update_product_ads_ad_groups(
        [
            {"token_id": 7, "site_id": "MLM", "ad_group_id": 31, "campaign_id": 41, "item_id": "MLM123"},
            {"token_id": 7, "site_id": "MLM", "ad_group_id": 31, "campaign_id": 41, "item_id": "MLM456"},
            {"token_id": 7, "site_id": "MLM", "ad_group_id": 32, "campaign_id": 41, "item_id": "MLM789"},
        ],
        status="paused",
    )

    assert result["requested_count"] == 3
    assert result["ad_group_count"] == 2
    assert result["success_count"] == 2
    assert client.calls == [
        ("MLM", 31, 41, "paused"),
        ("MLM", 32, 41, "paused"),
    ]


def test_product_ads_metric_client_uses_current_ad_group_endpoints(monkeypatch):
    calls = []
    client = MercadoLibreClient("token")
    monkeypatch.setattr(
        client,
        "request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {"results": []},
    )

    client.search_product_ads_ad_groups_metrics(
        "MLM", 17, date_from="2026-09-01", date_to="2026-09-17",
        metrics=("clicks", "cost"), limit=800, offset=0,
    )
    client.list_product_ads_ad_group_ads(
        "MLM", 31, date_from="2026-09-01", date_to="2026-09-17",
        metrics=("clicks", "cost"), limit=50, offset=0,
    )

    assert calls[0][1].endswith("/advertisers/17/product_ads/ad_groups/search")
    assert calls[0][2]["params"]["metrics_summary"] == "true"
    assert calls[1][1].endswith("/product_ads/ad_groups/31/ads")
    assert calls[1][2]["params"]["metrics"] == "clicks,cost"


def test_product_ads_ad_group_client_updates_requested_status(monkeypatch):
    calls = []
    client = MercadoLibreClient("token")
    monkeypatch.setattr(
        client,
        "request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {"status": "paused"},
    )

    client.update_product_ads_ad_group("MLM", 31, 41, "paused")

    assert calls[0][0] == "PUT"
    assert calls[0][1].endswith("/product_ads/ad_groups/31")
    assert calls[0][2]["json_body"] == {"status": "paused", "campaign_id": 41}


def test_ad_group_metric_client_can_filter_one_campaign(monkeypatch):
    calls = []
    client = MercadoLibreClient("token")
    monkeypatch.setattr(
        client,
        "request",
        lambda method, path, **kwargs: calls.append(kwargs) or {"results": []},
    )

    client.search_product_ads_ad_groups_metrics(
        "MLM", 17, date_from="2026-09-01", date_to="2026-09-17",
        metrics=("clicks",), campaign_id=41,
    )

    assert calls[0]["params"]["filters[campaign_id]"] == 41


def test_ad_analysis_module_is_present_in_workbench_template():
    source = Path("bit/templates/index.html").read_text(encoding="utf-8")
    assert 'data-tab="ad-analysis" data-icon="◎" data-permission="ad_analysis.view"' in source
    assert 'id="tab-ad-analysis"' in source
    assert 'id="ad-analysis-account-body"' in source
    assert 'id="ad-analysis-link-body"' in source
    assert 'id="ad-analysis-activate-selected"' in source
    assert 'id="ad-analysis-pause-selected"' in source
    assert 'id="ad-analysis-salesperson"' in source
    assert 'id="ad-analysis-group"' in source
    assert 'id="ad-analysis-store"' in source
    assert "默认展示上次更新的全部数据" in source
    assert 'query.append("refresh_token_ids"' in source
    assert "loadAdAnalysis" in source
