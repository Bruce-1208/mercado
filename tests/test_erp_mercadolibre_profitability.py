from erp.mercadolibre_profitability import (
    MercadoProfitabilityClient,
    calculate_billable_weight_g,
    calculate_net_proceeds_usd,
    enrich_profitability,
    shipping_dimensions_parameter,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class _OfficialApi:
    def __init__(self):
        self.calls = []

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
            return _Response([{
                "listing_type_id": "gold_pro",
                "listing_type_name": "Premium",
                "currency_id": "MXN",
                "sale_fee_amount": 70,
                "sale_fee_details": {"percentage_fee": 20},
            }])
        if "/shipping_options/free" in url:
            return _Response({
                "coverage": {
                    "all_country": {
                        "list_cost": 128,
                        "currency_id": "MXN",
                        "billable_weight": 1000,
                    }
                }
            })
        raise AssertionError(url)


def test_billable_weight_ignores_volumetric_up_to_and_including_500g():
    assert calculate_billable_weight_g(200, 9) == 200
    assert calculate_billable_weight_g(500, 9) == 500
    assert shipping_dimensions_parameter({"weight_g": 500, "volumetric_weight_kg": 9}) == (
        "1x1x1,500"
    )


def test_billable_weight_uses_larger_weight_only_above_500g():
    assert calculate_billable_weight_g(500.1, 0.8) == 800
    assert calculate_billable_weight_g(900, 0.8) == 900
    assert calculate_billable_weight_g(None, 1.2) is None
    assert shipping_dimensions_parameter({
        "weight_g": 600,
        "volumetric_weight_kg": 1,
        "package_length_cm": 10,
        "package_width_cm": 20,
        "package_height_cm": 30,
    }) == "30x20x10,1000"


def test_official_estimate_calculates_usd_commission_shipping_and_net_proceeds():
    http = _OfficialApi()
    client = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
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
    assert result["sale_price_usd"] == 17.5
    assert result["commission_rate"] == 20
    assert result["commission_amount_usd"] == 3.5
    assert result["shipping_fee_usd"] == 6.4
    assert result["billable_weight_g"] == 1000
    assert result["net_proceeds_usd"] == 7.6
    shipping_call = next(call for call in http.calls if "/shipping_options/free" in call[0])
    assert shipping_call[1]["dimensions"] == "30x20x10,1000"


def test_profitability_errors_are_saved_without_failing_collection():
    http = _OfficialApi()
    client = MercadoProfitabilityClient(
        {"access_token": "secret", "meli_user_id": "111"},
        http=http,
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
    assert result["profitability_source"] == "mercadolibre_official_api"


def test_net_proceeds_formula():
    assert calculate_net_proceeds_usd(20, 3.5, 6.4) == 10.1
