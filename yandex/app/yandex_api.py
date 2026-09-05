from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlencode

import httpx

from yandex.app.config import settings
from yandex.app.product_media import normalize_product_pictures


RETRYABLE_STATUS_CODES = {420, 429, 500, 502, 503, 504}
WRITE_SCOPES = {"ALL_METHODS", "offers-and-cards-management", "all-methods"}


class YandexApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details


@dataclass(slots=True)
class StoreContext:
    business_id: int
    business_name: str
    campaign_id: int
    store_name: str
    placement_type: str
    api_availability: str
    auth_scopes: list[str]

    def public_dict(self) -> dict[str, Any]:
        return {
            "business_id": self.business_id,
            "business_name": self.business_name,
            "campaign_id": self.campaign_id,
            "store_name": self.store_name,
            "placement_type": self.placement_type,
            "api_availability": self.api_availability,
            "auth_scopes": self.auth_scopes,
        }


@dataclass(slots=True)
class StockTarget:
    method: str
    warehouse_id: int | None
    warehouse_name: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "warehouse_id": self.warehouse_id,
            "warehouse_name": self.warehouse_name,
        }


def _clean_description(value: str) -> str:
    # 商品描述中不能带联系方式或外链；这里只删除明显的 URL/邮箱，不虚构内容。
    value = re.sub(r"https?://\S+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", "", value)
    value = " ".join(value.split())
    return value[:6000]


def _match_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return "".join(character for character in normalized if character.isalnum())


def _text_value(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _boolean_value(value: Any) -> str | None:
    normalized = _match_key(value)
    true_values = {"да", "есть", "true", "yes", "1", "是", "有", "支持"}
    false_values = {"нет", "отсутствует", "false", "no", "0", "否", "无", "不支持"}
    if normalized in true_values or any(normalized.startswith(item) for item in ("да", "есть")):
        return "true"
    if normalized in false_values or any(
        normalized.startswith(item) for item in ("нет", "отсутствует")
    ):
        return "false"
    return None


def _integer_price(value: Any) -> int:
    try:
        price = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise YandexApiError("商品价格格式不正确") from exc
    return int(price)


def _api_message(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)[:1000]
    messages: list[str] = []
    for key in ("message", "error", "errors", "warnings"):
        value = data.get(key)
        if value:
            messages.append(str(value))
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        for key in ("errors", "warnings"):
            if result.get(key):
                messages.append(str(result[key]))
    return "；".join(messages)[:2000] or "Yandex API 返回未知错误"


class YandexSellerClient:
    def __init__(self, token: str) -> None:
        self._token = token.strip()
        self._category_parameters_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> dict[str, Any]:
        headers = {"Api-Key": self._token, "Accept": "application/json"}
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    base_url=settings.seller_api_base_url,
                    headers=headers,
                    timeout=httpx.Timeout(20.0),
                    limits=limits,
                ) as client:
                    response = await client.request(method, path, json=json_body)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
                try:
                    data = response.json()
                except ValueError:
                    data = {"message": response.text[:1000]}
                if response.is_error:
                    raise YandexApiError(
                        _api_message(data), status_code=response.status_code, details=data
                    )
                return data
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
        raise YandexApiError("无法连接 Yandex 卖家 API，请稍后重试") from last_error

    async def get_token_info(self) -> dict[str, Any]:
        response = await self._request("POST", "/v2/auth/token", json_body={})
        api_key = ((response.get("result") or {}).get("apiKey") or {})
        return {
            "name": api_key.get("name", ""),
            "auth_scopes": list(api_key.get("authScopes") or []),
        }

    async def get_store_context(self) -> StoreContext:
        token_info, campaigns_response = await asyncio.gather(
            self.get_token_info(),
            self._request("GET", "/v2/campaigns"),
        )
        campaigns = campaigns_response.get("campaigns") or (
            (campaigns_response.get("result") or {}).get("campaigns") or []
        )
        if not campaigns:
            raise YandexApiError("该 token 没有可访问的店铺")
        available = [item for item in campaigns if item.get("apiAvailability") == "AVAILABLE"]
        campaign = (available or campaigns)[0]
        business = campaign.get("business") or {}
        business_id = business.get("id")
        campaign_id = campaign.get("id")
        if not business_id or not campaign_id:
            raise YandexApiError("Yandex API 未返回 businessId 或 campaignId")
        scopes = token_info["auth_scopes"]
        normalized_scopes = {str(scope).lower().replace("_", "-") for scope in scopes}
        supported_scopes = {
            "all-methods",
            "all-methods:read-only",
            "offers-and-cards-management",
            "offers-and-cards-management:read-only",
            "inventory-and-order-processing",
            "inventory-and-order-processing:read-only",
            "communication",
            "finance-and-accounting",
            "finance-and-accounting:read-only",
            "pricing",
            "pricing:read-only",
        }
        if not (supported_scopes & normalized_scopes):
            raise YandexApiError("token 没有本工作台支持的卖家 API 权限")
        if campaign.get("apiAvailability") != "AVAILABLE":
            raise YandexApiError(
                f"店铺 API 当前不可用：{campaign.get('apiAvailability', 'UNKNOWN')}"
            )
        return StoreContext(
            business_id=int(business_id),
            business_name=str(business.get("name") or ""),
            campaign_id=int(campaign_id),
            store_name=str(campaign.get("domain") or ""),
            placement_type=str(campaign.get("placementType") or ""),
            api_availability=str(campaign.get("apiAvailability") or ""),
            auth_scopes=scopes,
        )

    async def get_category_parameters(
        self,
        business_id: int,
        category_id: int,
    ) -> list[dict[str, Any]]:
        cache_key = (int(business_id), int(category_id))
        cached = self._category_parameters_cache.get(cache_key)
        if cached is not None:
            return cached
        response = await self._request(
            "POST",
            f"/v2/category/{int(category_id)}/parameters?businessId={int(business_id)}",
        )
        result = response.get("result") or {}
        parameters = result.get("parameters") or response.get("parameters") or []
        normalized = [item for item in parameters if isinstance(item, dict) and item.get("id")]
        self._category_parameters_cache[cache_key] = normalized
        return normalized

    @staticmethod
    def build_parameter_values(
        specifications: dict[str, Any],
        category_parameters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Safely map exact source spec names to Yandex category parameters.

        Yandex Market itself uses most of the same Russian parameter names on the buyer page,
        so exact normalized matching gives high coverage without guessing incompatible fields.
        """
        specification_lookup = {
            _match_key(key): _text_value(value)
            for key, value in (specifications or {}).items()
            if _match_key(key) and _text_value(value)
        }
        parameter_values: list[dict[str, Any]] = []
        for parameter in category_parameters or []:
            parameter_id = parameter.get("id")
            raw_value = specification_lookup.get(_match_key(parameter.get("name")))
            if not parameter_id or not raw_value:
                continue
            parameter_type = str(parameter.get("type") or "TEXT").upper()
            constraints = parameter.get("constraints") or {}

            if parameter_type == "BOOLEAN":
                value = _boolean_value(raw_value)
                if value is not None:
                    parameter_values.append({"parameterId": int(parameter_id), "value": value})
                continue

            if parameter_type == "NUMERIC":
                match = re.search(r"[-+]?\d+(?:[.,]\d+)?", raw_value.replace("−", "-"))
                if not match:
                    continue
                number_text = match.group(0).replace(",", ".")
                try:
                    numeric_value = Decimal(number_text)
                    minimum = constraints.get("minValue")
                    maximum = constraints.get("maxValue")
                    if minimum is not None and numeric_value < Decimal(str(minimum)):
                        continue
                    if maximum is not None and numeric_value > Decimal(str(maximum)):
                        continue
                except (InvalidOperation, TypeError, ValueError):
                    continue
                item: dict[str, Any] = {
                    "parameterId": int(parameter_id),
                    "value": format(numeric_value, "f"),
                }
                unit_source = _match_key(raw_value[match.end() :])
                unit_config = parameter.get("unit") or {}
                units = (
                    unit_config.get("units") or []
                    if isinstance(unit_config, dict)
                    else []
                )
                for unit in units:
                    if not isinstance(unit, dict) or not unit.get("id"):
                        continue
                    unit_keys = {
                        _match_key(unit.get("name")),
                        _match_key(unit.get("fullName")),
                    }
                    if unit_source and any(key and key in unit_source for key in unit_keys):
                        item["unitId"] = int(unit["id"])
                        break
                parameter_values.append(item)
                continue

            if parameter_type == "ENUM":
                is_multivalue = bool(
                    parameter.get("multivalue") or parameter.get("isMultivalue")
                )
                raw_parts = (
                    [part.strip() for part in re.split(r"[;,]", raw_value) if part.strip()]
                    if is_multivalue
                    else [raw_value]
                )
                allowed_values = {
                    _match_key(item.get("value")): item
                    for item in parameter.get("values") or []
                    if isinstance(item, dict) and _match_key(item.get("value"))
                }
                allow_custom = bool(
                    constraints.get("allowCustomValues")
                    or parameter.get("allowCustomValues")
                )
                for part in raw_parts:
                    allowed = allowed_values.get(_match_key(part))
                    value_id = (allowed or {}).get("id") or (allowed or {}).get("valueId")
                    if value_id:
                        parameter_values.append(
                            {"parameterId": int(parameter_id), "valueId": int(value_id)}
                        )
                    elif allow_custom:
                        parameter_values.append(
                            {"parameterId": int(parameter_id), "value": part[:255]}
                        )
                    if not is_multivalue:
                        break
                continue

            max_length = constraints.get("maxLength")
            try:
                limit = min(max(int(max_length), 1), 6000) if max_length else 6000
            except (TypeError, ValueError):
                limit = 6000
            parameter_values.append(
                {"parameterId": int(parameter_id), "value": raw_value[:limit]}
            )
        return parameter_values[:300]

    @staticmethod
    def build_offer_mapping(
        product: dict[str, Any],
        parameter_values: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        missing = product.get("missing_publish_fields") or []
        if missing:
            raise YandexApiError(f"商品缺少上传字段：{', '.join(missing)}")

        pictures = normalize_product_pictures(product.get("pictures") or [])
        if not pictures:
            raise YandexApiError("没有符合 Yandex 规则的商品图片（至少 300×300 像素）")
        dimensions = product.get("weight_dimensions") or {}
        dimension_names = ("length", "width", "height", "weight")
        try:
            normalized_dimensions = {
                name: round(float(dimensions[name]), 3) for name in dimension_names
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise YandexApiError("未填写完整的包装长、宽、高和重量") from exc
        if any(value <= 0 for value in normalized_dimensions.values()):
            raise YandexApiError("包装长、宽、高和重量必须大于 0")

        offer: dict[str, Any] = {
            "offerId": product["offer_id"],
            "name": " ".join(product["name"].split())[:256],
            "marketCategoryId": int(product["market_category_id"]),
            "pictures": pictures,
            "vendor": " ".join(product["vendor"].split())[:100],
            "description": _clean_description(product["description"]),
            "weightDimensions": normalized_dimensions,
        }
        if product.get("vendor_code"):
            offer["vendorCode"] = str(product["vendor_code"])[:100]
        if parameter_values:
            offer["parameterValues"] = parameter_values
        if product.get("price") and float(product["price"]) > 0:
            offer["basicPrice"] = {
                # Yandex 对当前店铺的 RUR/CNY 价格都要求不带小数位。
                "value": _integer_price(product["price"]),
                "currencyId": product.get("currency") or "RUR",
            }
        return {
            "offer": offer,
            "mapping": {"marketSku": int(product["market_sku"])},
        }

    async def publish_product(self, business_id: int, product: dict[str, Any]) -> dict[str, Any]:
        category_parameters = await self.get_category_parameters(
            business_id,
            int(product["market_category_id"]),
        )
        parameter_values = self.build_parameter_values(
            product.get("specifications") or {},
            category_parameters,
        )
        if product.get("specifications") and not parameter_values:
            raise YandexApiError(
                "采集到的规格未能匹配 Yandex 类目参数，请检查类目或规格名称"
            )
        mapping = self.build_offer_mapping(product, parameter_values)
        response = await self._request(
            "POST",
            f"/v2/businesses/{business_id}/offer-mappings/update",
            json_body={"offerMappings": [mapping], "onlyPartnerMediaContent": False},
        )
        if str(response.get("status", "OK")).upper() != "OK":
            raise YandexApiError(_api_message(response), details=response)
        results = response.get("results") or []
        if any(item.get("errors") for item in results if isinstance(item, dict)):
            raise YandexApiError(_api_message(response), details=response)
        response["_local"] = {
            "submittedParameterCount": len(parameter_values),
            "categoryParameterCount": len(category_parameters),
        }
        return response

    async def get_offer_cards(
        self,
        business_id: int,
        offer_ids: list[str],
        *,
        with_recommendations: bool = True,
    ) -> list[dict[str, Any]]:
        unique_offer_ids = list(dict.fromkeys(str(value) for value in offer_ids if value))
        if not unique_offer_ids or len(unique_offer_ids) > 200:
            raise YandexApiError("每次查询卡片质量必须包含 1–200 个商品")
        response = await self._request(
            "POST",
            f"/v2/businesses/{business_id}/offer-cards",
            json_body={
                "offerIds": unique_offer_ids,
                "withRecommendations": with_recommendations,
            },
        )
        result = response.get("result") or {}
        cards = result.get("offerCards") or response.get("offerCards") or []
        return [item for item in cards if isinstance(item, dict)]

    async def get_orders(
        self,
        business_id: int,
        *,
        campaign_id: int | None = None,
        statuses: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page_token: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return one page from the current business-level orders API."""
        query: dict[str, str | int] = {"limit": max(1, min(int(limit), 50))}
        if page_token:
            query["pageToken"] = page_token
        body: dict[str, Any] = {"fake": False}
        if campaign_id:
            body["campaignIds"] = [int(campaign_id)]
        if statuses:
            body["statuses"] = list(dict.fromkeys(str(value).upper() for value in statuses))
        dates = {
            key: value
            for key, value in {
                "creationDateFrom": date_from,
                "creationDateTo": date_to,
            }.items()
            if value
        }
        if dates:
            body["dates"] = dates
        response = await self._request(
            "POST",
            f"/v1/businesses/{int(business_id)}/orders?{urlencode(query)}",
            json_body=body,
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        result = response.get("result") or response
        orders = result.get("orders") or []
        paging = result.get("paging") or {}
        return {
            "orders": [item for item in orders if isinstance(item, dict)],
            "paging": {"nextPageToken": str(paging.get("nextPageToken") or "")},
        }

    async def update_order_status(
        self,
        campaign_id: int,
        order_id: int,
        *,
        status: str,
        substatus: str,
    ) -> dict[str, Any]:
        response = await self._request(
            "PUT",
            f"/v2/campaigns/{int(campaign_id)}/orders/{int(order_id)}/status",
            json_body={"order": {"status": status, "substatus": substatus}},
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        return response

    async def get_offer_stocks(
        self,
        business_id: int,
        campaign_id: int,
        target: StockTarget,
        *,
        offer_ids: list[str] | None = None,
        archived: bool = False,
        page_token: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(str(value).strip() for value in offer_ids or [] if str(value).strip()))
        if len(unique_ids) > 500:
            raise YandexApiError("每次查询库存最多包含 500 个 SKU")
        query: dict[str, str | int] = {}
        if not unique_ids:
            query["limit"] = max(1, min(int(limit), 100))
            if page_token:
                query["pageToken"] = page_token
        suffix = f"?{urlencode(query)}" if query else ""
        body: dict[str, Any] = {"offerIds": unique_ids} if unique_ids else {"archived": bool(archived)}

        if target.method == "business":
            if not target.warehouse_id:
                raise YandexApiError("库存查询缺少独立仓库 ID")
            body["partnerWarehouseId"] = int(target.warehouse_id)
            response = await self._request(
                "POST",
                f"/v3/businesses/{int(business_id)}/offers/stocks{suffix}",
                json_body=body,
            )
            result = response.get("result") or response
            warehouses = [{
                "warehouseId": result.get("partnerWarehouseId") or target.warehouse_id,
                "warehouseName": target.warehouse_name,
                "offers": [item for item in result.get("offers") or [] if isinstance(item, dict)],
            }]
        elif target.method == "campaign":
            response = await self._request(
                "POST",
                f"/v2/campaigns/{int(campaign_id)}/offers/stocks{suffix}",
                json_body=body,
            )
            result = response.get("result") or response
            warehouses = [item for item in result.get("warehouses") or [] if isinstance(item, dict)]
            for warehouse in warehouses:
                warehouse.setdefault("warehouseName", target.warehouse_name)
        else:
            raise YandexApiError(f"未知库存接口类型：{target.method}")
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        return {
            "warehouses": warehouses,
            "paging": {
                "nextPageToken": str(((result.get("paging") or {}).get("nextPageToken") or ""))
            },
            "stockMethod": target.method,
        }

    async def get_returns(
        self,
        campaign_id: int,
        *,
        return_type: str = "",
        statuses: list[str] | None = None,
        shipment_statuses: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page_token: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        query: dict[str, str | int] = {"limit": max(1, min(int(limit), 100))}
        if return_type:
            query["type"] = return_type
        if statuses:
            query["statuses"] = ",".join(dict.fromkeys(statuses))
        if shipment_statuses:
            query["shipmentStatuses"] = ",".join(dict.fromkeys(shipment_statuses))
        if date_from:
            query["fromDate"] = date_from
        if date_to:
            query["toDate"] = date_to
        if page_token:
            query["pageToken"] = page_token
        response = await self._request(
            "GET", f"/v2/campaigns/{int(campaign_id)}/returns?{urlencode(query)}"
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        result = response.get("result") or response
        return {
            "returns": [item for item in result.get("returns") or [] if isinstance(item, dict)],
            "paging": {"nextPageToken": str((result.get("paging") or {}).get("nextPageToken") or "")},
        }

    async def get_feedbacks(
        self,
        business_id: int,
        *,
        reaction_status: str = "ALL",
        rating_values: list[int] | None = None,
        offer_ids: list[str] | None = None,
        page_token: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        query: dict[str, str | int] = {"limit": max(1, min(int(limit), 50))}
        if page_token:
            query["pageToken"] = page_token
        body: dict[str, Any] = {"reactionStatus": reaction_status}
        if rating_values:
            body["ratingValues"] = list(dict.fromkeys(int(value) for value in rating_values))
        if offer_ids:
            body["offerIds"] = list(dict.fromkeys(str(value) for value in offer_ids))
        response = await self._request(
            "POST",
            f"/v2/businesses/{int(business_id)}/goods-feedback?{urlencode(query)}",
            json_body=body,
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        result = response.get("result") or response
        return {
            "feedbacks": [item for item in result.get("feedbacks") or [] if isinstance(item, dict)],
            "paging": {"nextPageToken": str((result.get("paging") or {}).get("nextPageToken") or "")},
        }

    async def reply_to_feedback(
        self, business_id: int, feedback_id: int, text: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v2/businesses/{int(business_id)}/goods-feedback/comments/update?sourceType=SELLER",
            json_body={"feedbackId": int(feedback_id), "comment": {"text": text}},
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        return response.get("result") or response

    async def skip_feedbacks(
        self, business_id: int, feedback_ids: list[int]
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v2/businesses/{int(business_id)}/goods-feedback/skip-reaction?sourceType=SELLER",
            json_body={"feedbackIds": list(dict.fromkeys(int(value) for value in feedback_ids))},
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        return response

    async def get_questions(
        self,
        business_id: int,
        *,
        need_answer: bool = False,
        date_from: str | None = None,
        date_to: str | None = None,
        page_token: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        query: dict[str, str | int] = {"limit": max(1, min(int(limit), 50))}
        if page_token:
            query["pageToken"] = page_token
        body: dict[str, Any] = {"needAnswer": bool(need_answer), "sort": "CREATED_AT_DESC"}
        if date_from:
            body["dateFrom"] = date_from
        if date_to:
            body["dateTo"] = date_to
        response = await self._request(
            "POST",
            f"/v1/businesses/{int(business_id)}/goods-questions?{urlencode(query)}",
            json_body=body,
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        result = response.get("result") or response
        return {
            "questions": [item for item in result.get("questions") or [] if isinstance(item, dict)],
            "totalCount": int(result.get("totalCount") or 0),
            "paging": {"nextPageToken": str((result.get("paging") or {}).get("nextPageToken") or "")},
        }

    async def reply_to_question(
        self, business_id: int, question_id: int, text: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/businesses/{int(business_id)}/goods-questions/update",
            json_body={
                "parentEntityId": {"id": int(question_id), "type": "QUESTION"},
                "text": text,
                "operationType": "CREATE",
            },
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        return response.get("result") or response

    async def get_campaign_offer_prices(
        self, campaign_id: int, offer_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Read campaign overrides; explicit SKU requests must not use pagination."""
        unique_ids = list(dict.fromkeys(str(value) for value in offer_ids if value))
        if not unique_ids or len(unique_ids) > 500:
            raise YandexApiError("每次查询店铺价格必须包含 1–500 个商品")
        response = await self._request(
            "POST", f"/v2/campaigns/{int(campaign_id)}/offer-prices",
            json_body={"offerIds": unique_ids}, attempts=1,
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        result = response.get("result") or response
        return [item for item in result.get("offers") or [] if isinstance(item, dict)]

    async def get_business_offer_prices(
        self, business_id: int, offer_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Read catalogue prices and media together, not buyer-discounted prices."""
        unique_ids = list(dict.fromkeys(str(value) for value in offer_ids if value))
        if not unique_ids or len(unique_ids) > 100:
            raise YandexApiError("每次查询目录价格必须包含 1–100 个商品")
        response = await self._request(
            "POST", f"/v2/businesses/{int(business_id)}/offer-mappings",
            json_body={"offerIds": unique_ids}, attempts=1,
        )
        if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
            raise YandexApiError(_api_message(response), details=response)
        result = response.get("result") or response
        return [
            {
                "offerId": item["offer"].get("offerId"),
                "price": item["offer"].get("basicPrice"),
                "name": item["offer"].get("name"),
                "vendor": item["offer"].get("vendor"),
                "archived": item["offer"].get("archived"),
                "pictures": item["offer"].get("pictures"),
                "mediaFiles": item["offer"].get("mediaFiles"),
                "campaigns": item["offer"].get("campaigns"),
                "mapping": item.get("mapping"),
                "showcaseUrls": item.get("showcaseUrls"),
            }
            for item in result.get("offerMappings") or []
            if isinstance(item, dict) and isinstance(item.get("offer"), dict)
        ]

    async def get_order_stats(
        self, campaign_id: int, order_ids: list[int]
    ) -> list[dict[str, Any]]:
        """Read the financial statistics for the orders already on the current page."""
        unique_ids = list(dict.fromkeys(int(value) for value in order_ids))
        if not unique_ids or len(unique_ids) > 200:
            raise YandexApiError("每次查询订单统计必须包含 1–200 个订单")
        collected: dict[int, dict[str, Any]] = {}
        page_token = ""
        seen_tokens: set[str] = set()
        while True:
            query: dict[str, str | int] = {"limit": 200}
            if page_token:
                query["pageToken"] = page_token
            response = await self._request(
                "POST", f"/v2/campaigns/{int(campaign_id)}/stats/orders?{urlencode(query)}",
                json_body={"orders": unique_ids}, attempts=1,
            )
            if str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
                raise YandexApiError(_api_message(response), details=response)
            result = response.get("result") or response
            for item in result.get("orders") or []:
                if isinstance(item, dict) and item.get("id") in unique_ids:
                    collected[int(item["id"])] = item
            page_token = str((result.get("paging") or {}).get("nextPageToken") or "")
            if not page_token or len(collected) == len(unique_ids):
                return list(collected.values())
            if page_token in seen_tokens or len(seen_tokens) >= len(unique_ids):
                raise YandexApiError("订单统计分页异常，请刷新重试")
            seen_tokens.add(page_token)

    async def resolve_stock_target(
        self,
        business_id: int,
        campaign_id: int,
        placement_type: str,
    ) -> StockTarget:
        normalized_placement = placement_type.strip().upper()
        if normalized_placement not in {"FBS", "DBS", "EXPRESS"}:
            raise YandexApiError(
                f"{normalized_placement or 'UNKNOWN'} 店铺不支持由程序写入卖家库存"
            )

        v3_error: YandexApiError | None = None
        try:
            response = await self._request(
                "POST",
                f"/v3/businesses/{business_id}/warehouses",
                json_body={},
            )
            warehouses = ((response.get("result") or {}).get("warehouses") or [])
            candidates: list[dict[str, Any]] = []
            for warehouse in warehouses:
                models = warehouse.get("models") or []
                if any(
                    str(model.get("placementType") or "").upper() == normalized_placement
                    and str(model.get("apiAvailability") or "").upper() == "AVAILABLE"
                    for model in models
                    if isinstance(model, dict)
                ):
                    candidates.append(warehouse)
            if candidates:
                warehouse = candidates[0]
                return StockTarget(
                    method="business",
                    warehouse_id=int(warehouse["id"]),
                    warehouse_name=str(warehouse.get("name") or f"仓库 {warehouse['id']}"),
                )
        except YandexApiError as exc:
            v3_error = exc
            if exc.status_code not in {400, 404, 420}:
                raise

        # 仓库组柜台不能使用 v3 库存接口；当前官方列表通过 v2 返回 groupInfo。
        try:
            response = await self._request(
                "POST",
                f"/v2/businesses/{business_id}/warehouses",
                json_body={"campaignIds": [int(campaign_id)]},
            )
            warehouses = ((response.get("result") or {}).get("warehouses") or [])
            for warehouse in warehouses:
                if not isinstance(warehouse, dict):
                    continue
                if int(warehouse.get("campaignId") or 0) != int(campaign_id):
                    continue
                group_info = warehouse.get("groupInfo") or {}
                if not group_info:
                    continue
                return StockTarget(
                    method="campaign",
                    warehouse_id=int(warehouse["id"]) if warehouse.get("id") else None,
                    warehouse_name=str(
                        group_info.get("name")
                        or warehouse.get("name")
                        or "统一库存仓库组"
                    ),
                )

            # 兼容 Yandex 旧响应，避免已经启用仓库组的店铺在过渡期中断。
            groups = ((response.get("result") or {}).get("warehouseGroups") or [])
            for group in groups:
                main_warehouse = group.get("mainWarehouse") or {}
                members = group.get("warehouses") or []
                group_campaign_ids = {
                    int(item["campaignId"])
                    for item in [main_warehouse, *members]
                    if isinstance(item, dict) and item.get("campaignId")
                }
                if campaign_id in group_campaign_ids:
                    warehouse_id = main_warehouse.get("id")
                    return StockTarget(
                        method="campaign",
                        warehouse_id=int(warehouse_id) if warehouse_id else None,
                        warehouse_name=str(
                            group.get("name")
                            or main_warehouse.get("name")
                            or "统一库存仓库组"
                        ),
                    )
        except YandexApiError:
            raise

        if v3_error:
            raise v3_error
        raise YandexApiError(
            f"没有找到可通过 API 写库存的 {normalized_placement} 仓库；请先在卖家后台启用仓库 API"
        )

    async def resume_offer_display(self, campaign_id: int, offer_id: str) -> dict[str, Any]:
        return await self.resume_offer_displays(campaign_id, [offer_id])

    async def resume_offer_displays(
        self,
        campaign_id: int,
        offer_ids: list[str],
    ) -> dict[str, Any]:
        unique_offer_ids = list(dict.fromkeys(str(value) for value in offer_ids if value))
        if not unique_offer_ids or len(unique_offer_ids) > 500:
            raise YandexApiError("每次恢复展示必须包含 1–500 个商品")
        response = await self._request(
            "POST",
            f"/v2/campaigns/{campaign_id}/hidden-offers/delete",
            json_body={
                "hiddenOffers": [{"offerId": offer_id} for offer_id in unique_offer_ids]
            },
        )
        if str(response.get("status", "OK")).upper() != "OK":
            raise YandexApiError(_api_message(response), details=response)
        return response

    async def update_offer_stock(
        self,
        business_id: int,
        campaign_id: int,
        offer_id: str,
        count: int,
        target: StockTarget,
    ) -> dict[str, Any]:
        return await self.update_offer_stocks(
            business_id,
            campaign_id,
            [offer_id],
            count,
            target,
        )

    async def update_offer_stocks(
        self,
        business_id: int,
        campaign_id: int,
        offer_ids: list[str],
        count: int,
        target: StockTarget,
    ) -> dict[str, Any]:
        if count < 0 or count > 2_000_000_000:
            raise YandexApiError("库存必须在 0–2000000000 件之间")
        unique_offer_ids = list(dict.fromkeys(str(value) for value in offer_ids if value))
        if not unique_offer_ids or len(unique_offer_ids) > 2000:
            raise YandexApiError("每次库存更新必须包含 1–2000 个商品")
        if target.method == "business":
            if not target.warehouse_id:
                raise YandexApiError("库存目标缺少 warehouseId")
            path = f"/v3/businesses/{business_id}/offers/stocks/update"
            body = {
                "skuItems": [
                    {
                        "sku": offer_id,
                        "partnerWarehouseId": target.warehouse_id,
                        "count": count,
                    }
                    for offer_id in unique_offer_ids
                ]
            }
        elif target.method == "campaign":
            path = f"/v2/campaigns/{campaign_id}/offers/stocks"
            body = {
                "skus": [
                    {"sku": offer_id, "items": [{"count": count}]}
                    for offer_id in unique_offer_ids
                ]
            }
        else:
            raise YandexApiError(f"未知库存接口类型：{target.method}")
        response = await self._request(
            "POST" if target.method == "business" else "PUT",
            path,
            json_body=body,
        )
        if str(response.get("status", "OK")).upper() != "OK":
            raise YandexApiError(_api_message(response), details=response)
        return response
