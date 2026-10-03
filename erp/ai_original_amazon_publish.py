"""Publish reviewed, standalone Amazon US listings through SP-API.

Reference contracts (checked 2026-10-03):
https://developer-docs.amazon.com/sp-api/docs/connecting-to-the-selling-partner-api
https://developer-docs.amazon.com/sp-api/docs/retrieve-a-product-type-definition
https://developer-docs.amazon.com/sp-api/reference/putlistingsitem
https://developer-docs.amazon.com/sp-api/docs/preview-errors-before-creating-a-listing
https://developer-docs.amazon.com/sp-api/docs/listings-apis-faq

Only ``publish`` sends a catalog-changing request. ``prepare`` uses Amazon's
VALIDATION_PREVIEW and fails closed when current requirements cannot be checked.
ACCEPTED means queued for processing, never proof that an offer is buyable.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import math
import re
import threading
import time
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import requests

from erp.ai_original_platforms import validate_platform_listing

US_MARKETPLACE_ID = "ATVPDKIKX0DER"
SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"
LWA_URL = "https://api.amazon.com/auth/o2/token"
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_NEXT: dict[str, float] = {}
_RELATIONSHIP_FIELDS = frozenset({
    "parentage_level", "child_parent_sku_relationship", "variation_theme",
    "parent_sku", "relationship_type",
})
_MANAGED_FIELDS = frozenset({
    "item_name", "product_description", "bullet_point", "item_highlights", "item_highlight",
    "brand", "condition_type", "externally_assigned_product_identifier",
    "supplier_declared_has_product_identifier_exemption", "purchasable_offer",
    "fulfillment_availability", "main_product_image_locator",
})


class AmazonPublishError(ValueError):
    """A safe, user-readable error that never embeds credential responses."""
    def __init__(self, message, *, status_code=None, uncertain_submission=False):
        super().__init__(message)
        self.status_code = status_code
        self.uncertain_submission = uncertain_submission


def _snapshot_and_draft(row):
    raw = row.get("source_snapshot_json") or {}
    try:
        snapshot = dict(raw) if isinstance(raw, Mapping) else json.loads(str(raw))
    except (ValueError, TypeError):
        raise ValueError("商品原始快照格式错误") from None
    if not isinstance(snapshot, Mapping):
        raise ValueError("商品原始快照格式错误")
    listings = row.get("marketplace_listings") or snapshot.get("marketplace_listings") or {}
    draft = listings.get("amazon") if isinstance(listings, Mapping) else None
    if not isinstance(draft, Mapping) or not draft:
        raise ValueError("请先生成并审核 Amazon 美国站原创草稿")
    return snapshot, deepcopy(dict(draft))


def _public_https(url):
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def _valid_gtin(value):
    if not re.fullmatch(r"(?:[0-9]{12}|[0-9]{13}|[0-9]{14})", value):
        return False
    total = sum(int(digit) * (3 if index % 2 == 0 else 1)
                for index, digit in enumerate(reversed(value[:-1])))
    return (total + int(value[-1])) % 10 == 0


def _schema_choices(node):
    if not isinstance(node, Mapping) or not node:
        return []
    if isinstance(node.get("enum"), list):
        names = node.get("enumNames") or []
        return [{"value": value, "label": str(names[index] if index < len(names) else value)}
                for index, value in enumerate(node["enum"])]
    for key in ("items", "properties"):
        nested = node.get(key) or {}
        if key == "properties":
            nested = nested.get("value") or {}
        result = _schema_choices(nested) if nested else []
        if result:
            return result
    return []


def _local_schema_errors(schema, attributes):
    try:
        from jsonschema import Draft201909Validator
        from referencing import Registry
    except ImportError:
        return ["缺少 jsonschema 校验依赖，请安装 jsonschema>=4.18,<5 后重试"]
    try:
        # Amazon's custom meta-schema extends Draft 2019-09. Never resolve
        # untrusted remote refs; Amazon-specific vocabulary is checked by its
        # mandatory server-side preview as well as normal schema validation.
        validator = Draft201909Validator(schema, registry=Registry())
        errors = []
        for error in validator.iter_errors(attributes):
            path = ".".join(str(part) for part in error.absolute_path) or "attributes"
            errors.append(f"{path}: {error.message}")
            if len(errors) >= 30:
                break
        return errors
    except Exception:
        return ["无法完整校验 Amazon 当前类目规则，未提交；请重新获取类目要求后重试"]


class AmazonOriginalPublisher:
    def __init__(self, credentials: dict, *, request=None):
        self._credentials = {key: str(credentials.get(key) or "").strip()
                             for key in ("seller_id", "client_id", "client_secret", "refresh_token")}
        missing = [key for key, value in self._credentials.items() if not value]
        if missing:
            raise AmazonPublishError("Amazon 店铺凭证不完整：" + ", ".join(missing))
        marketplace = str(credentials.get("marketplace_id") or US_MARKETPLACE_ID).strip()
        if marketplace != US_MARKETPLACE_ID:
            raise AmazonPublishError("当前自动上架仅支持 Amazon 美国站")
        self.marketplace_id = marketplace
        self.seller_id = self._credentials["seller_id"]
        self._session = requests.Session() if request is None else None
        self._request = self._session.request if self._session is not None else getattr(request, "request", request)
        self._access_token = ""
        self._token_expires = 0.0
        self._token_lock = threading.Lock()

    def _safe_text(self, value):
        text = str(value or "")
        for secret in (self._credentials["client_secret"], self._credentials["refresh_token"], self._access_token):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[:2000]

    def _issues(self, response):
        result = []
        for item in response.get("issues") or []:
            if not isinstance(item, Mapping):
                continue
            result.append({"code": self._safe_text(item.get("code")),
                           "message": self._safe_text(item.get("message")),
                           "severity": str(item.get("severity") or "ERROR").upper(),
                           "attributeNames": [self._safe_text(value) for value in item.get("attributeNames") or []],
                           "categories": [self._safe_text(value) for value in item.get("categories") or []]})
        return result

    def _request_json(self, method, url, *, authenticated=False, mutation=False, **kwargs):
        headers = {"User-Agent": "MercadoAIOriginal/1.0 (Language=Python)", "Accept": "application/json"}
        if authenticated:
            headers.update({"x-amz-access-token": self._token(),
                            "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")})
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"
        try:
            response = self._request(method, url, headers=headers, timeout=(10, 45),
                                     allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise AmazonPublishError(
                "Amazon 请求超时或连接失败；请刷新上架状态确认结果" if mutation else "暂时无法连接 Amazon，请稍后重试",
                uncertain_submission=mutation,
            ) from None
        status = int(response.status_code)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = {}
        if not 200 <= status < 300:
            if url == LWA_URL:
                message = f"Amazon LWA 授权失败（HTTP {status}），请检查应用与店铺授权凭证"
            else:
                errors = payload.get("errors") if isinstance(payload, Mapping) else None
                details = "; ".join(self._safe_text(item.get("message") or item.get("code"))
                                    for item in (errors or []) if isinstance(item, Mapping))
                message = f"Amazon API 请求失败（HTTP {status}）" + (f"：{details}" if details else "")
            raise AmazonPublishError(message, status_code=status,
                                     uncertain_submission=mutation and status >= 500)
        if not isinstance(payload, dict):
            raise AmazonPublishError("Amazon API 返回了无效响应", uncertain_submission=mutation)
        return payload

    def _token(self):
        with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires:
                return self._access_token
            payload = self._request_json("POST", LWA_URL, data={
                "grant_type": "refresh_token", "refresh_token": self._credentials["refresh_token"],
                "client_id": self._credentials["client_id"], "client_secret": self._credentials["client_secret"],
            })
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise AmazonPublishError("Amazon LWA 未返回有效访问令牌，请检查店铺授权")
            self._access_token = token
            try:
                lifetime = max(0, min(float(payload.get("expires_in", 3600)), 3600) - 60)
            except (TypeError, ValueError):
                lifetime = 0
            self._token_expires = time.monotonic() + lifetime
            return token

    def _api(self, method, path, **kwargs):
        return self._request_json(method, SP_API_BASE + path, authenticated=True, **kwargs)

    def _listing_path(self, sku):
        return f"/listings/2021-08-01/items/{quote(self.seller_id, safe='')}/{quote(str(sku), safe='')}"

    def _schema_document(self, resource):
        parsed = urlparse(str(resource or ""))
        host = (parsed.hostname or "").lower()
        if not _public_https(resource) or not any(host == domain or host.endswith("." + domain)
                                                for domain in ("amazonaws.com", "amazon.com", "amazon.dev")):
            raise AmazonPublishError("Amazon 类目 schema 下载地址无效，未提交")
        # Never attach LWA credentials to a signed S3/public schema URL.
        schema = self._request_json("GET", resource)
        if not isinstance(schema.get("properties"), dict):
            raise AmazonPublishError("Amazon 类目 schema 缺少属性定义，未提交")
        return schema

    def get_schema(self, product_type):
        category = str(product_type or "").strip().upper()
        if not category or not re.fullmatch(r"[A-Z0-9_]{1,128}", category):
            raise AmazonPublishError("请填写有效的 Amazon 产品类型（productType）")
        definition = self._api("GET", "/definitions/2020-09-01/productTypes/" + quote(category, safe=""), params={
            "sellerId": self.seller_id, "marketplaceIds": self.marketplace_id,
            "requirements": "LISTING", "requirementsEnforced": "ENFORCED", "locale": "en_US",
            "productTypeVersion": "LATEST", "parentageLevel": "NONE",
        })
        resource = ((definition.get("schema") or {}).get("link") or {}).get("resource")
        schema = self._schema_document(resource)
        required = set(schema.get("required") or [])
        fields = [{"id": key, "label": str(value.get("title") or key),
                   "required": key in required, "type": value.get("type") or "array",
                   "choices": _schema_choices(value), "description": str(value.get("description") or "")[:1000]}
                  for key, value in schema["properties"].items() if isinstance(value, Mapping)]
        return {"platform": "amazon", "category_id": category, "marketplace_id": self.marketplace_id,
                "fields": fields, "raw_schema": schema,
                "schema_version": str((definition.get("productTypeVersion") or {}).get("version") or ""),
                "notes": "必填标记仅展示顶层要求；条件必填与完整结构在校验和 Amazon 预检中检查。"}

    def _throttle_preview(self):
        # VALIDATION_PREVIEW has a lower limit (1 request/sec) than real PUT.
        key = self.seller_id
        with _PREVIEW_LOCK:
            now = time.monotonic()
            due = max(now, _PREVIEW_NEXT.get(key, now))
            _PREVIEW_NEXT[key] = due + 1.05
        if due > now:
            time.sleep(due - now)

    def _preview(self, sku, payload):
        self._throttle_preview()
        response = self._api("PUT", self._listing_path(sku), params={
            "marketplaceIds": self.marketplace_id, "issueLocale": "en_US",
            "mode": "VALIDATION_PREVIEW", "includedData": "issues,identifiers",
        }, json=deepcopy(payload))
        issues = self._issues(response)
        errors = [item["message"] or item["code"] for item in issues if item["severity"] == "ERROR"]
        warnings = [item["message"] or item["code"] for item in issues if item["severity"] != "ERROR"]
        status = str(response.get("status") or "").upper()
        if status != "VALID" and not errors:
            errors.append("Amazon 预检未明确返回 VALID，未提交上架")
        return {"status": status, "issues": issues, "errors": errors, "warnings": warnings}

    def _check_sku_owner(self, sku, known_sku=False):
        # The root service supplies this flag only from its persistent binding.
        # A random pre-existing Seller Central SKU must never be fully replaced.
        if known_sku:
            return
        try:
            self._api("GET", self._listing_path(sku), params={
                "marketplaceIds": self.marketplace_id, "includedData": "summaries",
            })
        except AmazonPublishError as exc:
            if exc.status_code == 404:
                return
            raise
        raise AmazonPublishError("该 SKU 已在 Amazon 存在且未绑定到当前商品，请更换 SKU，避免覆盖已有刊登")

    def prepare(self, row: dict, settings: dict):
        settings = dict(settings or {})
        sku = str(settings.get("sku") or "").strip()
        errors, warnings = [], []
        prepared = {"platform": "amazon", "sku": sku, "errors": errors, "warnings": warnings,
                    "payload": {}, "marketplace_id": self.marketplace_id, "seller_id": self.seller_id,
                    "_known_sku": settings.get("_known_sku") is True}
        try:
            snapshot, draft = _snapshot_and_draft(row)
        except ValueError as exc:
            errors.append(str(exc))
            return prepared
        validation = validate_platform_listing(draft, "amazon")
        errors.extend(validation["errors"])
        if draft.get("status") in {"queued", "processing", "running", "failed"}:
            errors.append("Amazon 原创草稿尚未完成或生成失败，请先完成并审核草稿")
        warnings.append("Amazon 接受请求后仍会审核和异步处理；只有后续状态为 BUYABLE 才表示可购买。")
        warnings.append("提交前须人工核对商品与图片真实性、品牌使用权及所有类目合规资料。")
        original = snapshot.get("original_1688") or {}
        variant_sets = [source.get(key) for source in (draft, row, original) if isinstance(source, Mapping)
                        for key in ("source_variations", "variations", "skus")]
        if any(values and (not isinstance(values, list) or len(values) > 1) for values in variant_sets):
            errors.append("当前仅支持单 SKU 商品；检测到未展开的变体，请先拆分并核实每个 SKU 的文案、价格和库存")
        if not sku or len(sku) > 40 or re.search(r"[\x00-\x1f\x7f]", sku):
            errors.append("请提供 1–40 个字符且不含控制字符的商家 SKU")
        category = str(settings.get("category_id") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{1,128}", category):
            errors.append("请填写 Amazon 产品类型（productType）")
        brand = str(settings.get("brand") or "").strip()
        if not brand:
            errors.append("请填写已核实且有权使用的真实品牌；无品牌商品也需明确填写获准的品牌值")
        condition = str(settings.get("condition") or "").strip()
        if not condition:
            errors.append("请明确商品状况（例如 new_new），不能由 AI 推断")
        image_url = str(settings.get("image_url") or "").strip()
        if not _public_https(image_url):
            errors.append("主图必须使用 Amazon 可公开访问的 HTTPS 地址，不能使用本机或内网地址")
        currency = str(settings.get("currency") or "USD").strip().upper()
        if currency != "USD":
            errors.append("Amazon 美国站销售价格必须使用 USD")
        try:
            raw_price = settings.get("price")
            price = Decimal(str(raw_price))
            if isinstance(raw_price, bool) or not price.is_finite() or price <= 0 or not math.isfinite(float(price)):
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            errors.append("请填写大于 0 的有效美元售价")
            price = Decimal("0")
        raw_quantity = settings.get("quantity")
        try:
            quantity = int(str(raw_quantity))
            if isinstance(raw_quantity, bool) or quantity < 0:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            errors.append("请填写大于或等于 0 的整数库存")
            quantity = 0
        if quantity == 0:
            warnings.append("库存为 0 时创建的自配送商品通常不可购买。")
        gtin = str(settings.get("gtin") or "").strip()
        exemption = settings.get("gtin_exemption") is True
        if settings.get("gtin_exemption") is not None and not isinstance(settings.get("gtin_exemption"), bool):
            errors.append("GTIN 豁免必须为布尔值，并仅在已获平台批准时勾选")
        if gtin and exemption:
            errors.append("请填写 GTIN 或声明已获批准的 GTIN 豁免，不可同时选择")
        elif gtin and not _valid_gtin(gtin):
            errors.append("GTIN/UPC/EAN 必须为校验位正确的 12、13 或 14 位真实编号")
        elif not gtin and not exemption:
            errors.append("请提供真实 GTIN/UPC/EAN，或明确已获该品牌及类目 GTIN 豁免")
        if exemption:
            warnings.append("GTIN 豁免须已在 Seller Central 获准，声明该字段不会申请或自动获得豁免。")
        extra = settings.get("additional_attributes", {})
        if extra is None:
            extra = {}
        if not isinstance(extra, Mapping):
            errors.append("类目补充属性必须是 JSON 对象，并遵循 Amazon 返回的属性结构")
            extra = {}
        try:
            json.dumps(extra, allow_nan=False)
        except (TypeError, ValueError):
            errors.append("类目补充属性必须包含可序列化的 JSON 值，不能包含 NaN 或无穷大")
        reserved = set(extra) & (_MANAGED_FIELDS | _RELATIONSHIP_FIELDS)
        if reserved:
            errors.append("补充属性不能覆盖基本刊登资料或定义变体：" + ", ".join(sorted(reserved)))
        if errors:
            return prepared
        localized = lambda value: [{"value": value, "language_tag": "en_US", "marketplace_id": self.marketplace_id}]
        attributes = {
            "item_name": localized(draft["title"]), "product_description": localized(draft["description"]),
            "bullet_point": [{"value": value, "language_tag": "en_US", "marketplace_id": self.marketplace_id}
                             for value in draft.get("bullet_points") or []],
            "brand": localized(brand),
            "condition_type": [{"value": condition, "marketplace_id": self.marketplace_id}],
            "main_product_image_locator": [{"media_location": image_url, "marketplace_id": self.marketplace_id}],
            "purchasable_offer": [{"currency": currency, "audience": "ALL", "marketplace_id": self.marketplace_id,
                                   "our_price": [{"schedule": [{"value_with_tax": float(price)}]}]}],
            "fulfillment_availability": [{"fulfillment_channel_code": "DEFAULT", "quantity": quantity}],
            **deepcopy(dict(extra)),
        }
        if gtin:
            attributes["externally_assigned_product_identifier"] = [{
                "type": {12: "upc", 13: "ean", 14: "gtin"}[len(gtin)],
                "value": gtin, "marketplace_id": self.marketplace_id,
            }]
        else:
            attributes["supplier_declared_has_product_identifier_exemption"] = [{"value": True, "marketplace_id": self.marketplace_id}]
        payload = {"productType": category, "requirements": "LISTING", "attributes": attributes}
        prepared["payload"] = payload
        try:
            self._check_sku_owner(sku, prepared["_known_sku"])
            catalog = self.get_schema(category)
            schema = catalog["raw_schema"]
            highlights = str(draft.get("item_highlights") or "").strip()
            if highlights:
                key = next((name for name in ("item_highlights", "item_highlight") if name in schema["properties"]), None)
                if key:
                    attributes[key] = localized(highlights)
                else:
                    warnings.append("当前类目未提供 Item Highlights 属性，标题补充信息未上传。")
            errors.extend(_local_schema_errors(schema, attributes))
            if not errors:
                preview = self._preview(sku, payload)
                prepared["validation_preview"] = preview
                errors.extend(preview["errors"])
                warnings.extend(preview["warnings"])
            prepared["schema_version"] = catalog.get("schema_version") or ""
        except ValueError as exc:
            errors.append(self._safe_text(exc))
        prepared["errors"] = list(dict.fromkeys(errors))
        prepared["warnings"] = list(dict.fromkeys(warnings))
        return prepared

    def _failure(self, sku, errors, *, issues=None, uncertain=False):
        return {"platform": "amazon", "state": "partial" if uncertain else "failed", "sku": sku,
                "remote_id": "", "live": False, "buyable": False, "discoverable": False,
                "submission_unknown": uncertain, "issues": issues or [], "errors": errors,
                "message": "；".join(str(item) for item in errors)}

    def publish(self, prepared):
        sku = str(prepared.get("sku") or "").strip()
        if prepared.get("errors"):
            return self._failure(sku, list(prepared["errors"]))
        payload = deepcopy(prepared.get("payload") or {})
        if (not sku or prepared.get("platform") != "amazon" or payload.get("requirements") != "LISTING"
                or prepared.get("seller_id") != self.seller_id
                or prepared.get("marketplace_id") != self.marketplace_id):
            return self._failure(sku, ["无效的 Amazon 上架准备结果，请重新预检"])
        try:
            # A persisted or delayed preview is not authorization to submit a
            # stale/edited body: re-fetch requirements and preview this exact
            # payload immediately before the catalog-changing call.
            self._check_sku_owner(sku, prepared.get("_known_sku") is True)
            schema = self.get_schema(payload.get("productType"))["raw_schema"]
            errors = _local_schema_errors(schema, payload.get("attributes") or {})
            if errors:
                return self._failure(sku, errors)
            preview = self._preview(sku, payload)
            if preview["errors"]:
                return self._failure(sku, preview["errors"], issues=preview["issues"])
            response = self._api("PUT", self._listing_path(sku), mutation=True, params={
                "marketplaceIds": self.marketplace_id, "issueLocale": "en_US", "includedData": "issues",
            }, json=payload)
        except AmazonPublishError as exc:
            return self._failure(sku, [self._safe_text(exc)], uncertain=exc.uncertain_submission)
        except ValueError as exc:
            return self._failure(sku, [self._safe_text(exc)])
        issues = self._issues(response)
        status = str(response.get("status") or "").upper()
        if status != "ACCEPTED":
            errors = [item["message"] or item["code"] for item in issues if item["severity"] == "ERROR"]
            return self._failure(sku, errors or ["Amazon 未明确接受此次提交，请刷新状态核实"],
                                 issues=issues, uncertain=status != "INVALID")
        identifiers = response.get("identifiers") or []
        asin = next((str(item.get("asin") or "") for item in identifiers if isinstance(item, Mapping)
                     and item.get("marketplaceId") == self.marketplace_id), "")
        return {"platform": "amazon", "state": "submitted", "sku": sku, "remote_id": asin,
                "submission_id": self._safe_text(response.get("submissionId")), "amazon_status": status,
                "message": "Amazon 已接受提交，正在处理；尚未确认商品可购买。", "issues": issues,
                "live": False, "buyable": False, "discoverable": False,
                "warnings": preview["warnings"], "errors": []}

    def status(self, sku):
        sku = str(sku or "").strip()
        if not sku:
            return self._failure(sku, ["缺少商家 SKU，无法查询 Amazon 上架状态"])
        try:
            response = self._api("GET", self._listing_path(sku), params={
                "marketplaceIds": self.marketplace_id, "issueLocale": "en_US",
                "includedData": "summaries,issues,offers,fulfillmentAvailability",
            })
        except AmazonPublishError as exc:
            if exc.status_code == 404:
                return {**self._failure(sku, ["Amazon 暂未查到该 SKU；新提交可能仍在处理中，请稍后刷新"], uncertain=True),
                        "not_found": True, "retryable": False}
            return self._failure(sku, [self._safe_text(exc)], uncertain=True)
        summaries = response.get("summaries") or []
        summary = next((item for item in summaries if isinstance(item, Mapping)
                        and item.get("marketplaceId") == self.marketplace_id), {})
        statuses = summary.get("status") or []
        if not isinstance(statuses, list):
            statuses = []
        buyable, discoverable = "BUYABLE" in statuses, "DISCOVERABLE" in statuses
        issues = self._issues(response)
        blocking = [item for item in issues if item["severity"] == "ERROR"]
        state = "partial" if blocking else "submitted"
        message = ("Amazon 商品已可购买。" if buyable else
                   "Amazon 存在阻断问题，商品尚未可购买。" if blocking else
                   "Amazon 已有此 SKU，尚未确认可购买，请继续查询处理状态。")
        return {"platform": "amazon", "state": state, "sku": sku,
                "remote_id": str(summary.get("asin") or ""), "message": message,
                "live": buyable, "buyable": buyable, "discoverable": discoverable,
                "submission_unknown": False,
                "listing_status": statuses, "issues": issues,
                "errors": [item["message"] or item["code"] for item in blocking]}


__all__ = ["AmazonOriginalPublisher", "AmazonPublishError", "US_MARKETPLACE_ID"]
