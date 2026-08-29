"""把工作台已保存的店铺 Token 接入美客多官方通信 API。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import requests

from erp.mercadolibre_translation import BatchTranslator, translate_texts
from mercado_api.communications import (
    MercadoCommunicationError,
    MercadoCommunicationsClient,
)


GetToken = Callable[[int], Mapping[str, Any] | None]
RefreshToken = Callable[[int], Any]
GetOrderContexts = Callable[[int, list[str]], list[Mapping[str, Any]]]


def _resource_identifier(value: Any) -> str:
    return str(value or "").split("/")[-1].strip()


def _attach_order_contexts(
    token_id: int,
    rows: list[dict[str, Any]],
    identifiers: list[str],
    get_order_contexts: GetOrderContexts | None,
) -> list[dict[str, Any]]:
    if get_order_contexts is None or not identifiers:
        return rows
    contexts = [dict(row or {}) for row in get_order_contexts(token_id, identifiers) or ()]
    lookup: dict[str, dict[str, Any]] = {}
    for context in contexts:
        for key in ("order_id", "pack_id", "shipping_id"):
            identifier = str(context.get(key) or "").strip()
            if identifier:
                lookup.setdefault(identifier, context)
    for row, identifier in zip(rows, identifiers):
        context = lookup.get(str(identifier or "").strip())
        if context:
            row["order_context"] = context
    return rows
PRE_SALE_SUMMARY_STATUSES = (
    "UNANSWERED",
    "ANSWERED",
    "CLOSED_UNANSWERED",
    "UNDER_REVIEW",
    "BANNED",
)
PRE_SALE_SPANISH_SITES = frozenset((
    "MLM", "MLA", "MLC", "MCO", "MLU", "MPE", "MEC",
))


def _pre_sale_site_language(site_id: Any) -> str:
    site = str(site_id or "").strip().upper()[:3]
    if site == "MLB":
        return "pt-BR"
    if site in PRE_SALE_SPANISH_SITES:
        return "es"
    raise ValueError(f"无法识别站点 {site or '(empty)'} 的买家语言")


def _store_client(
    token_id: int,
    *,
    get_token: GetToken,
    refresh_token: RefreshToken,
    http: requests.Session | None = None,
    timeout: int = 30,
) -> tuple[MercadoCommunicationsClient, dict[str, Any]]:
    identifier = int(token_id)
    token = dict(get_token(identifier) or {})
    if not token:
        raise KeyError("店铺授权不存在")
    access_token = str(token.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("该店铺没有 Access Token，请重新授权")

    client_http = http
    if client_http is None:
        client_http = requests.Session()
        client_http.trust_env = False

    def refresh_access_token() -> str:
        refresh_token(identifier)
        refreshed = dict(get_token(identifier) or {})
        refreshed_access_token = str(refreshed.get("access_token") or "").strip()
        if not refreshed_access_token:
            raise ValueError("刷新后没有读取到新的 Access Token")
        token.update(refreshed)
        return refreshed_access_token

    return (
        MercadoCommunicationsClient(
            access_token,
            refresh_access_token=refresh_access_token,
            session=client_http,
            timeout=timeout,
        ),
        token,
    )


def execute_store_communication(
    token_id: int,
    action: str,
    payload: Mapping[str, Any] | None = None,
    *,
    get_token: GetToken,
    refresh_token: RefreshToken,
    get_order_contexts: GetOrderContexts | None = None,
    http: requests.Session | None = None,
    timeout: int = 30,
    translator: BatchTranslator | None = None,
) -> Any:
    """执行白名单内的消息/投诉动作，确保密钥只在服务端读取。"""
    client, token = _store_client(
        token_id,
        get_token=get_token,
        refresh_token=refresh_token,
        http=http,
        timeout=timeout,
    )
    data = dict(payload or {})
    seller_id = str(token.get("meli_user_id") or "").strip()
    normalized_action = str(action or "").strip().lower().replace("_", "-")

    if normalized_action == "pre-sale-translate":
        raw_texts = data.get("texts")
        if not isinstance(raw_texts, list):
            raise ValueError("待翻译内容必须是文本数组")
        source_language = str(data.get("source_language") or "").strip()
        if not source_language:
            source_language = _pre_sale_site_language(data.get("site_id"))
        target_language = str(data.get("target_language") or "zh-CN").strip()
        translations = translate_texts(
            raw_texts,
            source_language,
            target_language,
            translator=translator,
        )
        return {
            "translations": translations,
            "source_language": source_language,
            "target_language": target_language,
        }

    if normalized_action == "pre-sale-list":
        if not seller_id and not str(data.get("item_id") or "").strip():
            raise ValueError("该授权没有 Seller ID，不能读取售前问题")
        return client.search_questions(
            seller_id=seller_id or None,
            item_id=data.get("item_id"),
            user_id=data.get("user_id"),
            status=data.get("status") or None,
            sort_fields=data.get("sort_fields") or "date_created",
            sort_types=data.get("sort_types") or "DESC",
            limit=data.get("limit", 50),
            offset=data.get("offset", 0),
        )
    if normalized_action == "pre-sale-summary":
        item_id = str(data.get("item_id") or "").strip() or None
        user_id = str(data.get("user_id") or "").strip() or None
        if not seller_id and not item_id:
            raise ValueError("该授权没有 Seller ID，不能读取售前问题")
        common_filters = {
            "seller_id": seller_id or None,
            "item_id": item_id,
            "user_id": user_id,
            "limit": 1,
            "offset": 0,
        }
        total_page = client.search_questions(**common_filters)
        counts = {}
        for status in PRE_SALE_SUMMARY_STATUSES:
            page = client.search_questions(status=status, **common_filters)
            counts[status] = int(page.get("total") or 0)
        return {"total": int(total_page.get("total") or 0), "counts": counts}
    if normalized_action == "pre-sale-detail":
        return client.get_question(data.get("question_id"))
    if normalized_action == "pre-sale-answer":
        reply_text = str(data.get("text") or "").strip()
        translated_text = data.get("text_translated")
        target_language = ""
        if data.get("auto_translate"):
            site_id = str(data.get("site_id") or "").strip().upper()
            if not site_id:
                question = client.get_question(data.get("question_id"))
                site_id = str(question.get("site_id") or "").strip().upper()
            target_language = _pre_sale_site_language(site_id)
            translated_text = translate_texts(
                [reply_text],
                "zh-CN",
                target_language,
                translator=translator,
            )[0]
        result = client.answer_question(
            data.get("question_id"),
            reply_text,
            text_translated=translated_text,
        )
        if isinstance(result, dict) and target_language:
            result = dict(result)
            result["translation"] = {
                "source_language": "zh-CN",
                "target_language": target_language,
                "text_translated": translated_text,
            }
        return result
    if normalized_action == "pre-sale-delete":
        return client.delete_question(data.get("question_id"))

    if normalized_action == "post-sale-unread":
        if not seller_id:
            raise ValueError("该授权没有 Seller ID，不能读取未读售后消息")
        result = dict(client.get_unread_post_sale_messages(seller_id))
        rows = [dict(row or {}) for row in result.get("results") or ()]
        identifiers = [_resource_identifier(row.get("resource")) for row in rows]
        result["results"] = _attach_order_contexts(
            int(token_id), rows, identifiers, get_order_contexts
        )
        return result
    if normalized_action == "post-sale-messages":
        if seller_id:
            return client.get_post_sale_messages_for_seller(
                data.get("pack_id"),
                seller_id,
                limit=data.get("limit", 50),
                offset=data.get("offset", 0),
                mark_as_read=False,
            )
        return client.get_post_sale_messages(data.get("pack_id"), limit=data.get("limit", 50), offset=data.get("offset", 0))
    if normalized_action == "post-sale-send":
        return client.send_post_sale_message(
            data.get("pack_id"),
            data.get("text", ""),
            text_translated=data.get("text_translated"),
            attachments=data.get("attachments"),
        )

    if normalized_action == "claims-list":
        if not seller_id:
            raise ValueError("该授权没有 Seller ID，不能读取投诉")
        claim_status = str(data.get("status") or "").strip().lower() or None
        has_search_filter = any(
            str(data.get(key) or "").strip()
            for key in (
                "stage", "claim_type", "claim_id", "order_id", "pack_id",
                "date_from", "date_to",
            )
        )
        # Mercado Libre currently rejects /claims/search when only user_id is
        # present (atLeastOneFilterProvided). Keep generic callers usable while
        # the workbench explicitly requests opened and closed in two passes.
        if not claim_status and not has_search_filter:
            claim_status = "opened"
        requested_limit = max(1, min(100, int(data.get("limit", 50) or 50)))
        requested_offset = max(0, int(data.get("offset", 0) or 0))
        seller_targets = [(str(token.get("site_id") or "").upper(), seller_id)]
        if str(token.get("site_id") or "").strip().upper() == "CBT":
            marketplace_data = client.request("GET", f"/marketplace/users/{seller_id}")
            marketplaces = (
                marketplace_data.get("marketplaces")
                if isinstance(marketplace_data, Mapping)
                else []
            )
            seller_targets = [
                (
                    str(marketplace.get("site_id") or "").strip().upper(),
                    str(marketplace.get("user_id") or "").strip(),
                )
                for marketplace in marketplaces or ()
                if str(marketplace.get("user_id") or "").strip()
            ]
            if not seller_targets:
                raise ValueError("该 CBT 授权没有可用于索赔查询的站点子账号")

        rows: list[dict[str, Any]] = []
        official_total = 0
        target_errors: list[dict[str, str]] = []
        first_error: Exception | None = None
        required_per_target = requested_offset + requested_limit
        for site_id, target_seller_id in seller_targets:
            target_rows: list[dict[str, Any]] = []
            target_offset = 0
            target_total = 0
            try:
                while len(target_rows) < required_per_target:
                    page_limit = min(100, required_per_target - len(target_rows))
                    page = client.search_claims(
                        target_seller_id,
                        status=claim_status,
                        stage=data.get("stage") or None,
                        claim_type=data.get("claim_type") or None,
                        claim_id=data.get("claim_id"),
                        order_id=data.get("order_id"),
                        pack_id=data.get("pack_id"),
                        date_from=data.get("date_from") or None,
                        date_to=data.get("date_to") or None,
                        limit=page_limit,
                        offset=target_offset,
                    )
                    batch = [dict(row or {}) for row in page.get("data") or ()]
                    target_total = int((page.get("paging") or {}).get("total") or 0)
                    for row in batch:
                        row.setdefault("site_id", site_id)
                        row["marketplace_user_id"] = target_seller_id
                    target_rows.extend(batch)
                    target_offset += len(batch)
                    if not batch or target_offset >= target_total:
                        break
                official_total += target_total
                rows.extend(target_rows)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                target_errors.append({"site_id": site_id, "message": str(exc)})

        if not rows and len(target_errors) == len(seller_targets) and first_error is not None:
            raise first_error
        rows.sort(
            key=lambda row: str(row.get("last_updated") or row.get("date_created") or ""),
            reverse=True,
        )
        deduplicated = list({str(row.get("id") or index): row for index, row in enumerate(rows)}.values())
        rows = deduplicated[requested_offset:requested_offset + requested_limit]
        result = {
            "paging": {
                "total": official_total,
                "offset": requested_offset,
                "limit": requested_limit,
            },
            "data": rows,
        }
        if target_errors:
            result["marketplace_errors"] = target_errors
        identifiers = [str(row.get("resource_id") or "") for row in rows]
        result["data"] = _attach_order_contexts(
            int(token_id), rows, identifiers, get_order_contexts
        )
        return result
    if normalized_action == "claims-detail":
        return client.get_claim_bundle(data.get("claim_id"))
    if normalized_action == "claims-send":
        return client.send_claim_message(
            data.get("claim_id"),
            data.get("message", ""),
            receiver_role=data.get("receiver_role", ""),
            attachments=data.get("attachments"),
        )

    raise ValueError("不支持的美客多消息操作")
