"""Download printable shipping-label PDFs from Mercado Libre."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from bit import bit_mysql, mercado_tokens
from mercado_api.client import MercadoAPIError, MercadoLibreClient


class MercadoLabelError(RuntimeError):
    """The selected order cannot currently provide an official Mercado label."""


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
    return "(401)" in message and any(
        marker in message for marker in ("invalid_token", "token_not_valid", "expired")
    )


def _download_one(context):
    order_id = str(context.get("order_id") or "")
    shipment_id = str(context.get("shipping_id") or "").strip()
    if not shipment_id:
        raise MercadoLabelError(f"订单 {order_id} 暂无美客多 Shipment ID，不能打印官方面单")
    access_token = str(context.get("access_token") or "").strip()
    if not access_token:
        raise MercadoLabelError(f"订单 {order_id} 所属店铺缺少 Access Token")
    client = MercadoLibreClient(access_token)
    try:
        content = client.get_shipment_label(shipment_id)
    except MercadoAPIError as exc:
        if not _is_invalid_token_error(exc) or not context.get("refresh_token"):
            raise MercadoLabelError(f"订单 {order_id}：{exc}") from exc
        refreshed = _refresh_store_token(context.get("token_id"))
        client = MercadoLibreClient(str((refreshed or {}).get("access_token") or ""))
        try:
            content = client.get_shipment_label(shipment_id)
        except MercadoAPIError as retry_exc:
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
    """Call Mercado's labels endpoint and return one printable PDF response."""
    contexts = bit_mysql.get_mercado_order_label_contexts(order_ids)
    if not contexts:
        raise MercadoLabelError("没有找到当前授权店铺下可打印的订单")
    requested = {str(value or "").strip() for value in order_ids or [] if str(value or "").strip()}
    found = {str(row.get("order_id") or "") for row in contexts}
    missing = sorted(requested - found)
    if missing:
        raise MercadoLabelError(f"以下订单不存在或不属于当前授权店铺：{', '.join(missing)}")

    documents = []
    printed_shipments = set()
    printed_order_ids = []
    for context in contexts:
        shipment_id = str(context.get("shipping_id") or "").strip()
        printed_order_ids.append(str(context.get("order_id") or ""))
        if shipment_id and shipment_id in printed_shipments:
            continue
        shipment_id, content = _download_one(context)
        printed_shipments.add(shipment_id)
        documents.append(content)
    merged = _merge_pdfs(documents)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = (
        f"mercado-label-{next(iter(printed_shipments))}.pdf"
        if len(printed_shipments) == 1
        else f"mercado-labels-{len(printed_shipments)}-{timestamp}.pdf"
    )
    return {
        "content": merged,
        "filename": filename,
        "order_ids": printed_order_ids,
        "shipment_count": len(printed_shipments),
    }
