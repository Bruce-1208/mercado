from bit import bit_store_link_ads as ads
from mercado_api.client import MercadoLibreClient


def test_advertise_store_link_creates_campaign_and_activates_group(monkeypatch):
    calls = []

    class FakeClient:
        def get_product_ads_advertisers(self):
            return [{"advertiser_id": 17, "site_id": "MLM"}]

        def search_product_ads_ad_groups(self, site, advertiser, item):
            assert (site, advertiser, item) == ("MLM", 17, "MLM123")
            return {"results": [{"id": 31, "campaign_id": 0, "ad_group_type": "ITEM", "ad_group_external_id": "MLM123"}]}

        def search_product_ads_campaigns(self, site, advertiser, **paging):
            return {"results": [], "paging": {"total": 0}}

        def create_product_ads_campaign(self, site, advertiser, payload):
            calls.append(("create", site, advertiser, payload))
            return {"id": 41, "currency_id": "USD"}

        def activate_product_ads_ad_group(self, site, ad_group, campaign):
            calls.append(("activate", site, ad_group, campaign))
            return {"status": "active"}

    monkeypatch.setattr(
        "erp.mercadolibre_store_link_store.get_store_links_by_ids",
        lambda _ids: [{"id": 1, "token_id": 2, "status": "active", "site_id": "MLM", "item_id": "MLM123"}],
    )
    monkeypatch.setattr("bit.bit_mysql.get_mercado_store_token", lambda _id: {"id": 2, "access_token": "token"})
    monkeypatch.setattr("bit.bit_store_link_sync._client_and_token", lambda token: (FakeClient(), token))

    result = ads.advertise_store_link(1, budget=10, roas_target=5)

    assert result["campaign_id"] == 41
    assert result["campaign_created"] is True
    assert result["ad_group_id"] == 31
    assert calls[0][3]["budget"] == 10
    assert calls[0][3]["roas_target"] == 5
    assert calls[1] == ("activate", "MLM", 31, 41)


def test_advertise_store_link_reuses_named_campaign(monkeypatch):
    calls = []

    class FakeClient:
        def get_product_ads_advertisers(self):
            return [{"advertiser_id": 17, "site_id": "MLB"}]

        def search_product_ads_ad_groups(self, *_args):
            return {"results": [{"id": 31, "campaign_id": 9, "ad_group_type": "FAMILY"}]}

        def search_product_ads_campaigns(self, *_args, **_kwargs):
            return {"results": [{"id": 41, "name": "主推", "status": "paused"}], "paging": {"total": 1}}

        def update_product_ads_campaign(self, site, campaign, payload):
            calls.append(("update", site, campaign, payload))
            return {"id": campaign, "currency_id": "USD"}

        def activate_product_ads_ad_group(self, site, ad_group, campaign):
            calls.append(("activate", site, ad_group, campaign))
            return {}

    monkeypatch.setattr(
        "erp.mercadolibre_store_link_store.get_store_links_by_ids",
        lambda _ids: [{"id": 1, "token_id": 2, "status": "active", "site_id": "MLB", "item_id": "MLB123"}],
    )
    monkeypatch.setattr("bit.bit_mysql.get_mercado_store_token", lambda _id: {"id": 2})
    monkeypatch.setattr("bit.bit_store_link_sync._client_and_token", lambda token: (FakeClient(), token))

    result = ads.advertise_store_link(1, budget="20.5", roas_target="7", campaign_name="主推")

    assert result["campaign_created"] is False
    assert result["previous_campaign_id"] == 9
    assert calls[0][0:3] == ("update", "MLB", 41)
    assert "channel" not in calls[0][3]
    assert calls[1] == ("activate", "MLB", 31, 41)


def test_advertise_store_link_validates_roas_before_external_calls():
    try:
        ads.advertise_store_link(1, budget=10, roas_target=36)
    except ValueError as exc:
        assert "ROAS" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_product_ads_client_uses_current_global_selling_endpoints(monkeypatch):
    calls = []
    client = MercadoLibreClient("token")

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/advertising/advertisers":
            return {"advertisers": []}
        if path.endswith("/campaigns/search") or path.endswith("/ad_groups/search"):
            return {"results": []}
        return {"id": 1}

    monkeypatch.setattr(client, "request", request)
    client.get_product_ads_advertisers()
    client.search_product_ads_campaigns("MLM", 7)
    client.create_product_ads_campaign("MLM", 7, {"name": "测试"})
    client.update_product_ads_campaign("MLM", 8, {"budget": 10})
    client.search_product_ads_ad_groups("MLM", 7, "MLM123")
    client.activate_product_ads_ad_group("MLM", 9, 8)

    assert calls[0][1] == "/advertising/advertisers"
    assert calls[0][2]["params"] == {"product_id": "PADS"}
    assert calls[1][1].endswith("/advertisers/7/product_ads/campaigns/search")
    assert calls[2][0:2] == (
        "POST", "/marketplace/advertising/MLM/advertisers/7/product_ads/campaigns"
    )
    assert calls[3][0:2] == (
        "PUT", "/marketplace/advertising/MLM/product_ads/campaigns/8"
    )
    assert calls[4][2]["params"] == {"filters[item_ids]": "MLM123"}
    assert calls[5][0:2] == (
        "PUT", "/marketplace/advertising/MLM/product_ads/ad_groups/9"
    )
