"""SKU cost conversion for Zying's USD net-proceeds field."""
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import re

from erp.ecb_exchange_rates import fetch_usd_cny_daily_rates

from .models import number, parse_price
from .store import CHINA


NET_INCOME_BUFFER_CNY = Decimal("5")


def sku_cost(price_text, surcharge_text=None):
    base = number(parse_price(price_text))
    if surcharge_text is None:
        return {"price": str(base), "price_mode": "final"}
    # Only an explicit, non-negative amount is a surcharge. Empty and range
    # values must not silently become zero or the lowest advertised price.
    match = re.fullmatch(r"\s*\+?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?\s*", surcharge_text) if isinstance(surcharge_text, str) else None
    if not match:
        raise ValueError("变体加价不是确定的人民币金额")
    surcharge = number(match[1], allow_zero=True)
    return {"price": str(base + surcharge), "price_mode": "base_plus_surcharge",
            "base_price_cny": str(base), "variant_surcharge_cny": str(surcharge)}


def exchange_rate(config, store, now=None):
    now = now or datetime.now(CHINA)
    today = now.date()
    manual = config.get("usd_cny_rate")
    if manual not in (None, ""):
        return {"cny_per_usd": str(number(manual)), "date": today.isoformat(), "source": "manual"}
    cached = store.state("usd_cny_exchange_rate", {})
    if cached.get("checked_day") == today.isoformat():
        number(cached.get("cny_per_usd"))
        return cached
    store.log("正在获取美元兑人民币参考汇率（1美元兑换的人民币金额）")
    rows = fetch_usd_cny_daily_rates(today - timedelta(days=14), today)
    valid = [r for r in rows if (today - timedelta(days=7)).isoformat() <= str(r["creation_date"])[:10] <= today.isoformat()]
    if not valid:
        raise ValueError("没有近7天的美元兑人民币汇率，请在参数设置中填写业务汇率")
    latest = max(valid, key=lambda r: r["creation_date"])
    rate = number(latest["ecb_cny_per_eur"]) / number(latest["ecb_usd_per_eur"])
    result = {"cny_per_usd": str(rate), "date": latest["creation_date"][:10],
              "cny_per_eur": str(number(latest["ecb_cny_per_eur"])),
              "usd_per_eur": str(number(latest["ecb_usd_per_eur"])),
              "source": "ecb_reference_cross_rate", "checked_day": today.isoformat()}
    store.set_state("usd_cny_exchange_rate", result)
    store.log(f"汇率已更新：1 USD = {rate} CNY；参考日期 {result['date']}")
    return result


def usd_cost(cost_price, rate):
    cost, cny_per_usd = number(cost_price), number(rate.get("cny_per_usd"))
    pricing_basis = cost + NET_INCOME_BUFFER_CNY
    # Rational arithmetic avoids a rounded intermediate quotient accidentally
    # crossing an integer-dollar boundary before the ceiling operation.
    quotient = Fraction(pricing_basis) / Fraction(cny_per_usd)
    if rate.get("source") == "ecb_reference_cross_rate":
        quotient = (Fraction(pricing_basis) * Fraction(number(rate["usd_per_eur"]))
                    / Fraction(number(rate["cny_per_eur"])))
    rounded = (quotient.numerator + quotient.denominator - 1) // quotient.denominator
    return {"cost_price_cny": str(cost), "cny_per_usd": str(cny_per_usd),
            "pricing_basis_cny": str(pricing_basis),
            "net_income_buffer_cny": str(NET_INCOME_BUFFER_CNY),
            "rate_date": rate["date"], "rate_source": rate["source"],
            "unrounded_usd": str(Decimal(quotient.numerator) / Decimal(quotient.denominator)), "net_income_usd": str(rounded),
            "exchange_rate": rate,
            "rounding": "(highest_variant_price_plus_5_cny)_ceiling_to_integer_usd",
            "target_field": "netproceed"}


def protect_net_income(original, pricing):
    """Keep the existing ERP net income when the new calculation is lower.

    ``pricing.net_income_usd`` remains the calculated value for audit/display;
    ``net_income_writeback_usd`` is the value that may be written to ERP.
    Keeping both values makes the protective decision explicit in the UI and
    exported execution report.
    """
    result = dict(pricing)
    calculated = number(result["net_income_usd"], allow_zero=True)
    result["calculated_net_income_usd"] = str(calculated)
    result["net_income_writeback_usd"] = str(calculated)
    result["net_income_retained_original"] = False
    result["net_income_policy"] = "use_calculated"
    try:
        previous = number(original, allow_zero=True)
    except ValueError:
        return result
    result["original_net_income_usd"] = str(previous)
    if calculated < previous:
        result["net_income_writeback_usd"] = str(previous)
        result["net_income_retained_original"] = True
        result["net_income_policy"] = "keep_original_if_calculated_lower"
        result["net_income_adjustment"] = (
            f"计算净收益 ${calculated} 低于原净收益 ${previous}，"
            f"保留原净收益 ${previous}，仅修改重量"
        )
    return result
