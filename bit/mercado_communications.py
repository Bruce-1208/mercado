"""把工作台已保存的店铺 Token 接入美客多官方通信 API。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import requests

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
        return client.answer_question(
            data.get("question_id"),
            data.get("text", ""),
            text_translated=data.get("text_translated"),
        )
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
        result = client.search_claims(
            seller_id,
            status=data.get("status") or None,
            stage=data.get("stage") or None,
            claim_type=data.get("claim_type") or None,
            claim_id=data.get("claim_id"),
            order_id=data.get("order_id"),
            pack_id=data.get("pack_id"),
            date_from=data.get("date_from") or None,
            date_to=data.get("date_to") or None,
            limit=data.get("limit", 50),
            offset=data.get("offset", 0),
        )
        rows = [dict(row or {}) for row in result.get("data") or ()]
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
