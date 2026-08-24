"""Official Mercado Libre fee, shipping and net-proceeds estimates.

The estimator is intended for source listings that do not belong to the
authorized seller yet.  It therefore uses the official pre-publication APIs:
category prediction, listing prices, currency conversion, and the seller's
shipping-options quote.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

import requests


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_LISTING_TYPE_ID = "gold_pro"
PROFITABILITY_SOURCE = "mercadolibre_official_api"
LIGHT_PACKAGE_LIMIT_G = 500.0


class MercadoProfitabilityError(RuntimeError):
    """An official profitability estimate could not be produced."""


_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, Any]] = {}


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def calculate_billable_weight_g(
    actual_weight_g: Any,
    volumetric_weight_kg: Any = None,
) -> float | None:
    """Apply the workbench's 500 g dimensional-weight rule.

    Up to and including 500 g, dimensions never increase the billed weight.
    Above 500 g, the larger of actual and volumetric weight is used.
    """

    actual = _positive(actual_weight_g)
    if actual is None:
        return None
    if actual <= LIGHT_PACKAGE_LIMIT_G:
        return round(actual, 4)
    volumetric_kg = _positive(volumetric_weight_kg)
    volumetric_g = volumetric_kg * 1000 if volumetric_kg is not None else 0.0
    return round(max(actual, volumetric_g), 4)


def shipping_dimensions_parameter(row: Mapping[str, Any]) -> str:
    """Build the official quote parameter while enforcing the 500 g rule."""

    actual = _positive(row.get("weight_g"))
    if actual is None:
        raise MercadoProfitabilityError("缺少智赢实际重量，暂时无法计算运费")
    billable = calculate_billable_weight_g(actual, row.get("volumetric_weight_kg"))
    assert billable is not None

    # Mercado Libre's quote accepts HxWxL,weight.  For light parcels we pass a
    # neutral 1 cm package so the API cannot apply dimensional weight below the
    # user-defined 500 g boundary.  Above the boundary, the already-calculated
    # billable weight is quoted together with the actual dimensions.
    if actual <= LIGHT_PACKAGE_LIMIT_G:
        height = width = length = 1.0
    else:
        height = _positive(row.get("package_height_cm")) or 1.0
        width = _positive(row.get("package_width_cm")) or 1.0
        length = _positive(row.get("package_length_cm")) or 1.0

    def text(value: float) -> str:
        return f"{value:.4f}".rstrip("0").rstrip(".")

    return f"{text(height)}x{text(width)}x{text(length)},{text(billable)}"


def calculate_net_proceeds_usd(
    sale_price_usd: Any,
    commission_amount_usd: Any,
    shipping_fee_usd: Any,
) -> float:
    values = tuple(_number(value) for value in (
        sale_price_usd,
        commission_amount_usd,
        shipping_fee_usd,
    ))
    if any(value is None for value in values):
        raise MercadoProfitabilityError("售价、佣金或运费不完整，无法计算净收益")
    return round(values[0] - values[1] - values[2], 2)  # type: ignore[operator]


def _cache_get(key: str) -> Any:
    with _cache_lock:
        expires_at, value = _cache.get(key, (0.0, None))
        if expires_at > time.time():
            return value
        _cache.pop(key, None)
    return None


def _cache_set(key: str, value: Any, ttl_seconds: int) -> Any:
    with _cache_lock:
        _cache[key] = (time.time() + max(1, int(ttl_seconds)), value)
    return value


def _site_id(row: Mapping[str, Any]) -> str:
    source = row.get("source") if isinstance(row.get("source"), Mapping) else {}
    item_id = str(row.get("source_item_id") or source.get("id") or "").upper()
    site_id = str(source.get("site_id") or item_id[:3] or "").upper()
    if len(site_id) != 3 or not site_id.startswith("ML"):
        raise MercadoProfitabilityError("无法识别商品所属国家站点")
    return site_id


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def active_store_token() -> dict[str, Any]:
    """Return a usable server-side token, refreshing it before expiry."""

    from bit import bit_mysql
    from bit import mercado_tokens

    summaries = list((bit_mysql.list_mercado_store_tokens() or {}).get("rows") or [])
    if not summaries:
        raise MercadoProfitabilityError("没有可用的 Mercado Libre 授权店铺")
    now = datetime.now()
    summaries.sort(
        key=lambda row: (
            _parse_datetime(row.get("expires_at")) or datetime.min,
            int(row.get("id") or 0),
        ),
        reverse=True,
    )
    summary = summaries[0]
    token_id = int(summary.get("id") or 0)
    expires_at = _parse_datetime(summary.get("expires_at"))
    if expires_at is not None and expires_at <= now + timedelta(minutes=10):
        try:
            mercado_tokens.refresh_and_save(
                token_id,
                get_token=bit_mysql.get_mercado_store_token,
                update_token=bit_mysql.update_mercado_store_token,
                record_error=bit_mysql.record_mercado_store_token_error,
            )
        except Exception as exc:
            if expires_at <= now:
                raise MercadoProfitabilityError(f"授权已过期且自动刷新失败：{exc}") from exc
    token = dict(bit_mysql.get_mercado_store_token(token_id) or {})
    if not token.get("access_token") or not token.get("meli_user_id"):
        raise MercadoProfitabilityError("授权店铺缺少 Access Token 或用户编号")
    return token


class MercadoProfitabilityClient:
    def __init__(
        self,
        token: Mapping[str, Any],
        *,
        http: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        self.token = dict(token)
        self.http = http or requests.Session()
        self.timeout = timeout
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token.get('access_token') or ''}",
        }

    def _get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        response = self.http.get(
            f"{API_BASE_URL}{path}",
            headers=self.headers,
            params=dict(params or {}),
            timeout=self.timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MercadoProfitabilityError(
                f"Mercado Libre 官网接口返回无法识别的数据（HTTP {response.status_code}）"
            ) from exc
        if not response.ok:
            message = payload.get("message") if isinstance(payload, Mapping) else payload
            raise MercadoProfitabilityError(
                f"Mercado Libre 官网接口失败（HTTP {response.status_code}）：{message}"
            )
        return payload

    def marketplace(self, site_id: str) -> dict[str, Any]:
        root_user_id = str(self.token.get("meli_user_id") or "")
        cache_key = f"marketplaces:{root_user_id}"
        payload = _cache_get(cache_key)
        if payload is None:
            payload = self._get(f"/marketplace/users/{root_user_id}")
            _cache_set(cache_key, payload, 6 * 60 * 60)
        marketplaces = payload.get("marketplaces") if isinstance(payload, Mapping) else []
        for marketplace in marketplaces or []:
            if str(marketplace.get("site_id") or "").upper() == site_id:
                return dict(marketplace)
        raise MercadoProfitabilityError(f"授权店铺未开通 {site_id} 站点")

    def category(self, site_id: str, title: str) -> dict[str, str]:
        payload = self._get(
            f"/sites/{site_id}/domain_discovery/search",
            params={"q": title},
        )
        if not isinstance(payload, list) or not payload:
            raise MercadoProfitabilityError("官网没有预测出对应商品分类")
        category = payload[0]
        category_id = str(category.get("category_id") or "")
        if not category_id:
            raise MercadoProfitabilityError("官网分类预测结果缺少分类编号")
        return {
            "category_id": category_id,
            "category_name": str(category.get("category_name") or ""),
        }

    def conversion_to_usd(self, currency_id: str) -> dict[str, Any]:
        currency_id = str(currency_id or "USD").upper()
        if currency_id == "USD":
            return {
                "ratio": 1.0,
                "creation_date": _now_text(),
                "valid_until": None,
            }
        cache_key = f"currency:{currency_id}:USD"
        payload = _cache_get(cache_key)
        if payload is None:
            payload = self._get(
                "/currency_conversions/search",
                params={"from": currency_id, "to": "USD"},
            )
            _cache_set(cache_key, payload, 60 * 60)
        ratio = _positive(payload.get("ratio") if isinstance(payload, Mapping) else None)
        if ratio is None:
            raise MercadoProfitabilityError(f"官网未返回 {currency_id} 到 USD 的汇率")
        return {
            "ratio": ratio,
            "creation_date": payload.get("creation_date"),
            "valid_until": payload.get("valid_until"),
        }

    def commission(
        self,
        site_id: str,
        category_id: str,
        price: float,
        listing_type_id: str,
    ) -> dict[str, Any]:
        payload = self._get(
            f"/sites/{site_id}/listing_prices",
            params={"price": price, "category_id": category_id},
        )
        choices = payload if isinstance(payload, list) else []
        selected = next(
            (
                choice for choice in choices
                if str(choice.get("listing_type_id") or "") == listing_type_id
            ),
            None,
        )
        if not selected:
            raise MercadoProfitabilityError(f"官网未返回刊登类型 {listing_type_id} 的分类佣金")
        details = selected.get("sale_fee_details") or {}
        amount = _number(selected.get("sale_fee_amount"))
        if amount is None:
            raise MercadoProfitabilityError("官网分类佣金缺少金额")
        return {
            "amount": amount,
            "currency_id": str(selected.get("currency_id") or ""),
            "rate": _number(details.get("percentage_fee")),
            "listing_type_name": str(selected.get("listing_type_name") or ""),
        }

    def shipping(
        self,
        marketplace: Mapping[str, Any],
        row: Mapping[str, Any],
        category_id: str,
        price: float,
        listing_type_id: str,
    ) -> dict[str, Any]:
        child_user_id = str(marketplace.get("user_id") or "")
        if not child_user_id:
            raise MercadoProfitabilityError("授权店铺站点缺少子账号编号")
        payload = self._get(
            f"/users/{child_user_id}/shipping_options/free",
            params={
                "dimensions": shipping_dimensions_parameter(row),
                "verbose": "true",
                "item_price": price,
                "listing_type_id": listing_type_id,
                "mode": "me2",
                "condition": "new",
                "logistic_type": str(marketplace.get("logistic_type") or "remote"),
                "free_shipping": "true",
                "category_id": category_id,
            },
        )
        coverage = payload.get("coverage") if isinstance(payload, Mapping) else {}
        country = coverage.get("all_country") if isinstance(coverage, Mapping) else {}
        amount = _number(country.get("list_cost") if isinstance(country, Mapping) else None)
        if amount is None:
            raise MercadoProfitabilityError("官网未返回全国运费")
        return {
            "amount": amount,
            "currency_id": str(country.get("currency_id") or ""),
            "api_billable_weight_g": _number(country.get("billable_weight")),
        }

    def estimate(self, row: Mapping[str, Any]) -> dict[str, Any]:
        price = _positive(row.get("price"))
        if price is None:
            raise MercadoProfitabilityError("商品缺少有效售价")
        title = str(row.get("title") or "").strip()
        if not title:
            raise MercadoProfitabilityError("商品缺少标题，无法预测分类")
        site_id = _site_id(row)
        currency_id = str(row.get("currency_id") or "USD").upper()
        listing_type_id = str(row.get("listing_type_id") or DEFAULT_LISTING_TYPE_ID)
        marketplace = self.marketplace(site_id)
        category = self.category(site_id, title)
        conversion = self.conversion_to_usd(currency_id)
        commission = self.commission(
            site_id,
            category["category_id"],
            price,
            listing_type_id,
        )
        shipping = self.shipping(
            marketplace,
            row,
            category["category_id"],
            price,
            listing_type_id,
        )
        exchange_rate = float(conversion["ratio"])
        commission_currency = commission["currency_id"] or currency_id
        shipping_currency = shipping["currency_id"] or currency_id
        commission_rate = exchange_rate
        if commission_currency != currency_id:
            commission_rate = float(self.conversion_to_usd(commission_currency)["ratio"])
        shipping_rate = exchange_rate
        if shipping_currency != currency_id:
            shipping_rate = float(self.conversion_to_usd(shipping_currency)["ratio"])
        sale_price_usd = round(price * exchange_rate, 2)
        commission_amount_usd = round(float(commission["amount"]) * commission_rate, 2)
        shipping_fee_usd = round(float(shipping["amount"]) * shipping_rate, 2)
        billable_weight = calculate_billable_weight_g(
            row.get("weight_g"), row.get("volumetric_weight_kg")
        )
        return {
            "sale_price_usd": sale_price_usd,
            "exchange_rate_to_usd": exchange_rate,
            "exchange_rate_updated_at": conversion.get("creation_date"),
            "category_id": category["category_id"],
            "category_name": category["category_name"],
            "listing_type_id": listing_type_id,
            "listing_type_name": commission.get("listing_type_name") or "",
            "commission_rate": commission.get("rate"),
            "commission_amount_local": commission["amount"],
            "commission_currency_id": commission_currency,
            "commission_amount_usd": commission_amount_usd,
            "shipping_fee_local": shipping["amount"],
            "shipping_currency_id": shipping_currency,
            "shipping_fee_usd": shipping_fee_usd,
            "billable_weight_g": billable_weight,
            "shipping_api_billable_weight_g": shipping.get("api_billable_weight_g"),
            "shipping_weight_rule": "actual_only_up_to_500g_else_max_actual_volumetric",
            "net_proceeds_usd": calculate_net_proceeds_usd(
                sale_price_usd, commission_amount_usd, shipping_fee_usd
            ),
            "profitability_updated_at": _now_text(),
            "profitability_source": PROFITABILITY_SOURCE,
            "profitability_error": "",
        }


def enrich_profitability(
    row: Mapping[str, Any],
    *,
    token: Mapping[str, Any] | None = None,
    client: MercadoProfitabilityClient | None = None,
) -> dict[str, Any]:
    result = dict(row)
    calculator = client or MercadoProfitabilityClient(token or active_store_token())
    try:
        result.update(calculator.estimate(result))
    except Exception as exc:
        result.update(
            profitability_updated_at=_now_text(),
            profitability_source=PROFITABILITY_SOURCE,
            profitability_error=str(exc)[:2000],
        )
    return result


__all__ = [
    "DEFAULT_LISTING_TYPE_ID",
    "LIGHT_PACKAGE_LIMIT_G",
    "MercadoProfitabilityClient",
    "MercadoProfitabilityError",
    "active_store_token",
    "calculate_billable_weight_g",
    "calculate_net_proceeds_usd",
    "enrich_profitability",
    "shipping_dimensions_parameter",
]
