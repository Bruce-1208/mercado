"""Daily USD/CNY reference rates derived from official ECB observations."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any

import requests

from erp.mercadolibre_profitability_cache import DatabaseProfitabilityCache


ECB_EXCHANGE_RATE_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.USD+CNY.EUR.SP00.A"
)
DEFAULT_HISTORY_DAYS = 800


class EcbExchangeRateError(RuntimeError):
    pass


def _date_text(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value or "").strip()


def parse_usd_cny_csv(content: str) -> list[dict[str, Any]]:
    """Cross ECB's CNY/EUR and USD/EUR series into CNY per USD."""
    observations: dict[str, dict[str, float]] = {}
    for row in csv.DictReader(io.StringIO(str(content or ""))):
        currency = str(row.get("CURRENCY") or "").upper()
        day = str(row.get("TIME_PERIOD") or "").strip()
        if currency not in ("USD", "CNY") or not day:
            continue
        try:
            value = float(row.get("OBS_VALUE"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            observations.setdefault(day, {})[currency] = value
    result = []
    for day in sorted(observations):
        rates = observations[day]
        if rates.get("USD", 0) <= 0 or rates.get("CNY", 0) <= 0:
            continue
        result.append({
            "currency_base": "USD",
            "currency_quote": "CNY",
            "ratio": rates["CNY"] / rates["USD"],
            "creation_date": f"{day}T16:00:00+01:00",
            "valid_until": None,
            "source": "ecb_reference_cross_rate",
            "ecb_usd_per_eur": rates["USD"],
            "ecb_cny_per_eur": rates["CNY"],
        })
    return result


def fetch_usd_cny_daily_rates(
    start_date: date | str,
    end_date: date | str,
    *,
    http=requests,
) -> list[dict[str, Any]]:
    response = http.get(
        ECB_EXCHANGE_RATE_URL,
        params={
            "startPeriod": _date_text(start_date),
            "endPeriod": _date_text(end_date),
            "format": "csvdata",
        },
        headers={"Accept": "text/csv", "User-Agent": "MercadoWorkbench/1.0"},
        timeout=30,
    )
    if not response.ok:
        raise EcbExchangeRateError(
            f"欧洲央行汇率接口失败（HTTP {response.status_code}）"
        )
    rates = parse_usd_cny_csv(response.text)
    if not rates:
        raise EcbExchangeRateError("欧洲央行没有返回 USD/CNY 每日参考汇率")
    return rates


def refresh_usd_cny_daily_rates(
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    cache_store: DatabaseProfitabilityCache | None = None,
    http=requests,
) -> dict[str, Any]:
    end = date.fromisoformat(_date_text(end_date)) if end_date else date.today()
    start = (
        date.fromisoformat(_date_text(start_date))
        if start_date
        else end - timedelta(days=DEFAULT_HISTORY_DAYS)
    )
    if end < start:
        raise ValueError("汇率截止日期不能早于起始日期")
    rates = fetch_usd_cny_daily_rates(start, end, http=http)
    store = cache_store or DatabaseProfitabilityCache()
    stored = store.put_exchange_rate_history("USD", "CNY", rates)
    latest = rates[-1]
    return {
        "stored": stored,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "latest_date": str(latest["creation_date"])[:10],
        "latest_rate": float(latest["ratio"]),
        "source": "European Central Bank",
    }


__all__ = [
    "DEFAULT_HISTORY_DAYS",
    "ECB_EXCHANGE_RATE_URL",
    "EcbExchangeRateError",
    "fetch_usd_cny_daily_rates",
    "parse_usd_cny_csv",
    "refresh_usd_cny_daily_rates",
]
