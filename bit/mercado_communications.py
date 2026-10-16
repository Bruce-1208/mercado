"""把工作台已保存的店铺 Token 接入美客多官方通信 API。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import base64
import binascii
import io
import hashlib
import json
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


def communication_date_bounds(payload: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """Interpret workbench calendar dates in Beijing time, with an exclusive end."""
    dates = []
    for key in ("date_from", "date_to"):
        text = str(payload.get(key) or "").strip()
        if not text:
            dates.append(None)
            continue
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc
        if parsed.strftime("%Y-%m-%d") != text:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式")
        dates.append(parsed.replace(tzinfo=timezone(timedelta(hours=8))))
    start, end = dates
    if start is not None and end is not None and start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return (
        start.timestamp() if start is not None else None,
        (end + timedelta(days=1)).timestamp() if end is not None else None,
    )


def communication_timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def communication_date_matches(value: Any, bounds: tuple[float | None, float | None]) -> bool:
    start, end = bounds
    if start is None and end is None:
        return True
    timestamp = communication_timestamp(value)
    return timestamp is not None and (start is None or timestamp >= start) and (
        end is None or timestamp < end
    )


def query_pre_sale_question_pages(
    search_page: Callable[[dict[str, Any]], Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the supported Questions pages and filter calendar dates locally.

    The official API documents date_created sorting, but no date-range search
    filter. Its total therefore describes the entire matching history.
    """
    bounds = communication_date_bounds(payload)
    has_date_filter = any(value is not None for value in bounds)
    filters = {
        key: payload.get(key) for key in ("item_id", "user_id", "status")
        if str(payload.get(key) or "").strip()
    }
    rows = []
    source_total = 0
    offset = 0
    complete = False
    while offset <= 1000:
        page = search_page({
            **filters,
            "sort_fields": "date_created",
            "sort_types": "DESC",
            "limit": 100,
            "offset": offset,
        }) or {}
        questions = [dict(row) for row in page.get("questions") or [] if isinstance(row, dict)]
        source_total = max(source_total, int(page.get("total") or 0))
        crossed_start = False
        for row in questions:
            timestamp = communication_timestamp(row.get("date_created"))
            if bounds[0] is not None and timestamp is not None and timestamp < bounds[0]:
                crossed_start = True
            if communication_date_matches(row.get("date_created"), bounds):
                rows.append(row)
        offset += len(questions)
        if crossed_start or offset >= source_total:
            complete = True
            break
        if not questions:
            break
    return {
        "questions": rows,
        "official_total": len(rows) if has_date_filter else source_total,
        "source_total": source_total,
        "truncated": not complete,
    }


def _pre_sale_site_language(site_id: Any) -> str:
    site = str(site_id or "").strip().upper()[:3]
    if site == "MLB":
        return "pt-BR"
    if site in PRE_SALE_SPANISH_SITES:
        return "es"
    raise ValueError(f"无法识别站点 {site or '(empty)'} 的买家语言")


def _claim_language(client, token, token_id, claim_id, get_order_contexts):
    claim = client.get_claim(claim_id)
    site_id = str(claim.get("site_id") or "").strip().upper()
    if not site_id or site_id == "CBT":
        contexts = get_order_contexts(int(token_id), [str(claim.get("resource_id") or "")]) if get_order_contexts else []
        site_id = next((str(row.get("site_id") or "").upper() for row in contexts or [] if row.get("site_id")), "")
    if not site_id or site_id == "CBT":
        site_id = str(token.get("site_id") or "").strip().upper()
    return site_id, _pre_sale_site_language(site_id)


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

    if normalized_action == "claims-translate":
        raw_texts = data.get("texts")
        if not isinstance(raw_texts, list):
            raise ValueError("待翻译内容必须是文本数组")
        if len(raw_texts) > 100 or any(not isinstance(text, str) or not text.strip() or len(text) > 50000 for text in raw_texts):
            raise ValueError("单次最多翻译 100 条非空文本，每条最多 50,000 字符")
        if data.get("background") is True:
            from bit.communication_preload import claim_translation_preload
            digest = hashlib.sha256(json.dumps(raw_texts, ensure_ascii=False).encode()).hexdigest()
            key = (int(token_id), str(data.get("claim_id") or ""), str(data.get("site_id") or ""), digest)
            state = claim_translation_preload.read(key, lambda: execute_store_communication(
                token_id, action, {**data, "background": False}, get_token=get_token,
                refresh_token=refresh_token, get_order_contexts=get_order_contexts,
                http=http, timeout=timeout, translator=translator))
            if state.get("error") and data.get("retry") is True:
                state = claim_translation_preload.read(key, lambda: execute_store_communication(
                    token_id, action, {**data, "background": False}, get_token=get_token,
                    refresh_token=refresh_token, get_order_contexts=get_order_contexts,
                    http=http, timeout=timeout, translator=translator), refresh=True)
            if state.get("pending"):
                return {"pending": True}
            if state.get("error"):
                raise RuntimeError(state["error"])
            return state["data"]
        from erp.mercadolibre_translation import detect_text_language
        client.timeout = min(client.timeout, 8)
        client.max_attempts = 2
        # Incoming display translation can reuse the site already read by the
        # UI. Sending still resolves the authoritative marketplace separately.
        site_id = str(data.get("site_id") or "").strip().upper()
        if site_id in PRE_SALE_SPANISH_SITES or site_id == "MLB":
            local_language = _pre_sale_site_language(site_id)
        else:
            _, local_language = _claim_language(client, token, token_id, data.get("claim_id"), get_order_contexts)
        if len(raw_texts) > 100:
            raise ValueError("单次最多翻译 100 条文本")
        translations = [""] * len(raw_texts)
        language_groups = {}
        for index, text in enumerate(raw_texts):
            # Resolve the marketplace once per batch and translate each language
            # together while preserving the original message order.
            source = detect_text_language(str(text or ""), local_language)
            language_groups.setdefault(source, []).append((index, text))
        for source, group in language_groups.items():
            translated = translate_texts([text for _, text in group], source, "zh-CN", translator=translator)
            for (index, _), text in zip(group, translated):
                translations[index] = text
        return {"translations": translations}

    if normalized_action == "claims-attachment":
        return client.get_chat_attachment(data.get("attachment_id"), claim_id=data.get("claim_id"))

    if normalized_action == "post-sale-attachment":
        site_id = str(data.get("site_id") or "").upper()
        if site_id in {"", "CBT"} and get_order_contexts and data.get("pack_id"):
            contexts = get_order_contexts(int(token_id), [str(data["pack_id"])])
            site_id = next((str(row.get("site_id") or "").upper() for row in contexts or [] if row.get("site_id")), "")
        return client.get_chat_attachment(data.get("attachment_id"), site_id=site_id)

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

    if normalized_action == "pre-sale-products":
        return client.get_question_products(data.get("item_ids"))
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
    if normalized_action == "seller-notices":
        return client.get_seller_notices(
            limit=data.get("limit", 20),
            offset=data.get("offset", 0),
        )
    if normalized_action == "pre-sale-summary":
        item_id = str(data.get("item_id") or "").strip() or None
        user_id = str(data.get("user_id") or "").strip() or None
        if not seller_id and not item_id:
            raise ValueError("该授权没有 Seller ID，不能读取售前问题")
        if any(str(data.get(key) or "").strip() for key in ("date_from", "date_to")):
            page = query_pre_sale_question_pages(
                lambda filters: client.search_questions(seller_id=seller_id or None, **filters),
                {**data, "status": ""},
            )
            counts = {status: 0 for status in PRE_SALE_SUMMARY_STATUSES}
            for question in page["questions"]:
                status = str(question.get("status") or "").strip().upper()
                if status:
                    counts[status] = counts.get(status, 0) + 1
            return {
                "total": page["official_total"],
                "counts": counts,
                "truncated": page["truncated"],
            }
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
        # CBT tokens belong to the global account, while the seller endpoint
        # requires the order's marketplace seller. Read the authorized pack
        # through Global Selling instead of passing the CBT parent as seller.
        if seller_id and str(token.get("site_id") or "").strip().upper() != "CBT":
            result = client.get_post_sale_messages_for_seller(
                data.get("pack_id"),
                seller_id,
                limit=data.get("limit", 50),
                offset=data.get("offset", 0),
                mark_as_read=False,
            )
        else:
            result = client.get_post_sale_messages(
                data.get("pack_id"), limit=data.get("limit", 50), offset=data.get("offset", 0),
                mark_as_read=False,
            )
        result = dict(result)
        result["seller_id"] = seller_id
        for message in result.get("messages") or ():
            for resource in message.get("message_resources") or ():
                if resource.get("name") == "sellers" and str(resource.get("id") or "").isdigit():
                    result["seller_id"] = str(resource["id"])
                    break
            if result["seller_id"] != seller_id:
                break
        if data.get("include_order_context") and int(data.get("offset", 0) or 0) == 0:
            _attach_order_contexts(
                int(token_id), [result], [str(data.get("pack_id") or "")], get_order_contexts
            )
        return result
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
                        calendar_timezone=timezone(timedelta(hours=8)),
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
        # Keep optional resources and their retry delays off the chat's path.
        client.timeout = min(client.timeout, 8)
        client.max_attempts = 2
        return client.get_claim_bundle(data.get("claim_id"), section=data.get("section") or "all")
    if normalized_action == "claims-send":
        message = str(data.get("message") or "").strip()
        images = data.get("images") or []
        if not isinstance(images, list) or len(images) > 5:
            raise ValueError("每次最多发送 5 张图片")
        validated_images = []
        from PIL import Image
        for image in images:
            if not isinstance(image, dict):
                raise ValueError("图片格式错误")
            encoded = image.get("content_base64")
            if not isinstance(encoded, str) or len(encoded) > 7 * 1024 * 1024:
                raise ValueError("每张图片不能超过 5 MB")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("图片编码无效") from None
            if not content or len(content) > 5 * 1024 * 1024:
                raise ValueError("图片不能为空且不能超过 5 MB")
            try:
                with Image.open(io.BytesIO(content)) as picture:
                    fmt = picture.format
                    picture.verify()
                mime, suffix = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png")}[fmt]
            except Exception:
                raise ValueError("仅支持有效的 JPG、PNG 图片") from None
            validated_images.append((f"claim-image-{len(validated_images) + 1}.{suffix}", content, mime))
        translation = None
        if data.get("auto_translate") is True and (message or not validated_images):
            if not message:
                raise ValueError("请输入中文索赔回复内容")
            site_id, language = _claim_language(client, token, token_id, data.get("claim_id"), get_order_contexts)
            english = translate_texts([message], "zh-CN", "en", translator=translator)[0]
            local = translate_texts([english], "en", language, translator=translator)[0]
            label = "Português" if language == "pt-BR" else "Español"
            message = f"English:\n{english}\n\n{label}:\n{local}"
            translation = {"source_text": data.get("message"), "english": english,
                           "local": local, "target_language": language, "site_id": site_id}
        if validated_images:
            role = str(data.get("receiver_role") or "").strip().lower()
            if role not in client.CLAIM_RECEIVER_ROLES:
                raise ValueError("接收方只支持 complainant、respondent 或 mediator")
        attachments = list(data.get("attachments") or [])
        for filename, content, mime in validated_images:
            uploaded = client.upload_claim_attachment(data.get("claim_id"), filename, content, content_type=mime)
            attachments.append(uploaded["file_name"])
        if validated_images and not message:
            message = "Please see the attached images."
        result = client.send_claim_message(
            data.get("claim_id"),
            message,
            receiver_role=data.get("receiver_role", ""),
            attachments=attachments,
        )
        return {"result": result, "translation": translation} if translation else result

    raise ValueError("不支持的美客多消息操作")
