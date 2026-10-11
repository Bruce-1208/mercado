"""Product reads are bounded, batch-based and independent of question loading."""
import json

import pytest

from bit.mercado_communications import execute_store_communication
from mercado_api.communications import MercadoCommunicationsClient


class Response:
    status_code = 200
    ok = True
    headers = {}

    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response([
            {"code": 404, "body": {"message": "not found"}} if identifier == "MLM404" else
            {"code": 200, "body": {
                "id": identifier, "title": "Título del producto",
                "thumbnail": "http://images.test/thumb.jpg",
                "pictures": [{"secure_url": "https://images.test/main.jpg"}],
            }} for identifier in kwargs["params"]["ids"].split(",")
        ])


def test_batch_products_deduplicates_ids_and_keeps_full_primary_picture():
    session = Session()
    result = execute_store_communication(
        7, "pre-sale-products", {"item_ids": "MLM123,CBT123,MLM123,MLM404"},
        get_token=lambda _: {"access_token": "secret"}, refresh_token=lambda _: None,
        http=session,
    )
    assert len(session.calls) == 1
    assert session.calls[0][1].endswith("/items")
    assert session.calls[0][2]["params"]["ids"] == "MLM123,CBT123,MLM404"
    assert result["products"]["MLM123"] == {
        "id": "MLM123", "title": "Título del producto", "image_url": "https://images.test/main.jpg",
    }
    assert "MLM404" in result["errors"]
    assert "secret" not in json.dumps(result)


def test_product_reads_split_batches_at_twenty():
    session = Session()
    client = MercadoCommunicationsClient("secret", session=session)
    result = client.get_question_products([f"MLM{i}" for i in range(40)])
    assert len(result["products"]) == 40
    assert [len(call[2]["params"]["ids"].split(",")) for call in session.calls] == [20, 20]


@pytest.mark.parametrize("identifiers", [[], ["MLM123/description"], ["../items"],
                                         [f"MLM{i}" for i in range(41)], None])
def test_invalid_product_ids_rejected_before_network_call(identifiers):
    session = Session()
    client = MercadoCommunicationsClient("secret", session=session)
    with pytest.raises(ValueError):
        client.get_question_products(identifiers)
    assert not session.calls


def test_thumbnail_is_fallback_and_http_upgraded():
    client = MercadoCommunicationsClient("secret")
    client.request = lambda *args, **kwargs: [{"code": 200, "body": {
        "id": "CBT123", "title": "Global product", "thumbnail": "http://images.test/thumb.jpg",
    }}]
    assert client.get_question_products(["CBT123"])["products"]["CBT123"]["image_url"] == "https://images.test/thumb.jpg"


def test_view_only_user_can_read_product_information(monkeypatch):
    from bit import bit_interface
    from test_mercado_communications import _workbench_user

    calls = []
    monkeypatch.setattr(bit_interface, "get_current_workbench_user",
                        lambda: _workbench_user("customer_service.view"))
    monkeypatch.setattr(bit_interface.bit_db_api, "execute_mercado_store_communication",
                        lambda token_id, action, payload: calls.append((token_id, action, payload))
                        or {"products": {"MLM123": {"title": "Product"}}})
    response = bit_interface.app.test_client().get(
        "/api/mercado-communications/7/pre-sale-products?item_ids=MLM123")
    assert response.status_code == 200
    assert calls == [(7, "pre-sale-products", {"item_ids": "MLM123"})]
    assert response.headers["Cache-Control"] == "no-store"
