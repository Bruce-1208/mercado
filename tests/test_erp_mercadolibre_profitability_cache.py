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


def test_existing_daily_rate_date_column_satisfies_schema_check(monkeypatch):
    rows = [
        {"TABLE_NAME": cache.EXCHANGE_RATE_TABLE, "COLUMN_NAME": "rate"},
        {"TABLE_NAME": cache.DAILY_EXCHANGE_RATE_TABLE, "COLUMN_NAME": "rate_date"},
        {"TABLE_NAME": cache.COMMISSION_TABLE, "COLUMN_NAME": "listing_type_id"},
        {"TABLE_NAME": cache.SHIPPING_RATE_TABLE, "COLUMN_NAME": "dimensions"},
    ]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query, _params=None):
            return None

        def fetchall(self):
            return rows

    class Connection:
        def __init__(self):
            self.commits = 0

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

    created = []
    monkeypatch.setattr(cache, "_schema_ready", False)
    monkeypatch.setattr(
        cache,
        "ensure_profitability_cache_tables",
        lambda _cursor: created.append(True),
    )
    connection = Connection()

    cache._ensure_schema(connection)

    assert created == []
    assert connection.commits == 1


def test_exchange_rate_snapshot_uses_official_creation_date():
    assert cache._exchange_rate_date(
        {"creation_date": "2026-08-26T06:10:48.000+00:00"},
        "2026-08-27 09:30:00",
    ) == "2026-08-26"


def test_exchange_rate_snapshot_falls_back_to_refresh_date():
    assert cache._exchange_rate_date({}, "2026-08-27 09:30:00") == "2026-08-27"


def test_exchange_rate_is_fixed_for_products_and_freshness_is_daily_only(monkeypatch):
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params=None):
            executed.append((" ".join(str(query).split()), params))

        def fetchone(self):
            return {"rate": 0.05, "expires_at": "2026-09-11 00:00:00"}

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    store = cache.DatabaseProfitabilityCache(connection_factory=Connection)
    monkeypatch.setattr(cache, "_ensure_schema", lambda _connection: None)

    assert store.get_exchange_rate("mxn", "usd")["rate"] == 0.05
    assert "expires_at` >" not in executed[-1][0]
    assert executed[-1][1] == ("MXN", "USD")

    assert store.get_exchange_rate("mxn", "usd", fresh_only=True)["rate"] == 0.05
    assert "expires_at` > %s" in executed[-1][0]
    assert len(executed[-1][1]) == 3


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
