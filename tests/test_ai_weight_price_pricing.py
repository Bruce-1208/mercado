from datetime import datetime

import pytest

from erp.ai_weight_price import pricing
from erp.ai_weight_price.config import validate
from erp.ai_weight_price.store import CHINA, Store


@pytest.mark.parametrize("cny,rate,usd", [(22, "7.2", "4"), ("14.4", "7.2", "2"), ("14.400001", "7.2", "3"), ("0.01", "7.2", "1"), ("35", "7", "5")])
def test_usd_cost_rounds_up_after_dividing(cny, rate, usd):
    result = pricing.usd_cost(cny, {"cny_per_usd": rate, "date": "2026-09-07", "source": "manual"})
    assert result["net_income_usd"] == usd
    assert result["cost_price_cny"] == str(cny)
    assert result["target_field"] == "netproceed"


@pytest.mark.parametrize("rate", [0, -1, True, "NaN", "Infinity", "", []])
def test_invalid_exchange_rate_is_rejected(rate):
    with pytest.raises(ValueError):
        pricing.usd_cost("12.50", {"cny_per_usd": rate})


def test_variant_surcharge_is_added_once():
    final = pricing.sku_cost("￥ 22.00")
    added = pricing.sku_cost("￥20", "+￥2.00")
    zero = pricing.sku_cost("￥22.00", "0")
    assert final["price"] == added["price"] == zero["price"] == "22.00"
    assert added["variant_surcharge_cny"] == "2.00"
    assert pricing.usd_cost(added["price"], {"cny_per_usd": "7.2", "date": "2026-09-07", "source": "manual"})["net_income_usd"] == "4"


@pytest.mark.parametrize("surcharge", ["", "1-2元", "2起", "-2", "优惠2元", "NaN", True])
def test_unclear_surcharge_is_not_silently_zero(surcharge):
    with pytest.raises(ValueError):
        pricing.sku_cost("20", surcharge)


def test_exchange_rate_reuses_existing_ecb_fetcher_without_mysql_and_keeps_exact_cross_rate(tmp_path, monkeypatch):
    calls = []
    def fetch(start, end):
        calls.append((start, end))
        return [{"creation_date": "2026-09-04T16:00:00+01:00", "ecb_cny_per_eur": 7, "ecb_usd_per_eur": 3}]
    monkeypatch.setattr(pricing, "fetch_usd_cny_daily_rates", fetch)
    store = Store(tmp_path)
    now = datetime(2026, 9, 7, 10, tzinfo=CHINA)
    rate = pricing.exchange_rate(validate({}), store, now)
    assert rate["date"] == "2026-09-04"
    assert pricing.exchange_rate(validate({}), Store(tmp_path), now) == rate
    assert len(calls) == 1
    result = pricing.usd_cost("7", rate)
    assert result["net_income_usd"] == result["unrounded_usd"] == "3"
    manual = pricing.exchange_rate(validate({"usd_cny_rate": "7.2"}), store, now)
    assert manual["source"] == "manual" and manual["cny_per_usd"] == "7.2"
    assert len(calls) == 1


def test_old_reference_rate_and_fetch_failure_are_not_replaced_with_a_guess(tmp_path, monkeypatch):
    store = Store(tmp_path)
    now = datetime(2026, 9, 7, tzinfo=CHINA)
    monkeypatch.setattr(pricing, "fetch_usd_cny_daily_rates", lambda *args: [{"creation_date": "2026-08-28", "ecb_cny_per_eur": 7, "ecb_usd_per_eur": 1}])
    with pytest.raises(ValueError, match="近7天"):
        pricing.exchange_rate(validate({}), store, now)
    def fail(*args): raise RuntimeError("no connection")
    monkeypatch.setattr(pricing, "fetch_usd_cny_daily_rates", fail)
    with pytest.raises(RuntimeError, match="no connection"):
        pricing.exchange_rate(validate({}), store, now)
    assert store.state("usd_cny_exchange_rate") is None
