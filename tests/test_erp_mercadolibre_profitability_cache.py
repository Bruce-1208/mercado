from erp import mercadolibre_profitability_cache as cache


class _Cursor:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((" ".join(str(query).split()), params))


def test_cache_schema_has_separate_daily_reference_tables():
    cursor = _Cursor()
    cache.ensure_profitability_cache_tables(cursor)

    sql = " ".join(query for query, _params in cursor.queries)
    assert cache.EXCHANGE_RATE_TABLE in sql
    assert cache.DAILY_EXCHANGE_RATE_TABLE in sql
    assert cache.COMMISSION_TABLE in sql
    assert cache.SHIPPING_RATE_TABLE in sql
    assert "`expires_at` DATETIME NOT NULL" in sql
    assert "`site_id`" in sql
    assert "`category_id`" in sql
    assert "`dimensions`" in sql


def test_exchange_rate_snapshot_uses_official_creation_date():
    assert cache._exchange_rate_date(
        {"creation_date": "2026-08-26T06:10:48.000+00:00"},
        "2026-08-27 09:30:00",
    ) == "2026-08-26"


def test_exchange_rate_snapshot_falls_back_to_refresh_date():
    assert cache._exchange_rate_date({}, "2026-08-27 09:30:00") == "2026-08-27"


def test_quote_cache_keys_include_every_price_affecting_field():
    base = {
        "site_id": "MLM",
        "category_id": "MLM123",
        "listing_type_id": "gold_pro",
        "price": 350.0,
        "currency_id": "MXN",
        "logistic_type": "remote",
        "shipping_mode": "me2",
        "billable_weight_g": 1000.0,
    }
    original = cache.DatabaseProfitabilityCache.commission_key(**base)
    assert original == cache.DatabaseProfitabilityCache.commission_key(**dict(base))
    assert original != cache.DatabaseProfitabilityCache.commission_key(
        **{**base, "price": 351.0}
    )
    assert original != cache.DatabaseProfitabilityCache.commission_key(
        **{**base, "site_id": "MLB"}
    )

    shipping = {
        "site_id": "MLM",
        "marketplace_user_id": "123",
        "category_id": "MLM123",
        "listing_type_id": "gold_special",
        "price": 350.0,
        "dimensions": "10x20x30,1000",
        "logistic_type": "remote",
        "shipping_mode": "me2",
        "free_shipping": True,
    }
    free_key = cache.DatabaseProfitabilityCache.shipping_key(**shipping)
    buyer_paid_key = cache.DatabaseProfitabilityCache.shipping_key(
        **{**shipping, "free_shipping": False}
    )
    assert free_key != buyer_paid_key
