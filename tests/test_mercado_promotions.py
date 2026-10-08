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
    assert store.all_applied_item_keys(token_ids=[1], site_id="mlm") == {
        (1, "MLM", "MLM2")
    }


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


def test_current_store_scope_finds_activities_with_stale_assignment_snapshot(tmp_path):
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    store.upsert_promotion(
        token_id=1, store_name="A店", application_id="app", seller_id="11", site_id="MLM",
        # Simulate an activity synced before the store was assigned to 半托管.
        row={"id": "P-1", "type": "DEAL", "name": "半托管活动", "status": "started"},
    )
    store.upsert_promotion(
        token_id=2, store_name="B店", application_id="app", seller_id="22", site_id="MLB",
        row={"id": "P-2", "type": "DEAL", "name": "全托管活动", "status": "started"},
    )

    rows = store.list_promotions(token_ids=[1, 2], scope_pairs=[(1, "MLM")])

    assert [row["promotion_id"] for row in rows] == ["P-1"]


def _campaign(store, *, token_id=1, promotion_id="P-1"):
    return store.upsert_promotion(
        token_id=token_id, store_name=f"店铺 {token_id}", application_id="app",
        seller_id=str(token_id), site_id="MLM",
        row={"id": promotion_id, "type": "DEAL", "name": promotion_id, "status": "started"},
    )


def _finished_job(store, campaign, results, *, action="enroll"):
    preview = store.create_preview(
        actor="operator", action=action, promotion_fk=campaign,
        payload={"promotion": store.get_promotion(campaign), "rows": []},
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    job = store.create_job(preview_id=preview, actor="operator", action=action, total=len(results))
    store.finish_job(job, results=results)
    return job


def test_latest_enrollment_results_survive_sync_and_withdrawal(tmp_path):
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    campaign = _campaign(store)
    store.replace_items(campaign, [{"id": "MLM1", "status": "candidate"}])
    original_local_id = store.list_items(campaign)[0]["id"]
    _finished_job(store, campaign, [
        {"item_id": "MLM1", "status": "failed", "message": "官方拒绝价格"},
        {"item_id": "MLM2", "status": "unknown", "message": "连接中断，结果未知"},
    ])
    success_job = _finished_job(store, campaign, [{"item_id": "MLM1", "status": "succeeded"}])
    withdraw_job = _finished_job(store, campaign, [{"item_id": "MLM1", "status": "failed"}], action="withdraw")
    store.replace_items(campaign, [{"id": "MLM1", "status": "pending"}])

    item = store.list_items(campaign)[0]
    assert item["id"] != original_local_id
    assert item["enrollment_result"]["status"] == "succeeded"
    assert item["enrollment_result"]["job_id"] == success_job
    assert store.get_promotion(campaign)["enrollment_summary"] == {
        "succeeded": 1, "failed": 0, "unknown": 1, "total": 2,
    }
    assert store.list_promotions()[0]["enrollment_summary"]["unknown"] == 1
    assert store.latest_job(campaign)["job_id"] == withdraw_job
    assert {row["item_id"] for row in store.enrollment_results(campaign)} == {"MLM1", "MLM2"}


def test_jobs_are_scoped_and_identify_the_campaign(tmp_path):
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    first, second = _campaign(store), _campaign(store, token_id=2, promotion_id="P-2")
    first_job = _finished_job(store, first, [{"item_id": "MLM1", "status": "succeeded"}])
    _finished_job(store, second, [{"item_id": "MLM1", "status": "failed"}])
    rows = store.list_jobs(token_ids=[1])
    assert [row["job_id"] for row in rows] == [first_job]
    assert rows[0]["promotion_fk"] == first
    assert rows[0]["promotion_name"] == "P-1"
    assert rows[0]["store_name"] == "店铺 1"
    assert rows[0]["site_id"] == "MLM"
    assert store.list_jobs(token_ids=[]) == []


def test_official_net_amounts_include_original_amount_currency_and_price_reference():
    from bit.mercado_promotions import _normalise_item_proceeds
    row = _normalise_item_proceeds({"promotion_type": "DEAL"}, {
        "price": 15, "original_price": 20, "currency_id": "USD",
        "net_proceeds": {"amount": 6, "original_amount": 10, "currency": "USD"},
    })
    assert row["net_proceeds_before"] == {
        "amount": 10.0, "currency_id": "USD", "source": "promotion_api",
        "applies_to_price": 20.0, "price_currency_id": "USD",
    }
    assert row["net_proceeds_after"]["amount"] == 6
    assert row["net_proceeds_after"]["applies_to_price"] == 15


def test_candidate_reference_net_amount_is_never_scaled_for_custom_price(tmp_path, monkeypatch):
    from bit import mercado_promotions as service
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    campaign = _campaign(store)
    store.replace_items(campaign, [{
        "id": "MLM1", "status": "candidate", "price": 0, "original_price": 20,
        "currency_id": "USD", "min_discounted_price": 12, "max_discounted_price": 18,
        "suggested_discounted_price": 15,
        "net_proceeds": {
            "suggested_discounted_price": {"amount": 6, "currency": "USD"},
            "max_discounted_price": {"amount": 8.4, "currency": "USD"},
            "min_discounted_price": {"amount": 3.6, "currency": "USD"},
        },
    }])
    monkeypatch.setattr(service, "enrich_item_products", lambda promotion, rows: [service._normalise_item_proceeds(promotion, row) for row in rows])
    item = store.list_items(campaign)[0]
    custom = service.create_preview(promotion_fk=campaign, action="enroll", actor="operator", store=store,
                                    rows=[{"item_local_id": item["id"], "deal_price": 14}])
    official = service.create_preview(promotion_fk=campaign, action="enroll", actor="operator", store=store,
                                      rows=[{"item_local_id": item["id"], "deal_price": 18}])
    assert custom["rows"][0]["net_proceeds_after"] is None
    assert official["rows"][0]["net_proceeds_after"]["amount"] == 8.4
    assert official["rows"][0]["net_proceeds_after"]["applies_to_price"] == 18


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "not a number", None])
def test_invalid_net_amounts_are_not_displayed(amount):
    from bit.mercado_promotions import _normalise_item_proceeds
    row = _normalise_item_proceeds({"promotion_type": "DEAL"}, {
        "price": 15, "original_price": 20, "currency_id": "USD",
        "net_proceeds": {"amount": amount, "currency": "USD"},
    })
    assert row["net_proceeds_before"] is None
    assert row["net_proceeds_after"] is None


def test_submission_reference_in_other_price_currency_cannot_match_same_number():
    from bit.mercado_promotions import _normalise_item_proceeds
    row = _normalise_item_proceeds({"promotion_type": "PRICE_DISCOUNT"}, {
        "price": 15, "original_price": 20, "currency_id": "MXN",
        "enrollment_result": {"status": "succeeded", "response": {
            "price": 15, "original_price": 20, "currency_id": "USD",
            "net_proceeds": {"amount": 6, "original_amount": 10, "currency": "USD"},
        }},
    })
    assert row["net_proceeds_before"] is None
    assert row["net_proceeds_after"] is None
    assert row["net_proceeds_options"] == []


def test_listing_reference_is_scoped_and_only_used_for_matching_original_price(monkeypatch):
    from decimal import Decimal
    from bit import mercado_promotions as service
    from erp import mercadolibre_store_link_store as links
    queries = []

    class Connection:
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def execute(self, sql, params):
            queries.append((sql, params))
        def fetchall(self):
            return [
                {"item_id": "MLM1", "price": Decimal("20"), "currency_id": "USD", "net_proceeds_manual": 1, "net_proceeds_usd": Decimal("11")},
                {"item_id": "MLM2", "price": 19, "currency_id": "USD", "net_proceeds_manual": 1, "net_proceeds_usd": 9},
                {"item_id": "MLM3", "price": 20, "currency_id": "MXN", "net_proceeds_manual": 1, "net_proceeds_usd": 7},
            ]
        def close(self):
            pass

    monkeypatch.setattr(links, "_connect", lambda: Connection())
    rows = service.enrich_item_products({"token_id": 9, "site_id": "MLM", "promotion_type": "DEAL"}, [
        {"item_id": item, "price": 15, "original_price": 20, "currency_id": "USD"}
        for item in ("MLM1", "MLM2", "MLM3")
    ])
    assert queries[0][1] == (9, "MLM", "MLM1", "MLM2", "MLM3")
    assert rows[0]["net_proceeds_before"]["amount"] == 11
    assert rows[0]["net_proceeds_before"]["source"] == "listing_manual"
    assert rows[1]["net_proceeds_before"] is None
    assert rows[2]["net_proceeds_before"] is None
    assert all(row["net_proceeds_after"] is None for row in rows)


def test_execution_keeps_mixed_results_and_official_submission_prices(tmp_path, monkeypatch):
    from bit import mercado_promotions as service
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    campaign = _campaign(store)
    store.replace_items(campaign, [
        {"id": item, "status": "candidate", "price": 18, "original_price": 20,
         "currency_id": "USD", "net_proceeds": {"amount": 9, "currency": "USD"}}
        for item in ("MLM1", "MLM2", "MLM3")
    ])
    monkeypatch.setattr(service, "enrich_item_products", lambda promotion, rows: [service._normalise_item_proceeds(promotion, row) for row in rows])
    preview = service.create_preview(
        promotion_fk=campaign, action="enroll", actor="operator", store=store,
        rows=[{"item_local_id": item["id"], "deal_price": 15} for item in store.list_items(campaign)],
    )

    class Adapter:
        def enroll_item(self, _, item_id, **kwargs):
            if item_id == "MLM2":
                raise MercadoAPIError("400: 官方价格校验失败")
            if item_id == "MLM3":
                raise MercadoAPIError("连接中断，结果未知")
            return {"price": 15, "original_price": 20, "currency_id": "USD",
                    "net_proceeds": {"amount": 6, "original_amount": 10, "currency": "USD"}}

    monkeypatch.setattr(service.bit_mysql, "get_mercado_store_token", lambda _: {})
    monkeypatch.setattr(service, "_adapter", lambda _: (Adapter(), {}))
    job = service.execute_preview(preview["preview_id"], actor="operator", store=store)
    assert [result["status"] for result in job["results"]] == ["succeeded", "failed", "unknown"]
    assert store.get_promotion(campaign)["enrollment_summary"] == {"succeeded": 1, "failed": 1, "unknown": 1, "total": 3}
    item = next(row for row in store.list_items(campaign) if row["item_id"] == "MLM1")
    service._normalise_item_proceeds(store.get_promotion(campaign), item)
    assert item["price"] == 15
    assert item["net_proceeds_after"]["amount"] == 6
    assert item["net_proceeds_after"]["applies_to_price"] == 15
    assert item["net_proceeds_before"]["amount"] == 10


def test_submission_without_net_amount_does_not_relabel_old_quote(tmp_path):
    from bit.mercado_promotions import _normalise_item_proceeds
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    campaign = _campaign(store)
    store.replace_items(campaign, [{
        "id": "MLM1", "status": "candidate", "price": 18, "original_price": 20,
        "currency_id": "USD", "net_proceeds": {"amount": 9, "original_amount": 10, "currency": "USD"},
    }])
    store.record_item_submission(campaign, "MLM1", {"price": 15, "original_price": 20, "currency_id": "USD"})
    item = _normalise_item_proceeds(store.get_promotion(campaign), store.list_items(campaign)[0])
    assert item["price"] == 15
    assert item["net_proceeds_after"] is None
    assert item["net_proceeds_before"]["amount"] == 10


def test_empty_success_response_clears_old_net_at_the_submitted_price(tmp_path, monkeypatch):
    from bit import mercado_promotions as service
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    campaign = _campaign(store)
    store.replace_items(campaign, [{
        "id": "MLM1", "status": "candidate", "price": 16, "original_price": 20,
        "currency_id": "USD", "net_proceeds": {"amount": 6, "currency": "USD"},
    }])
    monkeypatch.setattr(service, "enrich_item_products", lambda promotion, rows: [service._normalise_item_proceeds(promotion, row) for row in rows])
    preview = service.create_preview(
        promotion_fk=campaign, action="enroll", actor="operator", store=store,
        rows=[{"item_local_id": store.list_items(campaign)[0]["id"], "deal_price": 15}],
    )

    class Adapter:
        def enroll_item(self, *_args, **_kwargs):
            return {}

    monkeypatch.setattr(service.bit_mysql, "get_mercado_store_token", lambda _: {})
    monkeypatch.setattr(service, "_adapter", lambda _: (Adapter(), {}))
    submitted = service.execute_preview(preview["preview_id"], actor="operator", store=store)
    item = service._normalise_item_proceeds(store.get_promotion(campaign), store.list_items(campaign)[0])
    assert submitted["results"][0]["status"] == "succeeded"
    assert submitted["results"][0]["net_proceeds_after"] is None
    assert item["price"] == 15
    assert item["net_proceeds_after"] is None


@pytest.mark.parametrize("returned_original", [None, 35])
@pytest.mark.parametrize("returned_price", [None, 25])
def test_currency_change_uses_only_prices_returned_in_the_new_currency(tmp_path, monkeypatch, returned_original, returned_price):
    from bit import mercado_promotions as service
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    campaign = store.upsert_promotion(
        token_id=1, store_name="店铺 1", application_id="app", seller_id="1", site_id="MLM",
        row={"id": "P-1", "type": "PRICE_DISCOUNT", "name": "P-1", "status": "started"},
    )
    store.replace_items(campaign, [{
        "id": "MLM1", "status": "candidate", "price": 400, "original_price": 500,
        "currency_id": "MXN", "min_discounted_price": 10, "max_discounted_price": 450,
        "net_proceeds": {"amount": 10, "original_amount": 12, "currency": "USD"},
    }])
    monkeypatch.setattr(service, "enrich_item_products", lambda promotion, rows: [service._normalise_item_proceeds(promotion, row) for row in rows])
    preview = service.create_preview(
        promotion_fk=campaign, action="enroll", actor="operator", store=store,
        rows=[{"item_local_id": store.list_items(campaign)[0]["id"], "deal_price": 25,
               "start_date": "2026-10-10", "finish_date": "2026-10-15"}],
    )

    class Adapter:
        def enroll_item(self, *_args, **_kwargs):
            response = {"currency_id": "USD", "net_proceeds": {"amount": 20, "original_amount": 30, "currency": "USD"}}
            if returned_price is not None:
                response["price"] = returned_price
            if returned_original is not None:
                response["original_price"] = returned_original
            return response

    monkeypatch.setattr(service.bit_mysql, "get_mercado_store_token", lambda _: {})
    monkeypatch.setattr(service, "_adapter", lambda _: (Adapter(), {}))
    submitted = service.execute_preview(preview["preview_id"], actor="operator", store=store)
    result = submitted["results"][0]
    item = service._normalise_item_proceeds(store.get_promotion(campaign), store.list_items(campaign)[0])
    assert item["price"] == returned_price
    assert item["original_price"] == returned_original
    assert item["currency_id"] == "USD"
    assert item["min_price"] is None and item["max_price"] is None
    if returned_original is None:
        assert result["net_proceeds_before"] is None
        assert item["net_proceeds_before"] is None
        assert "original_price" not in item["raw"]
    else:
        assert result["net_proceeds_before"]["applies_to_price"] == returned_original
        assert item["net_proceeds_before"]["applies_to_price"] == returned_original
    if returned_price is None:
        assert result["net_proceeds_after"] is None
        assert item["net_proceeds_after"] is None
    else:
        assert result["net_proceeds_after"]["applies_to_price"] == returned_price
        assert item["net_proceeds_after"]["applies_to_price"] == returned_price


def test_routes_return_persisted_results_and_enforce_job_store_access(tmp_path, monkeypatch):
    from flask import Flask
    from bit import mercado_promotion_routes as routes
    store = PromotionStore(tmp_path / "promotions.sqlite3")
    first, second = _campaign(store), _campaign(store, token_id=2, promotion_id="P-2")
    first_job = _finished_job(store, first, [{"item_id": "MLM1", "status": "succeeded"}])
    second_job = _finished_job(store, second, [{"item_id": "MLM2", "status": "failed"}])
    monkeypatch.setattr(routes, "PromotionStore", lambda: store)
    monkeypatch.setattr(routes, "enrich_item_products", lambda _, rows: rows)
    app = Flask(__name__)
    app.register_blueprint(routes.create_promotions_blueprint(
        login_required=lambda fn: fn, current_user=lambda: {"username": "operator"},
        authorized_token_ids=lambda _: [1],
    ))
    client = app.test_client()
    body = client.get(f"/api/promotions/{first}/items").get_json()["data"]
    assert body["latest_job"]["job_id"] == first_job
    assert body["enrollment_results"][0]["status"] == "succeeded"
    assert [row["job_id"] for row in client.get("/api/promotions/jobs").get_json()["data"]["rows"]] == [first_job]
    other_preview = store.latest_job(second)["preview_id"]
    assert store.latest_job(second)["job_id"] == second_job
    assert client.get(f"/api/promotions/{second}/items").status_code == 403
    response = client.post("/api/promotions/jobs", json={"confirmed": True, "preview_id": other_preview})
    assert response.status_code == 403


def test_background_enrollment_returns_before_remote_and_saves_progress(tmp_path, monkeypatch):
    import threading
    from bit import mercado_promotions as service
    store = PromotionStore(tmp_path / "async.sqlite3")
    campaign = _campaign(store)
    preview_id = store.create_preview(
        actor="operator", action="enroll", promotion_fk=campaign,
        payload={"promotion": store.get_promotion(campaign), "rows": [
            {"item_id": "MLM1", "status": "ready", "deal_price": 15, "currency_id": "USD"},
        ]}, expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Adapter:
        def enroll_item(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return {}
    original_finish = store.finish_job
    def finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        finished.set()
    monkeypatch.setattr(store, "finish_job", finish)
    monkeypatch.setattr(service.bit_mysql, "get_mercado_store_token", lambda _: {})
    monkeypatch.setattr(service, "_adapter", lambda _: (Adapter(), {}))
    try:
        result = service.execute_preview(preview_id, actor="operator", store=store, background=True)
        assert result["status"] == "running"
        assert entered.wait(2)
        job = store.list_jobs()[0]
        assert job["unknown_count"] == 1
        assert job["results"][0]["item_id"] == "MLM1"
        with pytest.raises(ValueError, match="已提交"):
            service.execute_preview(preview_id, actor="operator", store=store, background=True)
    finally:
        release.set()
    assert finished.wait(3)
    assert store.list_jobs()[0]["success_count"] == 1


def test_gateway_error_does_not_retry_mutation():
    session = FakeSession([FakeResponse(status=502)])
    adapter = MercadoPromotionsClient(FakeClient(session))
    with pytest.raises(MercadoAPIError, match="结果未知"):
        adapter._mutation_once("POST", "/test", params={}, payload={})
    assert len(session.calls) == 1
