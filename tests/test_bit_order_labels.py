from io import BytesIO

import pytest

from bit import bit_order_labels
from mercado_api.client import MercadoAPIError


def _context(order_id="20001", shipment_id="30001"):
    return {
        "order_id": order_id,
        "shipping_id": shipment_id,
        "token_id": 7,
        "shop_name": "墨西哥一店",
        "access_token": "token-value",
        "refresh_token": "refresh-value",
    }


def test_download_order_labels_calls_mercado_shipment_label_endpoint(monkeypatch):
    seen = []

    class Client:
        def __init__(self, access_token):
            assert access_token == "token-value"

        def get_shipment_label(self, shipment_id):
            seen.append(shipment_id)
            return b"%PDF-1.4\nofficial-label\n%%EOF"

    monkeypatch.setattr(
        bit_order_labels.bit_mysql,
        "get_mercado_order_label_contexts",
        lambda _ids: [_context()],
    )
    monkeypatch.setattr(bit_order_labels, "MercadoLibreClient", Client)

    result = bit_order_labels.download_order_labels(["20001"])

    assert seen == ["30001"]
    assert result["content"].startswith(b"%PDF")
    assert result["filename"] == "mercado-label-30001.pdf"
    assert result["shipment_count"] == 1


def test_download_order_labels_deduplicates_pack_shipment(monkeypatch):
    calls = []
    contexts = [_context("20001", "30001"), _context("20002", "30001")]
    monkeypatch.setattr(
        bit_order_labels.bit_mysql,
        "get_mercado_order_label_contexts",
        lambda _ids: contexts,
    )

    def download(context):
        calls.append(context["shipping_id"])
        return context["shipping_id"], b"%PDF-1.4\npack-label\n%%EOF"

    monkeypatch.setattr(bit_order_labels, "_download_one", download)

    result = bit_order_labels.download_order_labels(["20001", "20002"])

    assert calls == ["30001"]
    assert result["order_ids"] == ["20001", "20002"]
    assert result["shipment_count"] == 1


def test_download_order_labels_returns_partial_pdf_when_one_shipment_is_unavailable(
    monkeypatch,
):
    contexts = [_context("20001", "30001"), _context("20002", "30002")]
    monkeypatch.setattr(
        bit_order_labels.bit_mysql,
        "get_mercado_order_label_contexts",
        lambda _ids: contexts,
    )

    def download(context):
        if context["shipping_id"] == "30002":
            raise bit_order_labels.MercadoLabelUnavailable(
                "运单已发货",
                shipment_status="shipped",
                permanent=True,
            )
        return context["shipping_id"], b"%PDF-1.4\nprintable\n%%EOF"

    recorded = []
    monkeypatch.setattr(bit_order_labels, "_download_one", download)
    monkeypatch.setattr(
        bit_order_labels.bit_mysql,
        "record_mercado_order_label_unavailable",
        lambda order_ids, **_kwargs: recorded.extend(order_ids) or len(order_ids),
    )

    result = bit_order_labels.download_order_labels(["20001", "20002"])

    assert result["content"].startswith(b"%PDF")
    assert result["order_ids"] == ["20001"]
    assert result["skipped_order_ids"] == ["20002"]
    assert result["failed_order_ids"] == []
    assert recorded == ["20002"]


def test_download_order_labels_reports_all_unavailable_orders_together(monkeypatch):
    contexts = [_context("20001", "30001"), _context("20002", "30002")]
    monkeypatch.setattr(
        bit_order_labels.bit_mysql,
        "get_mercado_order_label_contexts",
        lambda _ids: contexts,
    )
    monkeypatch.setattr(
        bit_order_labels,
        "_download_one",
        lambda _context: (_ for _ in ()).throw(
            bit_order_labels.MercadoLabelUnavailable(
                "运单已发货",
                shipment_status="shipped",
                permanent=True,
            )
        ),
    )
    monkeypatch.setattr(
        bit_order_labels.bit_mysql,
        "record_mercado_order_label_unavailable",
        lambda order_ids, **_kwargs: len(order_ids),
    )

    with pytest.raises(bit_order_labels.MercadoLabelError, match="2 个订单运单状态不可打印"):
        bit_order_labels.download_order_labels(["20001", "20002"])


def test_download_order_labels_rejects_order_without_shipment():
    with pytest.raises(bit_order_labels.MercadoLabelError, match="Shipment ID"):
        bit_order_labels._download_one(_context(shipment_id=""))


def test_cancelled_shipment_is_classified_as_permanently_unavailable(monkeypatch):
    class Client:
        def __init__(self, _access_token):
            pass

        def get_shipment_label(self, _shipment_id):
            raise MercadoAPIError(
                "GET labels 失败 (401): Unauthorized shipments: 30001: "
                "Shipment status is 'cancelled'"
            )

    monkeypatch.setattr(bit_order_labels, "MercadoLibreClient", Client)

    with pytest.raises(bit_order_labels.MercadoLabelUnavailable) as caught:
        bit_order_labels._download_one(_context())

    assert caught.value.shipment_status == "cancelled"
    assert caught.value.permanent is True


def test_token_not_valid_400_is_recognized_for_refresh():
    error = MercadoAPIError(
        "GET labels 失败 (400): Malformed access_token: TOKEN_NOT_VALID"
    )

    assert bit_order_labels._is_invalid_token_error(error) is True
