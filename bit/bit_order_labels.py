"""Download printable shipping-label PDFs from Mercado Libre."""

from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO

from bit import bit_mysql, mercado_tokens
from mercado_api.client import MercadoAPIError, MercadoLibreClient


class MercadoLabelError(RuntimeError):
    """The selected order cannot currently provide an official Mercado label."""


class MercadoLabelUnavailable(MercadoLabelError):
    """The shipment lifecycle says that an official label is unavailable."""

    def __init__(self, message, *, shipment_status="", permanent=False):
        super().__init__(message)
        self.shipment_status = str(shipment_status or "").strip().lower()
        self.permanent = bool(permanent)


_FINAL_NONPRINTABLE_SHIPMENT_STATUSES = {
    "cancelled",
    "canceled",
    "delivered",
    "shipped",
    "returned",
    "returned_to_sender",
    "not_delivered",
}


def _shipment_status_from_label_error(exc):
    message = str(exc or "")
    match = re.search(r"Shipment status is\s+['\"]([^'\"]+)['\"]", message, re.I)
    return str(match.group(1) if match else "").strip().lower()


def _refresh_store_token(token_id):
    mercado_tokens.refresh_and_save(
        int(token_id),
        get_token=bit_mysql.get_mercado_store_token,
        update_token=bit_mysql.update_mercado_store_token,
        record_error=bit_mysql.record_mercado_store_token_error,
    )
    return bit_mysql.get_mercado_store_token(int(token_id))


def _is_invalid_token_error(exc):
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "invalid_token",
            "token_not_valid",
            "malformed access_token",
            "access token expired",
        )
    )


def _download_one(context, *, max_attempts=4, timeout=30):
    order_id = str(context.get("order_id") or "")
    shipment_id = str(context.get("shipping_id") or "").strip()
    if not shipment_id:
        raise MercadoLabelError(f"订单 {order_id} 暂无美客多 Shipment ID，不能打印官方面单")
    access_token = str(context.get("access_token") or "").strip()
    if not access_token:
        raise MercadoLabelError(f"订单 {order_id} 所属店铺缺少 Access Token")
    # A print round already controls its retry count.  Keeping the HTTP client's
    # four retries here would multiply three UI attempts into twelve requests.
    client = MercadoLibreClient(access_token)
    if hasattr(client, "timeout"):
        client.timeout = int(timeout or 30)
    try:
        content = (
            client.get_shipment_label(shipment_id)
            if int(max_attempts or 4) == 4
            else client.get_shipment_label(shipment_id, max_attempts=max_attempts)
        )
    except MercadoAPIError as exc:
        shipment_status = _shipment_status_from_label_error(exc)
        if shipment_status:
            raise MercadoLabelUnavailable(
                f"订单 {order_id} 的运单状态为 {shipment_status}，当前没有可打印面单",
                shipment_status=shipment_status,
                permanent=shipment_status in _FINAL_NONPRINTABLE_SHIPMENT_STATUSES,
            ) from exc
        if not _is_invalid_token_error(exc) or not context.get("refresh_token"):
            raise MercadoLabelError(f"订单 {order_id}：{exc}") from exc
        refreshed = _refresh_store_token(context.get("token_id"))
        client = MercadoLibreClient(str((refreshed or {}).get("access_token") or ""))
        if hasattr(client, "timeout"):
            client.timeout = int(timeout or 30)
        try:
            content = (
                client.get_shipment_label(shipment_id)
                if int(max_attempts or 4) == 4
                else client.get_shipment_label(shipment_id, max_attempts=max_attempts)
            )
        except MercadoAPIError as retry_exc:
            shipment_status = _shipment_status_from_label_error(retry_exc)
            if shipment_status:
                raise MercadoLabelUnavailable(
                    f"订单 {order_id} 的运单状态为 {shipment_status}，当前没有可打印面单",
                    shipment_status=shipment_status,
                    permanent=shipment_status in _FINAL_NONPRINTABLE_SHIPMENT_STATUSES,
                ) from retry_exc
            raise MercadoLabelError(f"订单 {order_id}：{retry_exc}") from retry_exc
    if not content.startswith(b"%PDF"):
        raise MercadoLabelError(f"订单 {order_id} 的美客多面单接口未返回有效 PDF")
    return shipment_id, content


def _merge_pdfs(documents):
    if len(documents) == 1:
        return documents[0]
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError("多订单面单合并组件未安装，请执行 pip install pypdf") from exc
    writer = PdfWriter()
    for content in documents:
        reader = PdfReader(BytesIO(content))
        for page in reader.pages:
            writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def download_order_labels(order_ids):
    """Download every available label without letting one bad shipment abort a batch.

    A paid order is not necessarily printable: its shipment may already be
    shipped/cancelled, or Mercado may still be preparing the label.  Keep each
    shipment isolated so a mixed batch can still return the valid PDFs and so
    only successfully downloaded orders are recorded as printed.
    """
    contexts = bit_mysql.get_mercado_order_label_contexts(order_ids)
    if not contexts:
        raise MercadoLabelError("没有找到当前授权店铺下可打印的订单")
    requested_order_ids = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in order_ids or []
            if str(value or "").strip()
        )
    )
    requested = set(requested_order_ids)
    found = {str(row.get("order_id") or "") for row in contexts}
    missing = sorted(requested - found)
    if missing:
        raise MercadoLabelError(f"以下订单不存在或不属于当前授权店铺：{', '.join(missing)}")

    shipment_groups = {}
    for context in contexts:
        shipment_id = str(context.get("shipping_id") or "").strip()
        order_id = str(context.get("order_id") or "").strip()
        # Missing shipment IDs must remain isolated by order; grouping all of
        # them under an empty key would report unrelated orders as one failure.
        group_key = f"shipment:{shipment_id}" if shipment_id else f"order:{order_id}"
        shipment_groups.setdefault(group_key, []).append(context)

    documents = []
    printed_shipments = []
    printed_order_ids = []
    skipped_order_ids = []
    failed_order_ids = []
    skipped = []
    failures = []
    warnings = []
    for group in shipment_groups.values():
        group_order_ids = list(
            dict.fromkeys(
                str(row.get("order_id") or "").strip()
                for row in group
                if str(row.get("order_id") or "").strip()
            )
        )
        try:
            shipment_id, content = _download_one(group[0])
        except MercadoLabelUnavailable as exc:
            skipped_order_ids.extend(group_order_ids)
            skipped.append(
                {
                    "order_ids": group_order_ids,
                    "shipment_id": str(group[0].get("shipping_id") or ""),
                    "shipment_status": exc.shipment_status,
                    "reason": str(exc),
                    "permanent": exc.permanent,
                }
            )
            if exc.permanent:
                try:
                    bit_mysql.record_mercado_order_label_unavailable(
                        group_order_ids,
                        shipment_status=exc.shipment_status,
                        reason=str(exc),
                    )
                except Exception as record_exc:
                    warnings.append(f"不可打印状态写入失败：{record_exc}")
        except Exception as exc:
            failed_order_ids.extend(group_order_ids)
            failures.append(
                {
                    "order_ids": group_order_ids,
                    "shipment_id": str(group[0].get("shipping_id") or ""),
                    "reason": str(exc) or exc.__class__.__name__,
                }
            )
        else:
            printed_shipments.append(shipment_id)
            printed_order_ids.extend(group_order_ids)
            documents.append(content)

    if not documents:
        summary = []
        if skipped_order_ids:
            summary.append(f"{len(set(skipped_order_ids))} 个订单运单状态不可打印")
        if failed_order_ids:
            summary.append(f"{len(set(failed_order_ids))} 个订单下载失败")
        details = [row["reason"] for row in [*skipped, *failures] if row.get("reason")]
        message = "，".join(summary) or "没有可用面单"
        if details:
            message += "；" + "；".join(details[:3])
        raise MercadoLabelError(f"所选订单均未生成面单：{message}")

    merged = _merge_pdfs(documents)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = (
        f"mercado-label-{printed_shipments[0]}.pdf"
        if len(printed_shipments) == 1
        else f"mercado-labels-{len(printed_shipments)}-{timestamp}.pdf"
    )
    return {
        "content": merged,
        "filename": filename,
        "order_ids": list(dict.fromkeys(printed_order_ids)),
        "requested_order_ids": requested_order_ids,
        "shipment_count": len(printed_shipments),
        "skipped_order_ids": list(dict.fromkeys(skipped_order_ids)),
        "failed_order_ids": list(dict.fromkeys(failed_order_ids)),
        "skipped": skipped,
        "failures": failures,
        "warnings": warnings,
    }
