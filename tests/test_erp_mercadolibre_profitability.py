from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from erp import mercadolibre_profitability as profitability
from erp.mercadolibre_profitability import (
    MercadoProfitabilityClient,
    actual_weight_from_row,
    calculate_billable_weight_g,
    calculate_net_proceeds_usd,
    enrich_profitability,
    estimate_rate_card_shipping,
    refresh_supported_exchange_rates,
    shipping_dimensions_parameter,
)


class _MemoryProfitabilityCache:
    def __init__(self):
        self.exchange = {}
        self.commissions = {}
        self.shipping = {}

    @staticmethod
    def _key(quote):
        return tuple(sorted(quote.items()))

    def get_exchange_rate(self, source, target):
        return self.exchange.get((source, target))

    def put_exchange_rate(self, source, target, value):
        self.exchange[(source, target)] = {
            "rate": value["ratio"],
            "source_created_at": value.get("creation_date"),
            "source_valid_until": value.get("valid_until"),
        }

    def get_commission(self, **quote):
        return self.commissions.get(self._key(quote))

    def put_commission(self, quote, value):
        self.commissions[self._key(quote)] = dict(value)

    def get_shipping(self, **quote):
        return self.shipping.get(self._key(quote))

    def put_shipping(self, quote, value):
        self.shipping[self._key(quote)] = dict(value)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class _OfficialApi:
    def __init__(self, *, single_listing_price=False):
        self.calls = []
        self.single_listing_price = single_listing_price

    def get(self, url, *, headers, params, timeout):
        self.calls.append((url, params))
        if "/marketplace/users/" in url:
            return _Response({
                "marketplaces": [{
                    "site_id": "MLM",
                    "user_id": 222,
                    "logistic_type": "remote",
                }]
            })
        if "/domain_discovery/search" in url:
            return _Response([{
                "category_id": "MLM455455",
                "category_name": "Kits de Mochilas Escolares",
            }])
        if "/currency_conversions/search" in url:
            return _Response({
                "ratio": 0.05,
                "creation_date": "2026-08-23T00:00:00.000+00:00",
            })
        if "/listing_prices" in url:
            quote = {
                "listing_type_id": "gold_special",
                "listing_type_name": "Classic",
                "currency_id": "MXN",
                "sale_fee_amount": 70,
                "sale_fee_details": {"percentage_fee": 20},
            }
            return _Response(quote if self.single_listing_price else [quote])
        if "/items/" in url:
            return _Response({"shipping": {"free_shipping": True}})
        if "/shipping_options/free" in url:
            return _Response({
                "coverage": {
                    "all_country": {
                        "list_cost": 128 if params.get("free_shipping") == "true" else 64,
                        "currency_id": "MXN",
                        "billable_weight": 1000,
                    }
                }
            })
        raise AssertionError(url)


def test_billable_weight_uses_only_actual_weight_at_all_weights():
    assert calculate_billable_weight_g(200, 9) == 200
    assert calculate_billable_weight_g(500, 0.9) == 500
    assert shipping_dimensions_parameter({"weight_g": 500, "volumetric_weight_kg": 9}) == (
        "1x1x1,500"
    )


def test_billable_weight_ignores_volumetric_weight():
    assert calculate_billable_weight_g(500.1, 0.8) == 500.1
    assert calculate_billable_weight_g(900, 0.8) == 900
    assert calculate_billable_weight_g(None, 1.2) is None
    assert shipping_dimensions_parameter({
        "weight_g": 600,
        "volumetric_weight_kg": 1,
        "package_length_cm": 10,
        "package_width_cm": 20,
        "package_height_cm": 30,
    }) == "1x1x1,600"


def test_immediate_rate_card_match_uses_actual_weight_without_http():
    rate_rows = [
        {
            "site_id": "MLM",
            "rate_kind": "above_threshold",
            "weight_min_g": 500,
            "weight_max_g": 600,
            "shipping_amount_usd": 7.16,
        }
    ]
    row = {
        "source_item_id": "MLM3016972321",
        "price": 350,
        "currency_id": "MXN",
        "weight_g": 600,
        "volumetric_weight_kg": 9,
        "source_free_shipping": True,
    }

    result = estimate_rate_card_shipping(row, rate_rows=rate_rows)

    assert result["shipping_fee_usd"] == 7.16
    assert result["billable_weight_g"] == 600
    assert result["shipping_api_billable_weight_g"] == 600
    assert "actual_weight_only" in result["shipping_weight_rule"]
    assert actual_weight_from_row({
        **row,
        "weight_basis": "plugin_volumetric_fallback",
    }) is None
    assert estimate_rate_card_shipping(
        {**row, "weight_basis": "plugin_volumetric_fallback"},
        rate_rows=rate_rows,
    ) == {}


def test_immediate_rate_card_converts_usd_price_from_stored_daily_rate():
    rate_rows = [{
        "site_id": "MLM",
        "rate_kind": "above_threshold",
        "weight_min_g": 500,
        "weight_max_g": 600,
        "exchange_rate_to_usd": 0.05,
        "shipping_amount_usd": 7.16,
    }]

    result = estimate_rate_card_shipping({
        "source_item_id": "MLM3016972321",
        "price": 15,
        "currency_id": "USD",
        "weight_g": 600,
        "weight_basis": "plugin_actual",
    }, rate_rows=rate_rows)

    assert result["shipping_fee_usd"] == 7.16
    assert result["billable_weight_g"] == 600


def test_production_rate_card_miss_never_falls_back_to_dimensions_api():
    class EmptyRateStore:
        def match(self, **_quote):
            return None

    http = _OfficialApi()
    client = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
        cache_store=False,
        shipping_rate_store=EmptyRateStore(),
    )

    with pytest.raises(profitability.MercadoProfitabilityError, match="固定运费表未命中"):
        client.shipping(
            {},
            {
                "source_item_id": "MLM3016972321",
                "weight_g": 600,
                "weight_basis": "plugin_actual",
            },
            "MLM123",
            350,
            "gold_special",
            free_shipping=True,
        )

    assert not any("shipping_options/free" in url for url, _params in http.calls)


def test_shipping_dimensions_round_fractional_values_up_for_official_api():
    assert shipping_dimensions_parameter({
        "weight_g": 600,
        "volumetric_weight_kg": 1.3333,
        "package_length_cm": 20,
        "package_width_cm": 20,
        "package_height_cm": 20,
    }) == "1x1x1,600"
    assert shipping_dimensions_parameter({
        "weight_g": 312.5,
        "volumetric_weight_kg": 0.3125,
    }) == "1x1x1,313"


def test_official_estimate_calculates_usd_commission_shipping_and_net_proceeds():
    http = _OfficialApi()
    client = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
        cache_store=False,
    )
    result = client.estimate({
        "source_item_id": "MLM3016972321",
        "title": "Mochila escolar",
        "price": 350,
        "currency_id": "MXN",
        "weight_g": 600,
        "volumetric_weight_kg": 1,
        "package_length_cm": 10,
        "package_width_cm": 20,
        "package_height_cm": 30,
    })

    assert result["category_id"] == "MLM455455"
    assert result["listing_type_id"] == "gold_special"
    assert result["listing_type_name"] == "Classic"
    assert result["source_free_shipping"] is True
    assert result["sale_price_usd"] == 17.5
    assert result["commission_rate"] == 20
    assert result["commission_amount_usd"] == 3.5
    assert result["shipping_fee_usd"] == 6.4
    assert result["billable_weight_g"] == 600
    assert result["shipping_api_billable_weight_g"] == 600
    assert result["net_proceeds_usd"] == 7.6
    shipping_call = next(call for call in http.calls if "/shipping_options/free" in call[0])
    assert shipping_call[1]["dimensions"] == "1x1x1,600"
    assert shipping_call[1]["free_shipping"] == "true"
    listing_call = next(call for call in http.calls if "/listing_prices" in call[0])
    assert listing_call[1]["listing_type_id"] == "gold_special"


def test_non_free_shipping_uses_lower_buyer_paid_quote_and_keeps_classic():
    http = _OfficialApi()
    result = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
        cache_store=False,
    ).estimate({
        "source_item_id": "MLM3016972321",
        "title": "Mochila escolar",
        "price": 350,
        "currency_id": "MXN",
        "weight_g": 600,
        "volumetric_weight_kg": 1,
        "package_length_cm": 10,
        "package_width_cm": 20,
        "package_height_cm": 30,
        "source": {"shipping": {"free_shipping": False}},
        "listing_type_id": "gold_pro",
    })

    shipping_call = next(call for call in http.calls if "/shipping_options/free" in call[0])
    assert shipping_call[1]["free_shipping"] == "false"
    assert result["shipping_fee_local"] == 64
    assert result["shipping_fee_usd"] == 3.2
    assert result["net_proceeds_usd"] == 10.8
    assert result["listing_type_id"] == "gold_special"
    assert result["shipping_weight_rule"].startswith("buyer_pays_shipping:")


def test_global_selling_rate_card_uses_official_usd_without_local_conversion():
    class RateStore:
        def match(self, **quote):
            assert quote == {
                "site_id": "MLM",
                "price_local": 100,
                "billable_weight_g": 500,
                "free_shipping": False,
            }
            return {
                "shipping_amount_usd": 1.46,
                "currency_id": "MXN",
                "rate_kind": "below_threshold",
                "price_label": "Listings below MXN 299",
                "weight_label": "0.4 - 0.5",
                "refreshed_at": "2026-08-30 00:00:00",
            }

    http = _OfficialApi()
    client = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
        cache_store=False,
        shipping_rate_store=RateStore(),
    )
    result = client.estimate({
        "source_item_id": "MLM3016972321",
        "title": "Mochila escolar",
        "price": 100,
        "currency_id": "MXN",
        "weight_g": 500,
        "source_free_shipping": False,
    })

    assert result["shipping_fee_local"] == 1.46
    assert result["shipping_currency_id"] == "USD"
    assert result["shipping_fee_usd"] == 1.46
    assert result["shipping_weight_rule"].endswith(
        "official_global_selling_cainiao_rate_card"
    )
    assert result["profitability_source"] == (
        "mercadolibre_global_selling_cainiao_rate_card_daily_database_cache"
    )
    assert not any("shipping_options/free" in url for url, _params in http.calls)


def test_category_prediction_uses_captured_specs_when_cross_border_title_is_truncated():
    class Api(_OfficialApi):
        def get(self, url, *, headers, params, timeout):
            if "/domain_discovery/search" in url:
                self.calls.append((url, params))
                if params.get("q") == "cosplay anime":
                    return _Response([{
                        "category_id": "MLM455862",
                        "category_name": "Kits de Cosplay",
                    }])
                return _Response([])
            return super().get(url, headers=headers, params=params, timeout=timeout)

    http = Api()
    result = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
        cache_store=False,
    ).estimate({
        "source_item_id": "MLM2990352733",
        "title": "Anime Masculino Blue Lock Reo Nagi Bachira Isagi Chigiri Zip",
        "price": 224.93,
        "currency_id": "MXN",
        "weight_g": 338,
        "package_length_cm": 20,
        "package_width_cm": 20,
        "package_height_cm": 5,
        "page_snapshot_json": '{"specs":[{"name":"Cantidad de disfraces","value":"1"}]}',
        "source_free_shipping": False,
    })

    queries = [params["q"] for url, params in http.calls if "/domain_discovery/search" in url]
    assert queries == [
        "Anime Masculino Blue Lock Reo Nagi Bachira Isagi Chigiri Zip",
        "cosplay anime",
    ]
    assert result["category_id"] == "MLM455862"
    assert result["listing_type_id"] == "gold_special"


def test_concurrent_identical_category_predictions_share_one_api_call():
    class BlockingApi(_OfficialApi):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def get(self, url, *, headers, params, timeout):
            if "/domain_discovery/search" in url:
                self.entered.set()
                assert self.release.wait(5)
            return super().get(url, headers=headers, params=params, timeout=timeout)

    http = BlockingApi()
    client = MercadoProfitabilityClient(
        {}, http=http, cache_store=False, shipping_rate_store=False,
    )
    worker_count = 8
    start = threading.Barrier(worker_count + 1)

    def predict():
        start.wait(timeout=5)
        return client.category("MLM", "Mochila escolar")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(predict) for _ in range(worker_count)]
        start.wait(timeout=5)
        assert http.entered.wait(2)
        time.sleep(0.05)
        http.release.set()
        results = [future.result(timeout=5) for future in futures]

    assert all(result["category_id"] == "MLM455455" for result in results)
    assert sum(
        "/domain_discovery/search" in url for url, _params in http.calls
    ) == 1


def test_failed_category_singleflight_is_shared_then_can_retry():
    class FailingOnceApi(_OfficialApi):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.lock = threading.Lock()
            self.attempts = 0

        def get(self, url, *, headers, params, timeout):
            if "/domain_discovery/search" not in url:
                return super().get(
                    url, headers=headers, params=params, timeout=timeout,
                )
            with self.lock:
                self.attempts += 1
                attempt = self.attempts
            if attempt == 1:
                self.entered.set()
                assert self.release.wait(5)
                return _Response({"message": "temporary failure"}, status_code=503)
            return super().get(url, headers=headers, params=params, timeout=timeout)

    http = FailingOnceApi()
    client = MercadoProfitabilityClient(
        {}, http=http, cache_store=False, shipping_rate_store=False,
    )
    worker_count = 6
    start = threading.Barrier(worker_count + 1)

    def predict():
        start.wait(timeout=5)
        return client.category("MLM", "Mochila escolar")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(predict) for _ in range(worker_count)]
        start.wait(timeout=5)
        assert http.entered.wait(2)
        time.sleep(0.05)
        http.release.set()
        for future in futures:
            with pytest.raises(profitability.MercadoProfitabilityError, match="HTTP 503"):
                future.result(timeout=5)

    assert http.attempts == 1
    assert client.category("MLM", "Mochila escolar")["category_id"] == "MLM455455"
    assert http.attempts == 2


def test_listing_price_accepts_single_object_response_when_type_is_filtered():
    result = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=_OfficialApi(single_listing_price=True),
        cache_store=False,
    ).estimate({
        "source_item_id": "MLM3016972321",
        "title": "Mochila escolar",
        "price": 350,
        "currency_id": "MXN",
        "weight_g": 600,
        "volumetric_weight_kg": 1,
        "package_length_cm": 10,
        "package_width_cm": 20,
        "package_height_cm": 30,
    })

    assert result["commission_amount_local"] == 70
    assert result["commission_rate"] == 20


def test_concurrent_identical_commission_cache_misses_share_one_full_quote():
    class BlockingApi(_OfficialApi):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def get(self, url, *, headers, params, timeout):
            if "/listing_prices" in url:
                self.entered.set()
                assert self.release.wait(5)
            return super().get(url, headers=headers, params=params, timeout=timeout)

    http = BlockingApi()
    cache = _MemoryProfitabilityCache()
    client = MercadoProfitabilityClient(
        {}, http=http, cache_store=cache, shipping_rate_store=False,
    )
    worker_count = 8
    start = threading.Barrier(worker_count + 1)

    def quote():
        start.wait(timeout=5)
        return client.commission(
            "MLM",
            "MLM455455",
            350.0,
            "gold_special",
            currency_id="MXN",
            row={"weight_g": 600, "weight_basis": "plugin_actual"},
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(quote) for _ in range(worker_count)]
        start.wait(timeout=5)
        assert http.entered.wait(2)
        time.sleep(0.05)
        http.release.set()
        results = [future.result(timeout=5) for future in futures]

    assert all(result["amount"] == 70 for result in results)
    assert len(cache.commissions) == 1
    assert sum("/listing_prices" in url for url, _params in http.calls) == 1


def test_profitability_errors_are_saved_without_failing_collection():
    http = _OfficialApi()
    client = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
        cache_store=False,
    )
    result = enrich_profitability(
        {
            "source_item_id": "MLM3016972321",
            "title": "Mochila escolar",
            "price": 350,
            "currency_id": "MXN",
        },
        client=client,
    )

    assert "缺少智赢实际重量" in result["profitability_error"]
    assert result["sale_price_usd"] == 17.5
    assert result["exchange_rate_to_usd"] == 0.05
    assert result["profitability_source"] == (
        "mercadolibre_official_api_daily_database_cache"
    )


def test_net_proceeds_formula():
    assert calculate_net_proceeds_usd(20, 3.5, 6.4) == 10.1


def test_daily_cache_reuses_exchange_commission_and_shipping_quotes():
    with profitability._cache_lock:
        profitability._cache.clear()
    cache = _MemoryProfitabilityCache()
    row = {
        "source_item_id": "MLM3016972321",
        "title": "Mochila escolar",
        "price": 350,
        "currency_id": "MXN",
        "weight_g": 600,
        "volumetric_weight_kg": 1,
        "package_length_cm": 10,
        "package_width_cm": 20,
        "package_height_cm": 30,
    }
    first_http = _OfficialApi()
    first = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "cache-user"},
        http=first_http,
        cache_store=cache,
    ).estimate(row)
    assert cache.exchange
    assert cache.commissions
    assert cache.shipping

    with profitability._cache_lock:
        profitability._cache.clear()
    second_http = _OfficialApi()
    second = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "cache-user"},
        http=second_http,
        cache_store=cache,
    ).estimate({**row, **first})

    assert second["net_proceeds_usd"] == first["net_proceeds_usd"]
    urls = [url for url, _params in second_http.calls]
    assert not any("currency_conversions" in url for url in urls)
    assert not any("listing_prices" in url for url in urls)
    assert not any("shipping_options" in url for url in urls)
    assert not any("domain_discovery" in url for url in urls)


def test_supported_country_exchange_refresh_covers_all_publish_sites():
    calls = []

    class Client:
        def conversion_to_usd(self, currency_id, *, refresh_if_stale=False):
            calls.append((currency_id, refresh_if_stale))
            return {"ratio": 1}

    result = refresh_supported_exchange_rates(Client())

    assert set(result) == {"MLM", "MLB", "MLA", "MLC", "MCO", "MLU"}
    assert calls == [
        ("MXN", True),
        ("BRL", True),
        ("ARS", True),
        ("CLP", True),
        ("COP", True),
        ("UYU", True),
    ]


def test_product_pricing_uses_expired_database_rate_without_api_request():
    with profitability._cache_lock:
        profitability._cache.clear()

    class FixedDatabaseRate:
        def __init__(self):
            self.fresh_only_values = []

        def get_exchange_rate(self, source, target, *, fresh_only=False):
            self.fresh_only_values.append(fresh_only)
            return {
                "rate": 0.05,
                "source_created_at": "2026-09-11T00:00:00.000+00:00",
                "expires_at": "2026-09-12 00:00:00",
            }

    cache = FixedDatabaseRate()
    http = _OfficialApi()
    result = MercadoProfitabilityClient(
        {}, http=http, cache_store=cache,
    ).pricing({"price": 350, "currency_id": "MXN"})

    assert result["sale_price_usd"] == 17.5
    assert cache.fresh_only_values == [False]
    assert http.calls == []


def test_daily_exchange_refresh_replaces_only_stale_database_rate():
    with profitability._cache_lock:
        profitability._cache.clear()

    class DailyDatabaseRate:
        def __init__(self, fresh):
            self.fresh = fresh
            self.saved = []

        def get_exchange_rate(self, source, target, *, fresh_only=False):
            if fresh_only and not self.fresh:
                return None
            return {"rate": 0.04, "source_created_at": "2026-09-11"}

        def put_exchange_rate(self, source, target, value):
            self.saved.append((source, target, value["ratio"]))

    fresh_cache = DailyDatabaseRate(True)
    fresh_http = _OfficialApi()
    fresh = MercadoProfitabilityClient(
        {}, http=fresh_http, cache_store=fresh_cache,
    ).conversion_to_usd("MXN", refresh_if_stale=True)
    assert fresh["ratio"] == 0.04
    assert fresh_http.calls == []

    stale_cache = DailyDatabaseRate(False)
    stale_http = _OfficialApi()
    refreshed = MercadoProfitabilityClient(
        {}, http=stale_http, cache_store=stale_cache,
    ).conversion_to_usd("MXN", refresh_if_stale=True)
    assert refreshed["ratio"] == 0.05
    assert stale_cache.saved == [("MXN", "USD", 0.05)]
    assert len(stale_http.calls) == 1


class _DatabaseRateCard:
    def __init__(self):
        self.quotes = []

    def match(self, **quote):
        self.quotes.append(quote)
        return {"shipping_amount_usd": 6.4}


def test_cached_commission_and_rate_card_need_no_live_store_or_official_request():
    with profitability._cache_lock:
        profitability._cache.clear()
    cache = _MemoryProfitabilityCache()
    cache.put_exchange_rate("MXN", "USD", {"ratio": 0.05})
    cache.put_commission({
        "site_id": "MLM", "category_id": "MLM455455",
        "listing_type_id": "gold_special", "price": 350.0,
        "currency_id": "MXN", "logistic_type": "remote",
        "shipping_mode": "me2", "billable_weight_g": 600.0,
    }, {"amount": 70, "rate": 20, "currency_id": "MXN"})
    http = _OfficialApi()
    result = MercadoProfitabilityClient(
        {}, http=http, cache_store=cache, shipping_rate_store=_DatabaseRateCard(),
    ).estimate({
        "source_item_id": "MLM1", "category_id": "MLM455455",
        "price": 350, "currency_id": "MXN", "weight_g": 600,
    })
    assert result["net_proceeds_usd"] == 7.6
    assert http.calls == []


def test_usd_product_price_is_converted_before_matching_local_shipping_band():
    card = _DatabaseRateCard()
    client = MercadoProfitabilityClient(
        {}, http=_OfficialApi(), cache_store=False, shipping_rate_store=card,
    )
    result = client.shipping({}, {
        "source_item_id": "MLM1", "currency_id": "USD", "weight_g": 500,
    }, "", 20, "gold_special", free_shipping=True)
    assert card.quotes[0]["price_local"] == 400
    assert result["amount"] == 6.4
    assert result["currency_id"] == "USD"


def test_failed_category_does_not_block_database_shipping_or_keep_old_net_proceeds():
    class Api(_OfficialApi):
        def get(self, url, **kwargs):
            if "/domain_discovery/" in url:
                return _Response([])
            return super().get(url, **kwargs)

    http = Api()
    result = enrich_profitability({
        "source_item_id": "MLM1", "title": "Unknown product",
        "price": 350, "currency_id": "MXN", "weight_g": 600,
        "net_proceeds_usd": 100,
    }, client=MercadoProfitabilityClient(
        {}, http=http, cache_store=False, shipping_rate_store=_DatabaseRateCard(),
    ))
    assert result["shipping_fee_usd"] == 6.4
    assert result["sale_price_usd"] == 17.5
    assert result["net_proceeds_usd"] is None
    assert "分类" in result["profitability_error"]
    assert not any("/marketplace/users/" in url for url, _ in http.calls)


def test_missing_weight_keeps_successful_category_and_commission_for_next_retry():
    result = enrich_profitability({
        "source_item_id": "MLM1", "title": "Mochila escolar",
        "price": 350, "currency_id": "MXN",
    }, client=MercadoProfitabilityClient(
        {}, http=_OfficialApi(), cache_store=False,
        shipping_rate_store=_DatabaseRateCard(),
    ))
    assert result["category_id"] == "MLM455455"
    assert result["commission_amount_usd"] == 3.5
    assert result["commission_rate"] == 20
    assert result["net_proceeds_usd"] is None
    assert "实际重量" in result["profitability_error"]
