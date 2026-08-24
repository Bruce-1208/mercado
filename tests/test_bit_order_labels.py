from io import BytesIO

import pytest

from bit import bit_order_labels


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


def test_download_order_labels_rejects_order_without_shipment():
    with pytest.raises(bit_order_labels.MercadoLabelError, match="Shipment ID"):
        bit_order_labels._download_one(_context(shipment_id=""))
