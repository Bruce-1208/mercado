from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from erp.mercadolibre_promotion_store import PromotionStore
from mercado_api.client import MercadoAPIError
from mercado_api.promotions import MercadoPromotionsClient


class FakeResponse:
    def __init__(self, status=200, payload=None, content=b"{}"):
        self.status_code = status
        self._payload = payload or {}
        self.content = content
        self.ok = 200 <= status < 300
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.responses.pop(0)


class FakeClient:
    BASE_URL = "https://api.example.test"
    timeout = 5

    def __init__(self, session):
        self.session = session
        self.access_token = "token-a"
        self.refreshes = 0

    def _refresh_access_token(self):
        self.refreshes += 1
        self.access_token = "token-b"


def test_deal_enrollment_uses_v2_identity_and_usd_price():
    session = FakeSession([FakeResponse(payload={"price": 15, "currency_id": "USD"})])
    api = MercadoPromotionsClient(FakeClient(session), client_id="client", caller_id="caller")

    result = api.enroll_item(
        "991", "MLM123", promotion_id="P-1", promotion_type="DEAL", deal_price="15"
    )

    assert result["currency_id"] == "USD"
    method, url, request = session.calls[0]
    assert method == "POST"
    assert url.endswith("/marketplace/seller-promotions/items/MLM123")
    assert request["params"] == {"user_id": "991"}
    assert request["json"] == {
        "promotion_type": "DEAL",
        "promotion_id": "P-1",
        "deal_price": 15.0,
    }
    assert request["headers"]["version"] == "v2"
    assert request["headers"]["X-Client-Id"] == "client"
    assert request["headers"]["X-Caller-Id"] == "caller"


def test_marketplace_campaign_does_not_accept_manual_price():
    session = FakeSession([FakeResponse(payload={"offer_id": "OFFER-1"})])
    api = MercadoPromotionsClient(FakeClient(session))
    api.enroll_item(
        "991", "MLM123", promotion_id="P-1",
        promotion_type="MARKETPLACE_CAMPAIGN", deal_price=1,
    )
    assert "deal_price" not in session.calls[0][2]["json"]


def test_mutation_network_failure_is_unknown_and_not_retried():
    session = FakeSession(error=requests.ConnectionError("lost"))
    api = MercadoPromotionsClient(FakeClient(session))
    with pytest.raises(MercadoAPIError, match="结果未知"):
        api.withdraw_item(
            "991", "MLM123", promotion_id="P-1", promotion_type="DEAL"
        )
    assert len(session.calls) == 1


def test_mutation_refreshes_once_after_explicit_401():
    session = FakeSession([
        FakeResponse(status=401, payload={"message": "expired"}),
        FakeResponse(payload={"price": 15}),
    ])
    client = FakeClient(session)
    api = MercadoPromotionsClient(client)
    api.enroll_item("991", "MLM123", promotion_id="P-1", promotion_type="DEAL", deal_price=15)
    assert client.refreshes == 1
    assert len(session.calls) == 2
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer token-b"


def test_store_replaces_items_only_for_selected_promotion(tmp_path):
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    first = store.upsert_promotion(
        token_id=1, store_name="A店", application_id="app", seller_id="11", site_id="MLM",
        row={"id": "P-1", "type": "DEAL", "name": "九月大促", "status": "started"},
    )
    second = store.upsert_promotion(
        token_id=1, store_name="A店", application_id="app", seller_id="11", site_id="MLM",
        row={"id": "P-2", "type": "DEAL", "name": "十月大促", "status": "pending"},
    )
    store.replace_items(first, [{"id": "MLM1", "status": {"id": "candidate"}, "price": 12}])
    store.replace_items(second, [{"id": "MLM2", "status": {"id": "started"}, "price": 15}])
    store.replace_items(first, [{"id": "MLM3", "status": {"id": "candidate"}, "price": 10}])

    assert [row["item_id"] for row in store.list_items(first)] == ["MLM3"]
    assert [row["item_id"] for row in store.list_items(second)] == ["MLM2"]
    rows = store.list_promotions(token_ids=[1])
    assert sum(int(row["candidate_count"] or 0) for row in rows) == 1


def test_preview_payload_round_trip(tmp_path):
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    promotion_id = store.upsert_promotion(
        token_id=1, store_name="A店", application_id="app", seller_id="11", site_id="MLM",
        row={"id": "P-1", "type": "DEAL", "name": "活动", "status": "started"},
    )
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    preview_id = store.create_preview(
        actor="operator", action="enroll", promotion_fk=promotion_id,
        payload={"rows": [{"item_id": "MLM1"}]}, expires_at=expires,
    )
    preview = store.get_preview(preview_id)
    assert preview["payload"]["rows"][0]["item_id"] == "MLM1"
    assert preview["actor"] == "operator"


def test_campaigns_can_be_filtered_by_salesperson_group_and_site(tmp_path):
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    store.upsert_promotion(
        token_id=1, store_name="A店", salesperson="小王", group_name="精品组",
        application_id="app", seller_id="11", site_id="MLM",
        row={"id": "P-1", "type": "DEAL", "name": "墨西哥活动", "status": "started"},
    )
    store.upsert_promotion(
        token_id=2, store_name="B店", salesperson="小李", group_name="铺货组",
        application_id="app", seller_id="22", site_id="MLB",
        row={"id": "P-2", "type": "DEAL", "name": "巴西活动", "status": "started"},
    )

    rows = store.list_promotions(
        salesperson="小王", group_name="精品组", site_id="MLM"
    )
    assert [row["promotion_id"] for row in rows] == ["P-1"]
    assert rows[0]["salesperson"] == "小王"
    assert rows[0]["group_name"] == "精品组"
