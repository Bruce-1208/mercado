"""Original Yandex offers: read-only preview, explicit submission, verified status.

No Mercado payload, marketSku mapping or Yandex-only image filter is reused.
The caller owns authorization, public-image verification and preview approval.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from erp.ai_original_platforms import validate_platform_listing
from yandex.app.yandex_api import StockTarget, YandexApiError, YandexSellerClient

RULE_SOURCES = [
    "https://yandex.ru/dev/market/partner-api/doc/ru/reference/business-offer-mappings/updateOfferMappings",
    "https://yandex.ru/dev/market/partner-api/doc/ru/reference/content/getCategoryContentParameters",
    "https://yandex.ru/dev/market/partner-api/doc/ru/reference/offers/getCampaignOffers",
    "https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/paths/v3_businesses_businessId_offers_stocks_update.yaml",
    "https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/paths/v2_campaigns_campaignId_hidden-offers_delete.yaml",
    "https://github.com/yandex-market/yandex-market-partner-api/blob/main/openapi/paths/v2_campaigns_campaignId_offer-prices_updates.yaml",
]


def _sync(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    # The public interface is synchronous, including calls from an async host.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, awaitable).result()


def _object(value):
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except (ValueError, TypeError):
            pass
    return {}


def _positive_number(value, label, *, integer=False, maximum=None):
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError()
        if integer and number != number.to_integral_value():
            raise ValueError()
        if maximum is not None and number > maximum:
            raise ValueError()
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError(f"{label}必须是有效的正{'整数' if integer else '数'}") from None
    return int(number) if integer else number


def _public_image(value):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (len(value) > 2000 or parsed.scheme != "https" or not host or parsed.username
            or parsed.password or parsed.fragment or "." not in host
            or host.endswith((".localhost", ".local", ".internal", ".test"))):
        raise ValueError("商品图片必须是可公开访问的 HTTPS 地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("商品图片不能使用本机或内网地址")
    # DNS, redirects, image dimensions/content are checked by the root service.
    return value


def _messages(value):
    if isinstance(value, list):
        return [message for item in value for message in _messages(item)]
    if isinstance(value, Mapping):
        message = value.get("message") or value.get("description") or value.get("reason") or value.get("code") or value.get("type")
        return [str(message)] if message else [json.dumps(value, ensure_ascii=False)]
    return [str(value)] if value not in (None, "") else []


def _issues(response, key="errors"):
    found = []
    if isinstance(response, Mapping):
        found.extend(_messages(response.get(key)))
        for field in ("result", "results", "offers", "offerMappings"):
            child = response.get(field)
            if isinstance(child, list):
                for item in child:
                    found.extend(_issues(item, key))
            elif isinstance(child, Mapping):
                found.extend(_issues(child, key))
    return list(dict.fromkeys(found))


class _Rejected(YandexApiError):
    """The platform explicitly rejected a write, as opposed to an unknown result."""


def _checked(response):
    errors = _issues(response)
    if not isinstance(response, Mapping) or str(response.get("status", "OK")).upper() not in {"OK", "SUCCESS"}:
        errors = errors or _messages(response)
    if errors:
        raise _Rejected("；".join(errors), details=response)
    return response


def _parameter_values(parameters, supplied):
    """Validate IDs and values, including enum dependencies; never guess attributes."""
    if not isinstance(supplied, Mapping):
        raise ValueError("additional_attributes 必须是参数 ID 到值的对象")
    raw = []
    if "parameterValues" in supplied:
        if set(supplied) != {"parameterValues"} or not isinstance(supplied["parameterValues"], list):
            raise ValueError("parameterValues 必须是官方参数对象数组，不能混用其他键")
        raw = deepcopy(supplied["parameterValues"])
    else:
        for key, values in supplied.items():
            for value in values if isinstance(values, list) else [values]:
                raw.append({**(dict(value) if isinstance(value, Mapping) else {"value": value}), "parameterId": key})
    schema = {int(item["id"]): item for item in parameters}
    output, grouped = [], defaultdict(list)
    for entry in raw:
        if not isinstance(entry, Mapping) or set(entry) - {"parameterId", "valueId", "value", "unitId"}:
            raise ValueError("类目参数格式无效")
        pid = _positive_number(entry.get("parameterId"), "参数编号", integer=True)
        if pid not in schema:
            raise ValueError(f"参数 {pid} 不属于所选类目")
        definition = schema[pid]
        label = str(definition.get("name") or pid)
        value = entry.get("value")
        if value in (None, "") and entry.get("valueId") in (None, ""):
            continue
        if grouped[pid] and not definition.get("multivalue"):
            raise ValueError(f"{label}不允许多个值")
        kind = str(definition.get("type") or "TEXT").upper()
        item = {"parameterId": pid}
        if kind == "ENUM":
            allowed = {str(option["id"]): option for option in definition.get("values") or []}
            option = allowed.get(str(entry.get("valueId", value)))
            if option is None and entry.get("valueId") in (None, ""):
                option = next((option for option in allowed.values() if str(option.get("value")) == str(value)), None)
            if option:
                item["valueId"] = int(option["id"])
            elif definition.get("allowCustomValues") and entry.get("valueId") in (None, "") and isinstance(value, str) and value.strip():
                item["value"] = value.strip()
            else:
                raise ValueError(f"{label}请选择类目允许的枚举值")
        elif entry.get("valueId") is not None:
            raise ValueError(f"{label}不能使用枚举编号")
        elif kind == "BOOLEAN":
            if isinstance(value, bool):
                item["value"] = str(value).lower()
            elif str(value).strip().lower() in {"true", "false"}:
                item["value"] = str(value).strip().lower()
            else:
                raise ValueError(f"{label}必须是 true 或 false")
        elif kind == "NUMERIC":
            try:
                number = Decimal(str(value))
                if isinstance(value, bool) or not number.is_finite():
                    raise ValueError()
                constraints = definition.get("constraints") or {}
                if constraints.get("minValue") is not None and number < Decimal(str(constraints["minValue"])):
                    raise ValueError()
                if constraints.get("maxValue") is not None and number > Decimal(str(constraints["maxValue"])):
                    raise ValueError()
            except (InvalidOperation, ValueError):
                raise ValueError(f"{label}必须是类目范围内的数字") from None
            item["value"] = format(number, "f")
        elif kind == "TEXT":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError(f"{label}必须是文本")
            item["value"] = str(value).strip()
            maximum = (definition.get("constraints") or {}).get("maxLength")
            if maximum is not None and len(item["value"]) > int(maximum):
                raise ValueError(f"{label}超过类目字符上限 {maximum}")
        else:
            raise ValueError(f"暂不支持参数 {label} 的类型 {kind}")
        units = definition.get("unit") or {}
        unit_id = entry.get("unitId", units.get("defaultUnitId"))
        if unit_id is not None:
            if kind != "NUMERIC" or str(unit_id) not in {str(unit["id"]) for unit in units.get("units") or []}:
                raise ValueError(f"{label}的计量单位无效")
            item["unitId"] = int(unit_id)
        grouped[pid].append(item)
        output.append(item)
    missing = [str(item.get("name") or item["id"]) for item in parameters if item.get("required") and not grouped[int(item["id"])]]
    if missing:
        raise ValueError("缺少类目必填属性：" + "、".join(missing))
    if len(output) > 300:
        raise ValueError("类目参数最多 300 个值")
    for pid, entries in grouped.items():
        for restriction in schema[pid].get("valueRestrictions") or []:
            limiting = grouped.get(int(restriction["limitingParameterId"]), [])
            selected = {entry.get("valueId") for entry in limiting}
            for rule in restriction.get("limitedValues") or []:
                if rule.get("limitingOptionValueId") in selected:
                    allowed_ids = set(rule.get("optionValueIds") or [])
                    if any(entry.get("valueId") not in allowed_ids for entry in entries):
                        raise ValueError(f"{schema[pid].get('name') or pid}与其他已选属性不匹配")
    return output


class YandexOriginalPublisher:
    def __init__(self, store: dict, token: str, *, client=None):
        self.store = dict(store)
        self.business_id = _positive_number(store.get("business_id"), "Yandex business_id", integer=True)
        self.campaign_id = _positive_number(store.get("campaign_id"), "Yandex campaign_id", integer=True)
        self.placement_type = str(store.get("placement_type") or "").upper()
        self._secrets = [str(token or "")]
        self._secrets += [str(value) for key, value in store.items()
                          if re.search(r"token|secret|password|api.?key", str(key), re.I)
                          and isinstance(value, str)]
        if client is None and not str(token or "").strip():
            raise ValueError("Yandex 店铺缺少 API 凭证")
        self.client = client if client is not None else YandexSellerClient(token)

    def _safe_error(self, value):
        text = str(value)
        for secret in sorted(set(self._secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[已隐藏凭证]")
        text = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1[已隐藏凭证]", text)
        text = re.sub(r"(?i)((?:api[-_ ]?key|authorization|(?:refresh[_-]?)?token|(?:client[_-]?)?secret|password)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;&}]+", r"\1[已隐藏凭证]", text)
        return text[:2000]

    def get_schema(self, category_id):
        category_id = _positive_number(category_id, "Yandex 类目编号", integer=True)
        try:
            parameters = _sync(self.client.get_category_parameters(self.business_id, category_id))
        except Exception as exc:
            raise ValueError(self._safe_error(exc)) from None
        fields = [{"id": str(item["id"]), "label": str(item.get("name") or item["id"]),
                   "required": bool(item.get("required")),
                   "type": {"NUMERIC": "number", "BOOLEAN": "boolean", "ENUM": "enum"}.get(str(item.get("type")).upper(), "text"),
                   "choices": [{"value": str(option["id"]), "label": str(option.get("value") or option["id"])} for option in item.get("values") or []],
                   "multivalue": bool(item.get("multivalue")), "allow_custom_values": bool(item.get("allowCustomValues")),
                   "units": deepcopy((item.get("unit") or {}).get("units") or []),
                   "default_unit_id": (item.get("unit") or {}).get("defaultUnitId"),
                   "constraints": deepcopy(item.get("constraints") or {}),
                   "value_restrictions": deepcopy(item.get("valueRestrictions") or [])} for item in parameters]
        return {"platform": "yandex", "category_id": category_id, "fields": fields,
                "parameters": deepcopy(parameters), "rule_sources": RULE_SOURCES,
                "attribute_format": "parameter ID → value / {valueId} / {value, unitId}; arrays for multivalue"}

    def prepare(self, row, settings):
        settings = dict(settings or {})
        errors, warnings = [], []
        snapshot = _object(row.get("source_snapshot_json"))
        listings = _object(snapshot.get("marketplace_listings")) or _object(row.get("marketplace_listings"))
        draft = _object(listings.get("yandex"))
        original = _object(snapshot.get("original_1688")) or _object(row.get("original_1688"))
        sku = str(settings.get("sku") or (f"ai-original-{row['id']}" if row.get("id") else "")).strip()
        result = {"platform": "yandex", "sku": sku, "errors": errors, "warnings": warnings,
                  "business_id": self.business_id, "campaign_id": self.campaign_id,
                  "payload": {}, "rule_sources": RULE_SOURCES}
        if not sku or len(sku) > 255 or re.search(r"[\x00-\x1f\x7f]", sku):
            errors.append("SKU 必须为 1–255 字符且不含控制字符")
        if not draft or draft.get("status") != "completed":
            errors.append("请先完成 Yandex 俄语文案和主图生成")
        validation = validate_platform_listing(draft, "yandex")
        errors.extend(validation["errors"])
        warnings.extend(validation["warnings"])
        if any(isinstance(values, list) and len(values) > 1 for values in (original.get("variations"), draft.get("source_variations"), draft.get("variations"), row.get("variations"))):
            errors.append("源商品包含多个 SKU，请先拆成具有独立图片、规格、条码和库存的商品记录")
        if str(settings.get("condition") or "new").lower() not in {"new", "new_new"}:
            errors.append("当前自动上架仅支持新品；二手或瑕疵品需要独立品相资料")
        marker = "ai-src-" + hashlib.sha256(f"{row.get('id')}:{row.get('source_item_id')}".encode()).hexdigest()[:12]
        try:
            category_id = _positive_number(settings.get("category_id"), "Yandex 类目编号", integer=True)
            price = _positive_number(settings.get("price"), "售价", integer=True)
            quantity = _positive_number(settings.get("quantity"), "库存", integer=True, maximum=2_000_000_000)
            currency = str(settings.get("currency") or "").upper()
            if currency not in {"RUR", "CNY"}:
                raise ValueError("Yandex 币种必须明确选择 RUR 或 CNY")
            brand = str(settings.get("brand") or "").strip()
            if not brand:
                raise ValueError("请填写真实品牌；无品牌商品须明确填写平台无品牌值")
            image_url = _public_image(settings.get("image_url"))
            dimensions = {name: float(_positive_number(settings.get(f"package_{name}_cm"), f"包装{name}(cm)")) for name in ("length", "width", "height")}
            dimensions["weight"] = float(_positive_number(settings.get("weight_g"), "包装重量(g)") / 1000)
            if any(not Decimal(str(value)).is_finite() for value in dimensions.values()):
                raise ValueError("包装尺寸或重量过大")
            gtin = str(settings.get("gtin") or "").strip()
            if gtin and not re.fullmatch(r"\d{8}|\d{12,14}", gtin):
                raise ValueError("条码须为真实 EAN/UPC/GTIN 数字编码（8、12、13 或 14 位）")
            if gtin:
                digits = [int(char) for char in gtin]
                expected = (10 - sum(value * (3 if index % 2 == 0 else 1) for index, value in enumerate(reversed(digits[:-1]))) % 10) % 10
                if digits[-1] != expected:
                    raise ValueError("条码校验位不正确，请使用真实商品编码")
            else:
                warnings.append("未填写条码；部分品类或品牌必须提供有效 GTIN，平台仍可能要求补充")
            if settings.get("gtin_exemption"):
                warnings.append("Yandex 不使用 Amazon 的 GTIN 豁免字段，本次不会提交豁免声明")
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append(str(exc))
            return result
        result.update(category_id=category_id, price=price, currency=currency, quantity=quantity)
        if errors:
            return result
        try:
            schema = self.get_schema(category_id)
            values = _parameter_values(schema["parameters"], settings.get("additional_attributes") or {})
            result["schema"] = schema
            remote = _sync(self._prepare_remote(sku, marker, currency))
            warnings.extend(remote.pop("remote_warnings", []))
            result.update(remote)
            offer = {"offerId": sku, "name": draft["title"].strip(), "marketCategoryId": category_id,
                     "description": draft["description"].strip(), "pictures": [image_url], "vendor": brand,
                     "weightDimensions": dimensions, "tags": [marker]}
            if values:
                offer["parameterValues"] = values
            if gtin:
                offer["barcodes"] = [gtin]
            result["payload"] = {"offerMappings": [{"offer": offer}], "onlyPartnerMediaContent": False}
            result["price_payload"] = {"offers": [{"offerId": sku, "price": {"value": price, "currencyId": currency}}]}
        except Exception as exc:
            errors.append(self._safe_error(exc))
        return result

    async def _prepare_remote(self, sku, marker, currency):
        if self.placement_type not in {"FBS", "DBS", "EXPRESS"}:
            raise ValueError("当前自动上架支持 FBS/DBS/EXPRESS；平台仓模式须先完成入仓供货")
        token_info = await self.client.get_token_info()
        scopes = {str(value).lower().replace("_", "-") for value in token_info.get("auth_scopes") or []}
        if "all-methods" not in scopes:
            missing = {"offers-and-cards-management", "pricing"} - scopes
            if missing:
                raise ValueError("Yandex API 缺少写入权限：" + "、".join(sorted(missing)))
        business = await self.client.get_business_settings(self.business_id)
        account_currency = str(business.get("currency") or "").upper().replace("RUB", "RUR")
        if account_currency and account_currency != currency:
            raise ValueError(f"币种 {currency} 与店铺设置 {account_currency} 不一致")
        stock = await self.client.resolve_stock_target(self.business_id, self.campaign_id, self.placement_type)
        await self._ensure_sku_ownership(sku, marker)
        scope = "business" if business.get("onlyDefaultPrice") else "campaign"
        return {"stock_target": stock.public_dict(), "price_scope": scope,
                "remote_warnings": ["价格将应用到该商品的所有店铺"] if scope == "business" else []}

    async def _ensure_sku_ownership(self, sku, marker):
        if not re.fullmatch(r"ai-src-[0-9a-f]{12}", str(marker or "")):
            raise ValueError("缺少商品来源标记，请重新预检")
        existing = _checked(await self.client._request("POST", f"/v2/businesses/{self.business_id}/offer-mappings", json_body={"offerIds": [sku]}, attempts=1))
        mappings = (existing.get("result") or existing).get("offerMappings") or []
        for mapping in mappings:
            offer = mapping.get("offer") or {}
            if str(offer.get("offerId")) == sku and marker not in (offer.get("tags") or []):
                raise ValueError("该 SKU 已用于其他商品，不能覆盖；请指定新的 SKU")

    def publish(self, prepared):
        return _sync(self._publish(deepcopy(prepared)))

    async def _publish(self, prepared):
        sku = str(prepared.get("sku") or "")
        errors = list(prepared.get("errors") or [])
        offers = (prepared.get("payload") or {}).get("offerMappings") or []
        if (prepared.get("platform") != "yandex" or prepared.get("business_id") != self.business_id
                or prepared.get("campaign_id") != self.campaign_id or len(offers) != 1
                or (offers[0].get("offer") or {}).get("offerId") != sku or offers[0].get("mapping") is not None):
            errors.append("上架预览与当前店铺或 SKU 不匹配，请重新预检")
        try:
            target = StockTarget(**prepared["stock_target"])
            quantity = _positive_number(prepared.get("quantity"), "库存", integer=True, maximum=2_000_000_000)
            if prepared.get("price_scope") not in {"business", "campaign"}:
                raise ValueError("缺少价格作用范围")
            if not prepared.get("price_payload"):
                raise ValueError("缺少价格请求")
            prices = prepared["price_payload"].get("offers") or []
            expected_price = {"value": prepared.get("price"), "currencyId": prepared.get("currency")}
            if len(prices) != 1 or prices[0].get("offerId") != sku or prices[0].get("price") != expected_price:
                raise ValueError("价格请求与预览商品不一致")
            if target.method not in {"business", "campaign"} or (target.method == "business" and not target.warehouse_id):
                raise ValueError("库存目标无效")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(str(exc))
        if errors:
            return {"state": "failed", "platform": "yandex", "sku": sku, "remote_id": "", "live": False,
                    "message": "上架预检未通过", "issues": [self._safe_error(value) for value in errors], "steps": [],
                    "submission_unknown": False, "retryable": True}
        try:
            # Recheck after approval: another item may have claimed the SKU meanwhile.
            tags = offers[0]["offer"].get("tags") or []
            await self._ensure_sku_ownership(sku, tags[0] if len(tags) == 1 else None)
        except Exception as exc:
            return {"state": "failed", "platform": "yandex", "sku": sku, "remote_id": "", "live": False,
                    "failed_stage": "preflight", "retryable": True, "submission_unknown": False,
                    "message": "提交前 SKU 校验失败", "issues": [self._safe_error(exc)], "steps": []}
        steps, warnings = [], list(prepared.get("warnings") or [])
        stage = "offer"
        try:
            response = _checked(await self.client._request("POST", f"/v2/businesses/{self.business_id}/offer-mappings/update", json_body=prepared["payload"], attempts=1))
            warnings.extend(_issues(response, "warnings"))
            steps.append(stage)
            stage = "price"
            scope = prepared["price_scope"]
            path = f"/v2/businesses/{self.business_id}/offer-prices/updates" if scope == "business" else f"/v2/campaigns/{self.campaign_id}/offer-prices/updates"
            response = _checked(await self.client._request("POST", path, json_body=prepared["price_payload"], attempts=1))
            warnings.extend(_issues(response, "warnings"))
            steps.append(stage)
            stage = "stock"
            response = _checked(await self.client.update_offer_stock(self.business_id, self.campaign_id, sku, quantity, target))
            warnings.extend(_issues(response, "warnings"))
            steps.append(stage)
            stage = "resume"
            response = _checked(await self.client.resume_offer_display(self.campaign_id, sku))
            warnings.extend(_issues(response, "warnings"))
            steps.append(stage)
        except Exception as exc:
            explicit_rejection = isinstance(exc, _Rejected) or (isinstance(exc, YandexApiError) and exc.status_code is not None and 400 <= exc.status_code < 500)
            unknown = not explicit_rejection
            return {"state": "partial" if steps or unknown else "failed", "platform": "yandex", "sku": sku,
                    "remote_id": sku if steps else "", "live": False, "failed_stage": stage,
                    "submission_unknown": unknown, "retryable": not unknown, "steps": steps,
                    "message": (f"{stage} 提交结果不明，请保留同一 SKU 并先刷新平台状态，勿立即重发" if unknown
                                else f"{stage} 步骤被平台拒绝，请保留同一 SKU 修正或重试"),
                    "issues": [self._safe_error(exc)], "warnings": [self._safe_error(value) for value in warnings]}
        result = await self._status(sku)
        result.update(steps=steps, warnings=list(dict.fromkeys(self._safe_error(value) for value in warnings + result.get("warnings", []))))
        return result

    def status(self, sku):
        sku = str(sku or "").strip()
        if not sku or len(sku) > 255:
            raise ValueError("SKU 无效")
        return _sync(self._status(sku))

    async def _status(self, sku):
        results = await asyncio.gather(
            self.client.get_campaign_offers(self.campaign_id, offer_ids=[sku]),
            self.client.get_offer_cards(self.business_id, [sku]),
            self.client.get_hidden_offer_ids(self.campaign_id, [sku]),
            return_exceptions=True,
        )
        issues, warnings = [], []
        for label, value in zip(("店铺状态", "商品卡", "展示状态"), results):
            if isinstance(value, Exception):
                issues.append(f"{label}读取失败：{self._safe_error(value)}")
        offers = results[0].get("offers", []) if isinstance(results[0], Mapping) else []
        cards = results[1] if isinstance(results[1], list) else []
        offer = next((item for item in offers if str(item.get("offerId")) == sku), {})
        card = next((item for item in cards if str(item.get("offerId")) == sku), {})
        known = bool(offer or card)
        not_found = not known and all(
            isinstance(value, (Mapping, list)) or (isinstance(value, YandexApiError) and value.status_code == 404)
            for value in results[:2]
        )
        for value in (offer, card):
            issues.extend(_issues(value))
            warnings.extend(_issues(value, "warnings"))
        remote_status = str(offer.get("status") or "UNKNOWN")
        if remote_status in {"REJECTED_BY_MARKET", "DISABLED_AUTOMATICALLY", "NO_CARD", "NO_STOCKS", "ARCHIVED", "DISABLED_BY_PARTNER"}:
            issues.append(f"Yandex 商品状态需处理：{remote_status}")
        hidden = sku in results[2] if isinstance(results[2], list) else None
        if hidden:
            issues.append("商品仍处于隐藏状态")
        live = remote_status == "PUBLISHED" and hidden is False and not issues
        return {"state": "partial" if issues else "submitted", "platform": "yandex", "sku": sku,
                "remote_id": str((card.get("mapping") or {}).get("marketSku") or sku) if known else "",
                "remote_status": remote_status, "card_status": str(card.get("cardStatus") or "UNKNOWN"),
                "live": live, "hidden": hidden, "not_found": not_found, "submission_unknown": not known,
                "retryable": known and bool(issues) and not any(isinstance(value, Exception) for value in results),
                "issues": list(dict.fromkeys(self._safe_error(value) for value in issues)),
                "warnings": list(dict.fromkeys(self._safe_error(value) for value in warnings)),
                "message": ("Yandex 已确认商品在所选店铺展示" if live else
                            "暂未查到商品，可能仍在同步；请保留同一 SKU，稍后刷新状态，勿立即重发" if not_found else
                            "尚未确认在所选店铺展示；请查看平台状态及待补信息")}
