"""Mercado notification intake, durable event processing and operator inbox."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from flask import Blueprint, jsonify, render_template, request

from bit import bit_db_api


ALLOWED_TOPICS = frozenset({
    "orders", "marketplace_orders", "marketplace orders",
    "marketplace_orders_on_site",
    "shipments", "marketplace_shipments", "marketplace shipments",
    "items", "marketplace_items", "marketplace items", "items_prices",
    "marketplace_fbm_stock", "marketplace_user_products",
    "marketplace_user_products_families", "marketplace_cbt_items_uptin",
    "marketplace fbm stock", "marketplace user products",
    "marketplace user products families", "marketplace cbt items uptin",
    "questions", "marketplace_questions", "marketplace questions",
    "messages", "marketplace_messages", "marketplace messages",
    "claims", "marketplace_claims", "marketplace claims",
    "public_candidates", "public_offers", "marketplace_item_competition",
    "marketplace item competition",
})
_WORKBENCH_TZ = timezone(timedelta(hours=8))
_worker_guard = threading.Lock()
_worker_started = False
_feed_worker_started = False
_worker_stop = threading.Event()


def _safe_resource(value: Any) -> str:
    resource = str(value or "").strip()
    if (
        not resource
        or len(resource) > 500
        or not resource.startswith("/")
        or resource.startswith("//")
        or ".." in resource.split("/")
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", resource)
    ):
        raise ValueError("通知 resource 必须是美客多 API 相对路径")
    return resource


def _event_key(payload: Mapping[str, Any]) -> str:
    delivery_id = str(payload.get("_id") or payload.get("id") or "").strip()
    identity = "\0".join((
        delivery_id,
        str(payload.get("application_id") or ""),
        str(payload.get("user_id") or ""),
        str(payload.get("topic") or ""),
        str(payload.get("resource") or ""),
        str(payload.get("sent") or payload.get("received") or ""),
        json.dumps(payload.get("actions") or [], ensure_ascii=False, sort_keys=True),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return result
    return result.astimezone(_WORKBENCH_TZ).replace(tzinfo=None)


def _api_response_for_event(event: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    """Fetch the authoritative resource using only the server-owned token."""
    token_id = int(event.get("token_id") or 0)
    if token_id <= 0:
        raise ValueError("通知未能匹配到已授权店铺")
    from bit import bit_mysql, bit_order_sync
    from mercado_api.client import MercadoAPIError

    token = dict(bit_mysql.get_mercado_store_token(token_id) or {})
    client, token = bit_order_sync._client_and_token(token)
    resource = _safe_resource(event.get("resource"))
    topic = str(event.get("topic") or "").strip().lower().replace(" ", "_")

    def get_lead_time(active_client, shipment_id):
        try:
            return active_client.request(
                "GET", f"/marketplace/shipments/{shipment_id}/lead_time",
                headers={"x-format-new": "true"},
            )
        except MercadoAPIError as exc:
            if "401" in str(exc) or "access token" in str(exc).lower():
                raise
            return {}
        except Exception:
            return {}

    def get_resource(active_client):
        if topic in {"shipments", "marketplace_shipments"}:
            shipment_id = resource.rstrip("/").rsplit("/", 1)[-1]
            detail = active_client.get_shipment(shipment_id)
            lead_time = get_lead_time(active_client, shipment_id)
            return detail, lead_time
        if topic in {"orders", "marketplace_orders", "marketplace_orders_on_site"}:
            order_id = resource.rstrip("/").rsplit("/", 1)[-1]
            order = active_client.get_order(order_id)
            shipping = order.get("shipping") if isinstance(order, dict) else {}
            shipment_id = str((shipping or {}).get("id") or "").strip()
            lead_time = {}
            if shipment_id:
                lead_time = get_lead_time(active_client, shipment_id)
            return order, lead_time
        return active_client.request("GET", resource), {}

    try:
        payload, extra = get_resource(client)
    except MercadoAPIError as exc:
        if "401" not in str(exc) and "access token" not in str(exc).lower():
            raise
        token = bit_order_sync._refresh_token(token_id)
        client = bit_order_sync.MercadoLibreClient(str(token.get("access_token") or ""))
        payload, extra = get_resource(client)

    if topic in {"orders", "marketplace_orders", "marketplace_orders_on_site"} and isinstance(payload, dict):
        bit_mysql.upsert_mercado_synced_orders(token, [payload])
    return token, (payload, extra)


def _process_one_event(event: Mapping[str, Any]) -> None:
    token, result = _api_response_for_event(event)
    payload, extra = result
    topic = str(event.get("topic") or "").strip().lower().replace(" ", "_")
    title = ""
    details = ""
    due_at = None
    priority = ""
    resolution_reason = ""
    if topic in {"shipments", "marketplace_shipments"}:
        shipment = payload if isinstance(payload, dict) else {}
        lead_time = extra if isinstance(extra, dict) else {}
        status = str(shipment.get("status") or "").lower()
        pay_before = _parse_datetime(lead_time.get("pay_before"))
        if status in {"ready_to_ship", "handling"}:
            title = "运单待发货，请按官方截止时间处理" if pay_before else "运单待发货，请检查美客多显示的截止时间"
            details = f"物流状态：{status}；" + (f"pay_before：{lead_time.get('pay_before')}" if pay_before else "接口暂未返回 pay_before")
            priority = "urgent"
            due_at = pay_before
        elif status:
            title = f"物流状态变更：{status}"
            details = str(shipment.get("substatus") or "请复核最新物流节点")
            if status in {"shipped", "delivered", "cancelled", "canceled", "closed"}:
                resolution_reason = f"系统读取到物流状态 {status}，该发货提醒已结束。"
    elif topic in {"orders", "marketplace_orders", "marketplace_orders_on_site"}:
        order = payload if isinstance(payload, dict) else {}
        status = str(order.get("status") or "").lower()
        if status == "ready_to_ship":
            lead_time = extra if isinstance(extra, dict) else {}
            pay_before = _parse_datetime(lead_time.get("pay_before"))
            title = "订单待发货，请按官方截止时间处理" if pay_before else "订单待发货，请检查官方发货截止时间"
            priority = "urgent"
            due_at = pay_before
            details = f"订单 {order.get('id') or ''} 当前状态为待发货；" + (f"运单截止时间 pay_before：{lead_time.get('pay_before')}" if pay_before else "请打开运单核对美客多显示的截止时间。")
        elif status in {"shipped", "delivered", "cancelled", "canceled", "closed"}:
            resolution_reason = f"系统读取到订单状态 {status}，该订单提醒已结束。"
    elif topic in {"questions", "marketplace_questions"}:
        question = payload if isinstance(payload, dict) else {}
        title = "售前问题待回复，请检查问题状态"
        details = f"问题编号 {question.get('id') or ''}；商品 {question.get('item_id') or ''}；内容：{str(question.get('text') or '')[:800]}"
        priority = "urgent"
        if str(question.get("status") or "").lower() in {"answered", "closed", "deleted"}:
            title = "售前问题已结束"
            resolution_reason = f"系统读取到问题状态 {question.get('status')}，当前无需回复。"
    elif topic in {"messages", "marketplace_messages"}:
        message = payload if isinstance(payload, dict) else {}
        title = "收到新的售后消息，请检查并回复"
        details = f"消息资源 {event.get('resource') or ''}；订单 {message.get('order_id') or ''}；请在售后处理页核对是否仍未回复。"
        priority = "urgent"
    elif topic in {"claims", "marketplace_claims"}:
        claim = payload if isinstance(payload, dict) else {}
        deadline = next((claim.get(key) for key in (
            "due_date", "due_at", "respond_by", "deadline", "resolution_deadline",
        ) if claim.get(key)), None)
        due_at = _parse_datetime(deadline)
        title = "索赔需要处理，请核对官方截止时间"
        details = f"索赔编号 {claim.get('id') or ''}；状态 {claim.get('status') or '待核实'}；阶段 {claim.get('stage') or '—'}；" + (f"接口截止时间：{deadline}" if deadline else "请进入美客多核对可执行动作和截止时间。")
        priority = "urgent"
        if str(claim.get("status") or "").lower() in {"closed", "resolved", "cancelled", "canceled"}:
            title = "索赔已结束"
            resolution_reason = f"系统读取到索赔状态 {claim.get('status')}，该处理提醒已结束。"
    elif topic in {"items", "marketplace_items", "items_prices", "marketplace_fbm_stock", "marketplace_user_products", "marketplace_user_products_families"}:
        item = payload if isinstance(payload, dict) else {}
        status = str(item.get("status") or "").lower()
        quantity = item.get("available_quantity")
        if status in {"paused", "closed", "under_review"}:
            title = f"商品当前状态为 {status}，请检查是否需要恢复或申诉"
            details = f"商品 {item.get('id') or ''}；状态：{status}；平台库存：{quantity if quantity is not None else '未提供'}"
            priority = "high"
        elif quantity is not None and status == "active":
            try:
                if int(quantity or 0) <= 0:
                    title = "在售商品平台库存为 0，请核对库存"
                    details = f"商品 {item.get('id') or ''}；状态：{status}；可售库存：{quantity}"
                    priority = "high"
            except (TypeError, ValueError):
                pass
            else:
                if int(quantity or 0) > 0:
                    resolution_reason = f"系统读取到商品 {item.get('id') or ''} 正常在售且可售库存为 {quantity}。"
    elif topic in {"public_candidates", "public_offers"}:
        offer = payload if isinstance(payload, dict) else {}
        title = "促销资格或报名状态变化，请确认活动执行结果"
        details = f"商品 {offer.get('item_id') or ''}；活动 {offer.get('promotion_id') or ''}；状态 {offer.get('status') or '待核实'}。"
        priority = "high"
    elif topic in {"marketplace_item_competition", "item_competition"}:
        competition = payload if isinstance(payload, dict) else {}
        title = "商品竞争状态变化，请复核跟卖价格和利润"
        details = f"商品 {competition.get('item_id') or competition.get('id') or ''}；状态 {competition.get('status') or '已变化'}；请在竞争分析入口核对价格。"
        priority = "high"
    elif topic == "marketplace_cbt_items_uptin":
        title = "商品迁移事件待确认"
        details = f"资源 {event.get('resource') or ''}；请检查迁移后的商品 ID、状态和关联信息。"
        priority = "high"
    bit_db_api.enrich_mercado_notification_task(
        int(event.get("id") or 0),
        title=title,
        details=details,
        priority=priority,
        due_at=due_at.isoformat(sep=" ") if due_at else None,
    )
    if resolution_reason:
        bit_db_api.resolve_mercado_notification_task(int(event.get("id") or 0), resolution_reason)


def _notification_worker_loop():
    while not _worker_stop.is_set():
        try:
            event = bit_db_api.claim_mercado_notification_event()
        except Exception:
            logging.exception("读取美客多通知队列失败")
            _worker_stop.wait(5)
            continue
        if not event:
            _worker_stop.wait(2)
            continue
        try:
            _process_one_event(event)
            bit_db_api.finish_mercado_notification_event(int(event["id"]))
        except Exception as exc:
            logging.exception("美客多通知处理失败：event_id=%s", event.get("id"))
            try:
                bit_db_api.finish_mercado_notification_event(int(event["id"]), error=str(exc))
            except Exception:
                logging.exception("更新美客多通知处理状态失败：event_id=%s", event.get("id"))


def _sync_missed_feeds_once():
    """Recover callbacks Mercado recorded as missed, using the official feed API."""
    from bit import bit_mysql, bit_order_sync
    from mercado_api.client import MercadoAPIError

    rows = bit_mysql.list_mercado_store_tokens().get("rows", [])
    representatives = {}
    for row in rows:
        app_id = str(row.get("client_id") or "").strip()
        if not row.get("enabled") or not app_id.isdigit():
            continue
        representatives.setdefault(int(app_id), int(row["id"]))
    for application_id, token_id in representatives.items():
        try:
            token = bit_mysql.get_mercado_store_token(token_id)
            client, _ = bit_order_sync._client_and_token(token or {})
            try:
                result = client.request(
                    "GET", "/missed_feeds", params={"app_id": application_id},
                    max_attempts=2,
                )
            except MercadoAPIError as exc:
                if "401" not in str(exc) and "access token" not in str(exc).lower():
                    raise
                refreshed = bit_order_sync._refresh_token(token_id)
                client = bit_order_sync.MercadoLibreClient(str(refreshed.get("access_token") or ""))
                result = client.request(
                    "GET", "/missed_feeds", params={"app_id": application_id},
                    max_attempts=2,
                )
            messages = (result or {}).get("messages") or []
            if isinstance(messages, dict):
                messages = [item for group in messages.values() for item in (group if isinstance(group, list) else [group])]
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                notification = dict(message)
                notification.setdefault("application_id", application_id)
                if str(notification.get("topic") or "").strip().lower() not in ALLOWED_TOPICS:
                    continue
                try:
                    notification["resource"] = _safe_resource(notification.get("resource"))
                    notification["event_key"] = _event_key(notification)
                    bit_mysql.receive_mercado_notification(notification)
                except (TypeError, ValueError) as exc:
                    logging.warning("忽略归属或字段无效的 missed_feed：app=%s，原因=%s", application_id, exc)
        except Exception:
            logging.exception("拉取美客多 missed_feeds 失败：app=%s", application_id)


def _missed_feed_worker_loop():
    # First pass recovers history after restarts; later passes provide a
    # 15-minute safety net alongside the existing order poller.
    while not _worker_stop.is_set():
        try:
            _sync_missed_feeds_once()
        except Exception:
            logging.exception("美客多 missed_feeds 恢复扫描失败")
        _worker_stop.wait(15 * 60)


def ensure_notification_worker(*, enabled: bool):
    global _worker_started, _feed_worker_started
    if not enabled:
        return False
    with _worker_guard:
        if _worker_started:
            return True
        threading.Thread(
            target=_notification_worker_loop,
            name="mercado-notification-worker",
            daemon=True,
        ).start()
        _worker_started = True
        if not _feed_worker_started:
            threading.Thread(
                target=_missed_feed_worker_loop,
                name="mercado-missed-feed-worker",
                daemon=True,
            ).start()
            _feed_worker_started = True
    return True


def _task_key(*parts: Any) -> str:
    return hashlib.sha256("\0".join(str(value or "") for value in parts).encode("utf-8")).hexdigest()


def _upsert_watch_task(storage, *, organization_key, token_id, topic, resource, title, details, due_at, priority="high", entry_url="/?tab=orders", source="order_watch"):
    storage.upsert_mercado_operator_task({
        "task_key": _task_key(source, organization_key, token_id, topic, resource),
        "organization_key": organization_key,
        "token_id": token_id,
        "source": source,
        "topic": topic,
        "resource": resource,
        "title": title,
        "details": details,
        "entry_url": entry_url,
        "priority": priority,
        "due_at": due_at,
    })


def _refresh_order_watch_tasks(storage, token_ids: Iterable[int], organization_by_token: Mapping[int, str], *, now=None):
    """Convert stale local fulfillment/procurement signals into persistent tasks."""
    now = now or datetime.now()
    rows = storage.list_mercado_action_center_orders(token_ids=token_ids, limit=600)
    for row in rows or ():
        token_id = int(row.get("token_id") or 0)
        order_id = str(row.get("order_id") or "")
        if not token_id or not order_id:
            continue
        organization_key = organization_by_token.get(token_id) or "wuhan-zeshun"
        updated = _parse_datetime(row.get("last_updated") or row.get("date_created"))
        if updated is None:
            continue
        age = now - updated
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        shipment_status = str(
            raw.get("_shipment_status")
            or (raw.get("shipping") or {}).get("status")
            or ""
        ).lower()
        if str(row.get("status") or "") == "ready_to_ship" and age >= timedelta(hours=12):
            _upsert_watch_task(
                storage,
                organization_key=organization_key,
                token_id=token_id,
                topic="shipment_deadline_check",
                resource=order_id,
                title="待发货订单超过 12 小时未更新，请核对官方截止时间",
                details=f"店铺 {row.get('shop_name') or ''}；最近更新时间 {row.get('last_updated') or row.get('date_created') or ''}。打开订单查看美客多运单的 pay_before。",
                due_at=updated + timedelta(hours=12),
                priority="urgent",
                entry_url="/?tab=orders",
            )
        tracking = row.get("tracking") if isinstance(row.get("tracking"), dict) else {}
        tracking_status = str(tracking.get("status") or "").lower()
        checked = _parse_datetime(row.get("tracking_checked_at"))
        if (
            row.get("purchase_tracking")
            and str(row.get("workflow_status") or "") not in {"完成", "已签收", "已入库", "已取消"}
            and checked
            and now - checked >= timedelta(hours=48)
            and tracking_status in {"pending", "not_found", "exception", "failed"}
        ):
            _upsert_watch_task(
                storage,
                organization_key=organization_key,
                token_id=token_id,
                topic="procurement_tracking_stalled",
                resource=order_id,
                title="采购物流超过 48 小时仍未查到有效轨迹",
                details=f"采购单 {row.get('purchase_order') or '未登记'}；物流号 {row.get('purchase_tracking') or ''}；最近查询 {row.get('tracking_checked_at') or ''}。",
                due_at=checked + timedelta(hours=48),
                priority="high",
            )


def _refresh_promotion_tasks(storage, token_ids: Iterable[int], organization_by_token: Mapping[int, str], *, now=None):
    """Persist near-deadline promotions and completed jobs with unknown outcomes."""
    from erp.mercadolibre_promotion_store import PromotionStore

    now = now or datetime.now()
    promotion_store = PromotionStore()
    allowed = {int(value) for value in token_ids if int(value or 0) > 0}
    for promotion in promotion_store.list_promotions(token_ids=allowed):
        if int(promotion.get("token_id") or 0) not in allowed:
            continue
        if str(promotion.get("status_raw") or "").lower() == "finished":
            continue
        deadline = _parse_datetime(promotion.get("deadline_date"))
        if deadline is None:
            continue
        deadline = deadline.replace(hour=23, minute=59, second=0, microsecond=0)
        if now - timedelta(days=7) <= deadline <= now + timedelta(hours=48):
            _upsert_watch_task(
                storage,
                organization_key=organization_by_token.get(int(promotion.get("token_id") or 0)) or "wuhan-zeshun",
                token_id=int(promotion.get("token_id") or 0),
                topic="promotion_deadline",
                resource=str(promotion.get("promotion_id") or promotion.get("id") or ""),
                title="促销活动即将截止，请确认商品和活动状态",
                details=f"{promotion.get('name') or '促销活动'}；状态 {promotion.get('status_raw') or '未知'}；截止日期 {promotion.get('deadline_date') or ''}。",
                due_at=deadline,
                priority="high",
                entry_url="/?tab=promotions",
                source="promotion_watch",
            )
    for job in promotion_store.list_jobs(limit=200):
        if int(job.get("unknown_count") or 0) <= 0:
            continue
        preview = promotion_store.get_preview(str(job.get("preview_id") or "")) or {}
        promotion = promotion_store.get_promotion(int(preview.get("promotion_fk") or 0)) or {}
        token_id = int(promotion.get("token_id") or 0)
        if not token_id or token_id not in allowed:
            continue
        _upsert_watch_task(
            storage,
            organization_key=organization_by_token.get(token_id) or "wuhan-zeshun",
            token_id=token_id,
            topic="promotion_result_unknown",
            resource=str(job.get("job_id") or ""),
            title="促销操作结果待核实",
            details=f"任务 {job.get('job_id') or ''} 有 {int(job.get('unknown_count') or 0)} 项结果未知；请查询美客多活动和商品状态后记录处理结果。",
            due_at=now + timedelta(hours=2),
            priority="urgent",
            entry_url="/?tab=promotions",
            source="promotion_job",
        )


def create_action_center_blueprint(*, login_required, current_user, authorized_token_ids, storage=None, has_permission=None):
    store = storage or bit_db_api
    blueprint = Blueprint("mercado_action_center", __name__)

    def scope():
        user = current_user() or {}
        allowed = authorized_token_ids(user)
        allowed = None if allowed is None else {int(value) for value in allowed}
        organization = ""
        if not (user.get("is_platform_admin") and not getattr(request, "args", {}).get("organization_key")):
            organization = str(request.args.get("organization_key") or user.get("organization_key") or "")
        if user.get("own_store_only"):
            organization = str(user.get("organization_key") or organization)
        return user, allowed, organization

    def permission(permission_key):
        user = current_user() or {}
        return not has_permission or has_permission(user, permission_key)

    @blueprint.route("/mercado/today")
    @login_required
    def page():
        if not permission("tasks.view"):
            return "当前账号没有运营待办查看权限", 403
        return render_template(
            "mercado_action_center.html",
            current_user=current_user() or {},
            can_execute=permission("tasks.execute"),
        )

    @blueprint.route("/api/mercado/today/tasks", methods=["GET"])
    @login_required
    def list_tasks():
        if not permission("tasks.view"):
            return jsonify(status="error", message="无待办查看权限"), 403
        try:
            user, allowed, organization = scope()
            token_rows = store.list_mercado_store_tokens().get("rows", []) if hasattr(store, "list_mercado_store_tokens") else []
            token_rows = [
                row for row in token_rows
                if (allowed is None or int(row.get("id") or 0) in allowed)
                and (not organization or str(row.get("organization_key") or "") == organization)
            ]
            token_ids = sorted(int(row["id"]) for row in token_rows if int(row.get("id") or 0) > 0)
            if token_ids:
                organization_by_token = {
                    int(row["id"]): str(row.get("organization_key") or "wuhan-zeshun")
                    for row in token_rows if int(row.get("id") or 0) > 0
                }
                _refresh_order_watch_tasks(store, token_ids, organization_by_token)
                _refresh_promotion_tasks(store, token_ids, organization_by_token)
            data = store.list_mercado_operator_tasks(
                token_ids=token_ids,
                organization_key=organization,
                include_closed=str(request.args.get("include_closed") or "0") == "1",
                limit=500,
            )
            token_issues = []
            for token in token_rows:
                if not token.get("enabled") or token.get("status") in {"expired", "expiring", "warning", "unknown"}:
                    token_issues.append({
                        "id": token.get("id"),
                        "store_name": token.get("display_name") or token.get("nickname"),
                        "status": token.get("status_text") or "需检查",
                        "reason": token.get("last_error") or "店铺授权已停用或需要重新授权",
                        "entry_url": "/?tab=store-tokens",
                    })
            events = store.list_mercado_notification_events(
                token_ids=token_ids,
                organization_key=organization,
                limit=100,
            )
            data["authorization_issues"] = token_issues
            data["events"] = events.get("rows") or []
            data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return jsonify(status="success", data=data)
        except Exception as exc:
            logging.exception("加载美客多运营待办失败")
            return jsonify(status="error", message=str(exc)), 500

    @blueprint.route("/api/mercado/today/tasks/<int:task_id>", methods=["PATCH"])
    @login_required
    def update_task(task_id):
        if not permission("tasks.execute"):
            return jsonify(status="error", message="无待办处理权限"), 403
        try:
            user, allowed, organization = scope()
            existing = store.get_mercado_operator_task(task_id)
            if not existing:
                return jsonify(status="error", message="待办不存在"), 404
            token_id = int(existing.get("token_id") or 0)
            if allowed is not None and token_id not in allowed:
                return jsonify(status="error", message="当前账号不能处理该店铺待办"), 403
            if organization and str(existing.get("organization_key") or "") != organization:
                return jsonify(status="error", message="当前账号不能处理该企业待办"), 403
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                return jsonify(status="error", message="待办更新内容格式无效"), 400
            result = store.update_mercado_operator_task(
                task_id,
                actor=str(user.get("display_name") or user.get("username") or "未知用户"),
                owner=body.get("owner") if "owner" in body else None,
                status=body.get("status") if "status" in body else None,
                due_at=body.get("due_at") if "due_at" in body else None,
                note=body.get("note") or "",
            )
            return jsonify(status="success", data=result)
        except (KeyError, ValueError) as exc:
            return jsonify(status="error", message=str(exc)), 400
        except Exception as exc:
            logging.exception("更新美客多运营待办失败：%s", task_id)
            return jsonify(status="error", message=str(exc)), 500

    @blueprint.route("/api/mercado/today/events/<int:event_id>/replay", methods=["POST"])
    @login_required
    def replay_event(event_id):
        if not permission("tasks.execute"):
            return jsonify(status="error", message="无通知重放权限"), 403
        try:
            user, allowed, organization = scope()
            event = store.get_mercado_notification_event(event_id)
            if not event:
                return jsonify(status="error", message="通知不存在"), 404
            token_id = int(event.get("token_id") or 0)
            if allowed is not None and token_id not in allowed:
                return jsonify(status="error", message="当前账号不能重放该店铺通知"), 403
            if organization and str(event.get("organization_key") or "") != organization:
                return jsonify(status="error", message="当前账号不能重放该企业通知"), 403
            result = store.replay_mercado_notification_event(event_id)
            return jsonify(status="success", data=result)
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 409
        except Exception as exc:
            logging.exception("重放美客多通知失败：%s", event_id)
            return jsonify(status="error", message=str(exc)), 500

    @blueprint.route("/api/mercado/notifications", methods=["POST"])
    def receive_notification():
        if bit_db_api.DB_MODE != "mysql":
            return "Notification endpoint must run on the server", 503
        raw_body = request.get_data(cache=True)
        if len(raw_body) > 64 * 1024:
            return "Payload too large", 413
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return "Invalid notification", 400
        topic = str(payload.get("topic") or "").strip().lower()
        if topic not in ALLOWED_TOPICS:
            return "Unsupported topic", 400
        try:
            resource = _safe_resource(payload.get("resource"))
            application_id = int(payload.get("application_id") or 0)
            user_id = str(payload.get("user_id") or "").strip()
            if application_id <= 0 or not user_id:
                return "Missing application_id or user_id", 400
            notification = {
                "event_key": _event_key(payload),
                "application_id": application_id,
                "user_id": user_id,
                "topic": topic,
                "resource": resource,
                "payload": payload,
            }
        except (TypeError, ValueError) as exc:
            logging.warning("拒绝无效的美客多通知：%s", exc)
            return "Invalid notification", 400
        try:
            accepted = store.receive_mercado_notification(notification)
            if not accepted.get("accepted"):
                logging.error("无法持久化归属不明的美客多通知：%s", accepted.get("reason"))
                return "Store mapping unavailable", 503
            # Mercado retries non-200 deliveries. Once the event has been
            # durably stored, always acknowledge immediately; processing runs
            # in the server-side worker.
            return "OK", 200
        except ValueError as exc:
            logging.error("美客多通知店铺匹配失败，将要求平台重试：%s", exc)
            return "Store mapping unavailable", 503
        except Exception:
            logging.exception("持久化美客多通知失败")
            return "Temporary persistence failure", 503

    return blueprint
