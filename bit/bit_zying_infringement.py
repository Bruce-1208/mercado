"""Review pending ZYing products with DeepSeek and update their audit status.

Only the list payload is used: product id, title, thumbnail and audit status.
Titles are sent to DeepSeek in fixed groups of 20.  A product is queried again
with the pending-status filter immediately before ``sale.update`` so a product
changed by another operator is never overwritten.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable

from bit.bit_check_risk import classify_risk_records
from bit.bit_zying_caiji import (
    ZYING_API_PAGE_SIZE,
    ZYING_MELI_PLATFORM_ID,
    ZyingAuthenticationError,
    _clean_text,
    _format_number,
    _reuse_zying_frontend_signer,
    _zying_api_post,
    load_zying_auth_token,
)


BATCH_SIZE = 20
PENDING_STATUS = 3000
APPROVED_STATUS = 1000
SUSPECTED_STATUS = 5000


class ZyingInfringementStopped(RuntimeError):
    """The user safely stopped a ZYing infringement review."""


def _check_stopped(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise ZyingInfringementStopped("智赢产品查侵权已由用户结束")


def _chunks(rows, size=BATCH_SIZE):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _plain_title(value):
    return _clean_text(html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))))


def _status_code(row):
    value = row.get("sale_stat", row.get("stat", row.get("status")))
    try:
        return (int(float(value)) // 1000) * 1000
    except (TypeError, ValueError):
        return 0


def _product_id(row):
    return _format_number(row.get("id") or row.get("sale_id") or row.get("product_id"))


def _normalize_candidate(row, page_number):
    product_id = _product_id(row)
    title = _plain_title(row.get("title"))
    if not product_id or not title:
        return None
    return {
        "row_id": product_id,
        "record_key": product_id,
        "product_id": product_id,
        "title": title,
        "main_image_url": _clean_text(row.get("thumb")),
        "page_number": page_number,
    }


def _listing_rows(data, context):
    listing = data.get("list") if isinstance(data, dict) else None
    rows = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"智赢{context}返回格式异常")
    return rows


def _list_payload(page, category=None, product_developer_id="", ids=None, status=PENDING_STATUS):
    payload = {
        "page": max(1, int(page)),
        "pagesize": ZYING_API_PAGE_SIZE,
        "word": "",
        "from": ZYING_MELI_PLATFORM_ID,
        "stat": int(status),
    }
    category_id = _clean_text(category)
    if category_id:
        payload["localid"] = category_id
    developer_id = _format_number(product_developer_id)
    if developer_id:
        payload["loginid"] = int(developer_id) if developer_id.isdigit() else developer_id
    if ids:
        payload["ids"] = [int(value) if str(value).isdigit() else str(value) for value in ids]
        payload["pagesize"] = max(1, min(len(payload["ids"]), ZYING_API_PAGE_SIZE))
    return payload


def _call(api_call, command, payload):
    return api_call(command, payload)


def _default_api_call(token):
    def call(command, payload):
        return _zying_api_post(None, token, command, payload)

    return call


def _still_pending(api_call, product_id):
    data = _call(
        api_call,
        "sale.stat",
        _list_payload(1, ids=[product_id], status=PENDING_STATUS),
    )
    return any(_product_id(row) == product_id for row in _listing_rows(data, "待审核复核接口"))


def _verify_status(api_call, product_id, target_status):
    data = _call(
        api_call,
        "sale.stat",
        _list_payload(1, ids=[product_id], status=target_status),
    )
    return any(_product_id(row) == product_id for row in _listing_rows(data, "状态回读接口"))


def _write_status(api_call, product_id, target_status):
    sale_id = int(product_id) if product_id.isdigit() else product_id
    _call(
        api_call,
        "sale.update",
        {"sale_id": sale_id, "sale_stat": int(target_status)},
    )
    if not _verify_status(api_call, product_id, target_status):
        raise RuntimeError(f"产品 {product_id} 保存后未回读到目标审核状态")


@_reuse_zying_frontend_signer
def review_pending_products(
    *,
    auth_token=None,
    start_page=1,
    end_page=1,
    category=None,
    category_name="",
    product_developer_id="",
    product_developer_name="",
    start_product_id="",
    max_items=None,
    stop_event=None,
    api_call: Callable | None = None,
    classifier: Callable | None = None,
    deepseek_api_key: str = "",
    log_callback: Callable | None = None,
    progress_callback: Callable | None = None,
):
    """Review only pending rows and write ``通过`` or ``疑似`` to ZYing."""

    start_page = max(1, int(start_page))
    end_page = max(1, int(end_page))
    if start_page > end_page:
        raise ValueError(f"起始页 {start_page} 不能大于结束页 {end_page}")
    requested_start_product_id = _format_number(start_product_id)
    if _clean_text(start_product_id) and not re.fullmatch(r"[1-9]\d*", requested_start_product_id):
        raise ValueError("起始产品编号必须是正整数，或留空从分类首件开始")
    item_limit = None if max_items is None else int(max_items)
    if item_limit is not None and not 1 <= item_limit <= 10000:
        raise ValueError("最多产品数必须是 1–10000 的整数")
    cursor_mode = item_limit is not None or bool(requested_start_product_id)
    cursor_found = not bool(requested_start_product_id)
    token = _clean_text(auth_token) or load_zying_auth_token()
    if not token:
        raise ZyingAuthenticationError("智赢登录凭证为空，请重新登录")
    api_call = api_call or _default_api_call(token)
    classifier = classifier or classify_risk_records

    def log(message):
        text = str(message or "").strip()
        if text:
            print(text, flush=True)
            if log_callback:
                log_callback(text)

    def progress():
        if progress_callback:
            progress_callback(dict(summary))

    started_at = time.time()
    summary = {
        "pages": 0,
        "pending_count": 0,
        "checked_count": 0,
        "approved_count": 0,
        "suspected_count": 0,
        "skipped_changed_count": 0,
        "failed_count": 0,
        "start_product_id": requested_start_product_id,
        "max_items": item_limit,
    }
    scope = category_name or category or "全部"
    developer = product_developer_name or product_developer_id or "全部"
    log(
        f"智赢产品查侵权启动：第 {start_page}-{end_page} 页，分类 {scope}，"
        f"产品开发 {developer}；仅查询待审核，每 20 个标题提交一次 DeepSeek"
        + (
            f"；从产品 {requested_start_product_id or '分类首件'} 开始，最多 {item_limit} 个"
            if cursor_mode else ""
        )
    )

    candidates = []
    seen_ids = set()
    # Snapshot the requested pages before changing any status.  Updating page 1
    # removes those rows from the pending result set and would otherwise shift
    # later pages forward, causing products to be skipped.
    for page_number in range(start_page, end_page + 1):
        _check_stopped(stop_event)
        data = _call(
            api_call,
            "sale.stat",
            _list_payload(page_number, category, product_developer_id),
        )
        rows = _listing_rows(data, f"列表第 {page_number} 页")
        page_candidates = []
        for row in rows:
            # ``sale.stat`` is already filtered by stat=3000.  If the response
            # also exposes a status, reject anything inconsistent defensively.
            status = _status_code(row)
            if status and status != PENDING_STATUS:
                continue
            candidate = _normalize_candidate(row, page_number)
            if candidate and candidate["product_id"] not in seen_ids:
                if not cursor_found:
                    if candidate["product_id"] != requested_start_product_id:
                        continue
                    cursor_found = True
                    log(f"已在待审核列表第 {page_number} 页定位起始产品 {requested_start_product_id}")
                if item_limit is not None and len(candidates) + len(page_candidates) >= item_limit:
                    break
                seen_ids.add(candidate["product_id"])
                page_candidates.append(candidate)
        summary["pages"] += 1
        summary["pending_count"] += len(page_candidates)
        candidates.extend(page_candidates)
        progress()
        log(f"第 {page_number}/{end_page} 页读取到待审核商品 {len(page_candidates)} 条（仅保留标题和主图）")
        if not rows:
            log("智赢已没有更多待审核产品，提前结束")
            break
        if item_limit is not None and len(candidates) >= item_limit:
            log(f"已达到本次最多产品数 {item_limit}，停止继续读取")
            break

    if requested_start_product_id and not cursor_found:
        raise ValueError(
            f"所选分类的待审核列表中未找到起始产品编号 {requested_start_product_id}，请确认编号、分类和审核状态"
        )

    batches = list(_chunks(candidates))
    for batch_number, batch in enumerate(batches, start=1):
        _check_stopped(stop_event)
        log(f"第 {batch_number}/{len(batches)} 批提交 DeepSeek：{len(batch)} 个标题")
        results = (
            classifier(batch, api_key=deepseek_api_key)
            if deepseek_api_key
            else classifier(batch)
        )
        by_id = {str(item.get("record_key") or item.get("row_id")): item for item in results}
        if set(by_id) != {row["product_id"] for row in batch}:
            raise ValueError("DeepSeek 未完整返回本批次全部产品，已停止本批写回")

        for candidate in batch:
            _check_stopped(stop_event)
            product_id = candidate["product_id"]
            result = by_id[product_id]
            target_status = APPROVED_STATUS if int(result.get("risk_level") or 0) == 0 else SUSPECTED_STATUS
            try:
                if not _still_pending(api_call, product_id):
                    summary["skipped_changed_count"] += 1
                    log(f"产品 {product_id} 状态已不再是待审核，跳过写回")
                    continue
                _write_status(api_call, product_id, target_status)
                summary["checked_count"] += 1
                if target_status == APPROVED_STATUS:
                    summary["approved_count"] += 1
                    log(f"产品 {product_id} 未发现侵权问题，已改为通过")
                else:
                    summary["suspected_count"] += 1
                    keywords = "、".join(result.get("keywords") or [])
                    suffix = f"（{keywords}）" if keywords else ""
                    log(f"产品 {product_id} 有侵权风险，已改为疑似{suffix}")
            except ZyingInfringementStopped:
                raise
            except Exception as exc:
                summary["failed_count"] += 1
                log(f"产品 {product_id} 状态写回失败：{exc}")
            finally:
                progress()

    summary["elapsed_seconds"] = round(time.time() - started_at, 2)
    progress()
    log(
        "智赢产品查侵权完成："
        f"审核 {summary['checked_count']} 条，通过 {summary['approved_count']} 条，"
        f"疑似 {summary['suspected_count']} 条，状态变化跳过 {summary['skipped_changed_count']} 条，"
        f"失败 {summary['failed_count']} 条"
    )
    return summary
