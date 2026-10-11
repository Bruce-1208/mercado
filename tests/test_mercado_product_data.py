import concurrent.futures
import time

import pytest

from bit.mercado_product_data import DataError, ProductDataService, estimate, normalize

ROW = {"Sku":"MLM6226808220", "Weight":1300, "Size":[25,33,3], "Orders":0,
       "Fee":20, "Storagetype":1, "SellerAddressCountry":"CN"}


def provider(calls):
    def call(msg):
        calls.append(msg)
        if msg["action"] == "products":
            time.sleep(.02)
            return [ROW], None
        if msg["action"] == "freight":
            return [{"abbr":"MLM","start":0,"ended":500,"freight":8.15,"below_freight":5},
                    {"abbr":"MLM","start":500,"ended":2000,"freight":12.5,"below_freight":9}], None
        return {"USD":670.585,"MXN":35}, None
    return call


def test_shared_cache_coalesces_across_service_instances_and_retains_zero(tmp_path):
    calls = []
    services = [ProductDataService(tmp_path, provider(calls)) for _ in range(4)]
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda s:s.query([{"sku":ROW["Sku"],"price":900}]), services))
    assert len([c for c in calls if c["action"] == "products"]) == 1
    assert sum(r["items"][0]["cached"] for r in results) == 3
    info = results[0]["items"][0]["product"]
    assert info["sales"] == 0
    assert info["volumetric_weight_g"] == 413
    assert info["weight_freight_usd"] == 12.5
    assert info["size_freight_usd"] == 8.15
    assert info["commission_percent"] == 20
    assert info["estimate_commission_percent"] == 15
    assert info["net_proceeds_usd"] is not None


@pytest.mark.parametrize("code", [401,403,429,502])
def test_upstream_failure_cooldown_persists_without_customer_401(tmp_path, code):
    calls = []
    def call(msg):
        calls.append(msg)
        return None, code
    service = ProductDataService(tmp_path, call)
    for _ in range(2):
        with pytest.raises(DataError) as error:
            service.query([{"sku":ROW["Sku"]}])
        assert error.value.status == 503
    assert len(calls) == (2 if code == 401 else 1)


def test_missing_measurements_and_fees_are_not_zero_or_invented():
    info = normalize({**ROW,"Weight":0,"Size":[25,None,3],"Orders":None,"Storagetype":99})
    assert info["weight_g"] is None and info["size_cm"] is None
    assert info["sales"] is None and not info["fulfillment_eligible"]
    result = estimate(info, 900, [], {})
    assert result["net_proceeds_usd"] is None
    assert not normalize({**ROW,"SellerAddressCountry":"US"})["fulfillment_eligible"]


def test_missing_record_negative_cache_and_bad_input(tmp_path):
    calls = []
    service = ProductDataService(tmp_path, provider(calls))
    for _ in range(2):
        assert service.query([{"sku":"MLM999999999"}])["items"][0]["product"] is None
    assert len([c for c in calls if c["action"] == "products"]) == 1
    for invalid in [None, [], ["x"], [{"sku":"https://evil.test"}], [{"sku":"MLMU12345"}], [{}]*51]:
        with pytest.raises(DataError) as error:
            service.query(invalid)
        assert error.value.status == 400


def test_price_changes_recalculate_without_upstream_query(tmp_path):
    calls = []
    service = ProductDataService(tmp_path, provider(calls))
    high = service.query([{"sku":ROW["Sku"],"price":900}])["items"][0]["product"]
    low = service.query([{"sku":ROW["Sku"],"price":200}])["items"][0]["product"]
    assert high["weight_freight_usd"] == 12.5
    assert low["weight_freight_usd"] == 9
    assert low["net_proceeds_usd"] == 3
    assert len([c for c in calls if c["action"] == "products"]) == 1


def test_expired_product_refreshes(tmp_path):
    calls=[]
    service=ProductDataService(tmp_path, provider(calls))
    service.query([{"sku":ROW["Sku"]}])
    with service.database() as db:
        db.execute("UPDATE cache SET expires=0 WHERE key LIKE 'item:%'")
    assert not service.query([{"sku":ROW["Sku"]}])["items"][0]["cached"]
    assert len([c for c in calls if c["action"] == "products"]) == 2


def test_same_sku_different_prices_share_lookup_but_not_estimate(tmp_path):
    calls=[]
    service=ProductDataService(tmp_path, provider(calls))
    rows=service.query([{"sku":ROW["Sku"],"price":900},{"sku":ROW["Sku"],"price":200}])["items"]
    assert [r["requested_price"] for r in rows] == [900,200]
    assert [r["product"]["weight_freight_usd"] for r in rows] == [12.5,9]
    assert len([c for c in calls if c["action"] == "products"]) == 1


def test_display_rates_share_cache_and_refresh_after_expiry(tmp_path):
    calls = []
    service = ProductDataService(tmp_path, provider(calls))
    snapshot = service.exchange_rates()
    assert snapshot["rates_to_usd"]["MXN"] == pytest.approx(35 / 670.585)
    assert snapshot["rates_to_usd"]["USD"] == 1
    assert "ARS" not in snapshot["rates_to_usd"]
    assert snapshot["expires_at"] - snapshot["fetched_at"] == 1800
    assert service.exchange_rates() == snapshot
    assert len(calls) == 1
    with service.database() as db:
        db.execute("UPDATE cache SET expires=0 WHERE key='meta:exchange'")
    service.exchange_rates()
    assert len(calls) == 2


@pytest.mark.parametrize("rates", [{}, {"USD":0}, {"USD":float("nan")}, None])
def test_invalid_display_rates_are_not_used(tmp_path, rates):
    service = ProductDataService(tmp_path, lambda msg:(rates, None))
    with pytest.raises(DataError):
        service.exchange_rates()
