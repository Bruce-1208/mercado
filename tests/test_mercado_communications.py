from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from bit import bit_interface
from bit.mercado_communications import execute_store_communication
from mercado_api.communications import (
    MercadoCommunicationError,
    MercadoCommunicationsClient,
)


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {}
        self._payload = {} if payload is None else payload
        self.text = "" if payload is None else json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_pre_sale_search_and_answer_use_official_marketplace_resources():
    session = Session(
        [
            Response(payload={"total": 1, "questions": [{"id": 101}]}),
            Response(payload={"id": 101, "status": "ANSWERED"}),
        ]
    )
    client = MercadoCommunicationsClient("secret", session=session)

    questions = client.search_questions(seller_id="523130418", status="unanswered")
    answer = client.answer_question(101, "可以发货", text_translated="Sí, podemos enviarlo")

    assert questions["questions"][0]["id"] == 101
    assert answer["status"] == "ANSWERED"
    assert session.calls[0][1].endswith("/marketplace/questions/search")
    assert session.calls[0][2]["params"]["status"] == "UNANSWERED"
    assert session.calls[1][1].endswith("/marketplace/answers")
    assert session.calls[1][2]["json"] == {
        "question_id": 101,
        "text": "可以发货",
        "text_translated": "Sí, podemos enviarlo",
    }


def test_pre_sale_item_and_buyer_filters_use_official_query_names():
    session = Session([Response(payload={"total": 0, "questions": []})])
    client = MercadoCommunicationsClient("secret", session=session)

    client.search_questions(item_id="CBT910505150", user_id="466076859")

    params = session.calls[0][2]["params"]
    assert params["item"] == "CBT910505150"
    assert params["from"] == "466076859"
    assert "item_id" not in params
    assert "user_id" not in params


def test_pre_sale_sort_uses_official_query_names_and_validates_values():
    session = Session([Response(payload={"total": 0, "questions": []})])
    client = MercadoCommunicationsClient("secret", session=session)

    client.search_questions(
        seller_id="523130418",
        sort_fields=("date_created", "item_id"),
        sort_types="desc",
    )

    params = session.calls[0][2]["params"]
    assert params["sort_fields"] == "date_created,item_id"
    assert params["sort_types"] == "DESC"
    with pytest.raises(ValueError, match="排序字段"):
        client.search_questions(seller_id="523130418", sort_fields="unknown")


def test_post_sale_messages_are_pack_scoped_and_include_translation():
    session = Session([Response(payload={"id": "message-1"})])
    client = MercadoCommunicationsClient("secret", session=session)

    result = client.send_post_sale_message(
        "2315686468",
        "Your order has shipped",
        text_translated="Tu pedido ha sido enviado",
    )

    assert result["id"] == "message-1"
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/marketplace/messages/packs/2315686468")
    assert kwargs["json"]["text_translated"] == "Tu pedido ha sido enviado"


def test_workbench_reads_pack_without_marking_messages_as_read():
    records = {
        7: {
            "id": 7,
            "meli_user_id": "523130418",
            "access_token": "secret",
        }
    }
    session = Session([Response(payload={"messages": []})])

    execute_store_communication(
        7,
        "post-sale-messages",
        {"pack_id": "2315686468", "limit": 20},
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        http=session,
    )

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith("/messages/packs/2315686468/sellers/523130418")
    assert kwargs["params"]["tag"] == "post_sale"
    assert kwargs["params"]["mark_as_read"] == "false"


def test_unread_conversations_are_enriched_with_local_order_context():
    records = {
        7: {
            "id": 7,
            "meli_user_id": "523130418",
            "access_token": "secret",
        }
    }
    session = Session([
        Response(payload={
            "userId": 523130418,
            "results": [{"resource": "/packs/2315686468", "count": 2}],
        })
    ])

    result = execute_store_communication(
        7,
        "post-sale-unread",
        {},
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        get_order_contexts=lambda token_id, ids: [{
            "order_id": "2000009256002260",
            "pack_id": ids[0],
            "image_url": "https://img.example/order.jpg",
        }],
        http=session,
    )

    assert result["results"][0]["order_context"] == {
        "order_id": "2000009256002260",
        "pack_id": "2315686468",
        "image_url": "https://img.example/order.jpg",
    }


def test_claim_bundle_reads_claim_detail_messages_reason_and_reputation():
    session = Session(
        [
            Response(payload={"id": 5298903643, "reason_id": "PDD9939"}),
            Response(payload={"title": "Return awaiting response"}),
            Response(payload={"id": "PDD9939", "detail": "Wrong product"}),
            Response(payload=[{"message": "No estoy de acuerdo"}]),
            Response(payload={"affects_reputation": "affected"}),
            Response(payload=[{"expected_resolution": "return_product", "status": "pending"}]),
        ]
    )
    client = MercadoCommunicationsClient("secret", session=session)

    result = client.get_claim_bundle("5298903643")

    assert result["claim"]["id"] == 5298903643
    assert result["detail"]["title"] == "Return awaiting response"
    assert result["messages"][0]["message"] == "No estoy de acuerdo"
    assert result["reason"]["detail"] == "Wrong product"
    assert result["affects_reputation"]["affects_reputation"] == "affected"
    assert result["expected_resolutions"][0]["expected_resolution"] == "return_product"


def test_claim_message_validates_receiver_role_and_uses_action_endpoint():
    session = Session([Response(status_code=201, payload={})])
    client = MercadoCommunicationsClient("secret", session=session)

    client.send_claim_message(
        5294629673,
        "We can help",
        receiver_role="complainant",
    )

    assert session.calls[0][1].endswith(
        "/marketplace/v2/claims/5294629673/actions/send-message"
    )
    assert session.calls[0][2]["json"]["receiver_role"] == "complainant"
    with pytest.raises(ValueError, match="接收方"):
        client.send_claim_message(5294629673, "text", receiver_role="buyer")


def test_claims_1_detail_and_messages_fall_back_to_post_purchase_api():
    unavailable = {
        "message": "This functionality is not available for Claims 1.0. Please see the documentation"
    }
    session = Session(
        [
            Response(payload={"id": 5568513900, "reason_id": "PDD9939"}),
            Response(status_code=401, payload=unavailable),
            Response(payload={"title": "Legacy claim detail"}),
            Response(status_code=401, payload=unavailable),
            Response(status_code=401, payload=unavailable),
            Response(payload=[{"message": "Legacy buyer message"}]),
            Response(status_code=401, payload=unavailable),
            Response(status_code=401, payload=unavailable),
        ]
    )
    client = MercadoCommunicationsClient("secret", session=session)

    result = client.get_claim_bundle("5568513900")

    assert result["detail"]["title"] == "Legacy claim detail"
    assert result["messages"][0]["message"] == "Legacy buyer message"
    assert result["api_version"] == "claims_1"
    assert set(result["resource_errors"]) == {
        "reason", "affects_reputation", "expected_resolutions"
    }
    urls = [call[1] for call in session.calls]
    assert any(url.endswith("/post-purchase/v1/claims/5568513900/detail") for url in urls)
    assert any(url.endswith("/post-purchase/v1/claims/5568513900/messages") for url in urls)


def test_claims_1_reply_falls_back_to_legacy_messages_endpoint():
    session = Session(
        [
            Response(
                status_code=401,
                payload={"message": "This functionality is not available for Claims 1.0."},
            ),
            Response(status_code=201, payload={"id": "legacy-message"}),
        ]
    )
    client = MercadoCommunicationsClient("secret", session=session)

    result = client.send_claim_message(
        5568513900,
        "We can help",
        receiver_role="complainant",
    )

    assert result["id"] == "legacy-message"
    assert session.calls[1][1].endswith(
        "/post-purchase/v1/claims/5568513900/messages"
    )
    assert session.calls[1][2]["json"] == {
        "receiver_role": "complainant",
        "message": "We can help",
        "attachments": [],
    }


def test_claim_search_supports_after_sale_type_and_date_filters():
    session = Session([Response(payload={"paging": {"total": 0}, "data": []})])
    client = MercadoCommunicationsClient("secret", session=session)

    client.search_claims(
        "523130418",
        status="opened",
        claim_type="mediations",
        claim_id="5298903643",
        date_from="2026-08-01",
        date_to="2026-08-28",
        limit=20,
        offset=40,
    )

    params = session.calls[0][2]["params"]
    assert params["type"] == "mediations"
    assert params["id"] == "5298903643"
    assert params["range"] == (
        "date_created:after:2026-08-01T00:00:00.000+00:00,"
        "before:2026-08-28T23:59:59.999+00:00"
    )
    assert params["limit"] == 20
    assert params["offset"] == 40


def test_claim_search_rejects_invalid_type_and_inverted_dates():
    client = MercadoCommunicationsClient("secret", session=Session([]))

    with pytest.raises(ValueError, match="索赔类型"):
        client.search_claims("523130418", claim_type="unknown")
    with pytest.raises(ValueError, match="起始日期"):
        client.search_claims(
            "523130418", date_from="2026-08-28", date_to="2026-08-01"
        )


def test_claim_adapter_defaults_unfiltered_search_to_opened():
    records = {
        7: {
            "id": 7,
            "meli_user_id": "523130418",
            "access_token": "secret",
        }
    }
    session = Session([Response(payload={"paging": {"total": 0}, "data": []})])

    execute_store_communication(
        7,
        "claims-list",
        {"limit": 1, "offset": 0},
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        http=session,
    )

    assert session.calls[0][2]["params"]["status"] == "opened"


def test_claim_adapter_uses_cbt_marketplace_child_sellers_and_merges_results():
    records = {
        7: {
            "id": 7,
            "meli_user_id": "root-seller",
            "site_id": "CBT",
            "access_token": "secret",
        }
    }
    session = Session([
        Response(payload={"marketplaces": [
            {"site_id": "MLM", "user_id": "70001"},
            {"site_id": "MLB", "user_id": "70002"},
        ]}),
        Response(payload={
            "paging": {"total": 1},
            "data": [{"id": 101, "last_updated": "2026-08-27T10:00:00Z"}],
        }),
        Response(payload={
            "paging": {"total": 1},
            "data": [{"id": 102, "last_updated": "2026-08-28T10:00:00Z"}],
        }),
    ])

    result = execute_store_communication(
        7,
        "claims-list",
        {"status": "opened", "limit": 20, "offset": 0},
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        http=session,
    )

    assert session.calls[0][1].endswith("/marketplace/users/root-seller")
    assert session.calls[1][2]["params"]["user_id"] == "70001"
    assert session.calls[2][2]["params"]["user_id"] == "70002"
    assert result["paging"]["total"] == 2
    assert [row["id"] for row in result["data"]] == [102, 101]
    assert [row["site_id"] for row in result["data"]] == ["MLB", "MLM"]


def test_unauthorized_request_refreshes_once_and_retries_with_new_token():
    session = Session(
        [Response(status_code=401, payload={"message": "expired"}), Response(payload={"id": 10})]
    )
    refreshed = []

    def refresh():
        refreshed.append(True)
        return "new-secret"

    client = MercadoCommunicationsClient(
        "old-secret", refresh_access_token=refresh, session=session
    )

    assert client.get_question(10)["id"] == 10
    assert refreshed == [True]
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer old-secret"
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer new-secret"


def test_model_6_forbidden_has_actionable_error():
    session = Session(
        [
            Response(
                status_code=403,
                payload={"message": "Forbidden for CBT model 6 sellers."},
            )
        ]
    )
    client = MercadoCommunicationsClient("secret", session=session, max_attempts=1)

    with pytest.raises(MercadoCommunicationError) as error:
        client.search_questions(seller_id="523130418")

    assert error.value.model_6_restricted
    assert "Model 6" in str(error.value)


def test_workbench_adapter_refreshes_server_side_without_returning_token():
    records = {
        7: {
            "id": 7,
            "meli_user_id": "523130418",
            "access_token": "old-secret",
        }
    }
    session = Session(
        [
            Response(status_code=401, payload={"message": "expired"}),
            Response(payload={"total": 0, "questions": []}),
        ]
    )

    def refresh(token_id):
        records[token_id]["access_token"] = "new-secret"
        return {"id": token_id}  # 与工作台真实刷新函数一样，不返回密钥。

    result = execute_store_communication(
        7,
        "pre-sale-list",
        {"status": "UNANSWERED"},
        get_token=records.get,
        refresh_token=refresh,
        http=session,
    )

    assert result == {"total": 0, "questions": []}
    assert "secret" not in json.dumps(result)
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer new-secret"


def test_pre_sale_summary_returns_status_totals_for_dashboard_tabs():
    records = {
        7: {
            "id": 7,
            "meli_user_id": "523130418",
            "access_token": "secret",
        }
    }
    totals = [24, 7, 12, 2, 1, 2]
    session = Session([
        Response(payload={"total": total, "questions": []}) for total in totals
    ])

    result = execute_store_communication(
        7,
        "pre-sale-summary",
        {"item_id": "CBT910505150", "user_id": "466076859"},
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        http=session,
    )

    assert result == {
        "total": 24,
        "counts": {
            "UNANSWERED": 7,
            "ANSWERED": 12,
            "CLOSED_UNANSWERED": 2,
            "UNDER_REVIEW": 1,
            "BANNED": 2,
        },
    }
    assert all(call[2]["params"]["item"] == "CBT910505150" for call in session.calls)
    assert all(call[2]["params"]["from"] == "466076859" for call in session.calls)


def test_pre_sale_translation_converts_local_question_text_to_chinese():
    records = {7: {"id": 7, "meli_user_id": "523130418", "access_token": "secret"}}
    calls = []

    result = execute_store_communication(
        7,
        "pre-sale-translate",
        {
            "texts": ["¿Tienen stock?"],
            "site_id": "MLM",
            "target_language": "zh-CN",
        },
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        http=Session([]),
        translator=lambda texts, source, target: calls.append((texts, source, target))
        or ["有库存吗？"],
    )

    assert result["translations"] == ["有库存吗？"]
    assert calls == [(["¿Tienen stock?"], "es", "zh-CN")]


def test_pre_sale_translation_can_auto_detect_question_source_language():
    records = {7: {"id": 7, "meli_user_id": "523130418", "access_token": "secret"}}
    calls = []

    result = execute_store_communication(
        7,
        "pre-sale-translate",
        {
            "texts": ["Is this compatible?"],
            "source_language": "auto",
            "target_language": "zh-CN",
        },
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        http=Session([]),
        translator=lambda texts, source, target: calls.append((texts, source, target))
        or ["这个兼容吗？"],
    )

    assert result["translations"] == ["这个兼容吗？"]
    assert calls == [(["Is this compatible?"], "auto", "zh-CN")]


def test_pre_sale_chinese_reply_is_translated_to_buyer_language_before_send():
    records = {7: {"id": 7, "meli_user_id": "523130418", "access_token": "secret"}}
    session = Session([Response(payload={"id": 101, "status": "ANSWERED"})])

    result = execute_store_communication(
        7,
        "pre-sale-answer",
        {
            "question_id": 101,
            "text": "有库存，今天可以发货。",
            "site_id": "MLB",
            "auto_translate": True,
        },
        get_token=records.get,
        refresh_token=lambda _token_id: None,
        http=session,
        translator=lambda texts, source, target: ["Temos estoque e podemos enviar hoje."],
    )

    assert session.calls[0][2]["json"] == {
        "question_id": 101,
        "text": "有库存，今天可以发货。",
        "text_translated": "Temos estoque e podemos enviar hoje.",
    }
    assert result["translation"]["target_language"] == "pt-BR"


def _workbench_user(*permissions):
    return {
        "id": 12,
        "username": "support-agent",
        "permissions": list(permissions),
        "access_version": 1,
    }


def test_pre_sale_aggregate_queries_stores_with_thread_pool(monkeypatch):
    lock = threading.Lock()
    active = 0
    max_active = 0
    thread_names = set()

    def fake_query(token_id, action, payload):
        nonlocal active, max_active
        assert action == "pre-sale-list"
        assert payload["limit"] == 100
        with lock:
            active += 1
            max_active = max(max_active, active)
            thread_names.add(threading.current_thread().name)
        time.sleep(0.04)
        with lock:
            active -= 1
        return {
            "total": 1,
            "questions": [{"id": token_id * 100, "date_created": "2026-08-31"}],
        }

    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "execute_mercado_store_communication",
        fake_query,
    )

    response = bit_interface.app.test_client().post(
        "/api/mercado-communications/pre-sale-aggregate",
        json={"token_ids": [1, 2, 3, 4], "status": "UNANSWERED", "workers": 4},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["workers"] == 4
    assert data["success_stores"] == 4
    assert [row["token_id"] for row in data["stores"]] == [1, 2, 3, 4]
    assert max_active > 1
    assert len(thread_names) > 1


def test_customer_service_aggregate_queries_claim_stores_with_thread_pool(monkeypatch):
    lock = threading.Lock()
    active = 0
    max_active = 0
    thread_names = set()

    def fake_query(token_id, action, payload):
        nonlocal active, max_active
        assert action == "claims-list"
        with lock:
            active += 1
            max_active = max(max_active, active)
            thread_names.add(threading.current_thread().name)
        time.sleep(0.04)
        with lock:
            active -= 1
        status = payload["status"]
        return {
            "paging": {"total": 1},
            "data": [{
                "id": token_id * 100 + (1 if status == "opened" else 2),
                "status": status,
                "last_updated": "2026-08-31T10:00:00Z",
            }],
        }

    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "execute_mercado_store_communication",
        fake_query,
    )

    response = bit_interface.app.test_client().post(
        "/api/mercado-communications/customer-service-aggregate",
        json={
            "mode": "claims",
            "token_ids": [1, 2, 3, 4],
            "required_rows": 20,
            "workers": 4,
        },
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["workers"] == 4
    assert data["success_stores"] == 4
    assert data["failed_stores"] == 0
    assert [row["token_id"] for row in data["stores"]] == [1, 2, 3, 4]
    assert all(row["total"] == 2 for row in data["stores"])
    assert max_active > 1
    assert len(thread_names) > 1


def test_customer_service_aggregate_keeps_single_store_failure_isolated(monkeypatch):
    def fake_query(token_id, action, payload):
        assert action == "claims-list"
        if token_id == 2:
            raise RuntimeError("店铺接口暂不可用")
        return {"paging": {"total": 0}, "data": []}

    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "execute_mercado_store_communication",
        fake_query,
    )

    response = bit_interface.app.test_client().post(
        "/api/mercado-communications/customer-service-aggregate",
        json={"mode": "claims", "token_ids": [1, 2], "workers": 2},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["success_stores"] == 1
    assert data["failed_stores"] == 1
    assert data["stores"][0]["token_id"] == 1
    assert data["failures"] == [{
        "token_id": 2,
        "message": "店铺接口暂不可用",
    }]


def test_customer_service_view_can_read_but_cannot_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "execute_mercado_store_communication",
        lambda token_id, action, payload: calls.append((token_id, action, payload))
        or {"questions": []},
    )
    client = bit_interface.app.test_client()

    read = client.get(
        "/api/mercado-communications/7/pre-sale-list?status=UNANSWERED"
    )
    reply = client.post(
        "/api/mercado-communications/7/pre-sale-answer",
        json={"question_id": 1, "text": "answer"},
    )

    assert read.status_code == 200
    assert calls == [(7, "pre-sale-list", {"status": "UNANSWERED"})]
    assert reply.status_code == 403
    assert reply.get_json()["required_permissions"] == ["customer_service.manage"]


def test_customer_service_view_can_read_pre_sale_summary(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "execute_mercado_store_communication",
        lambda _token_id, _action, _payload: {"total": 3, "counts": {}},
    )

    response = bit_interface.app.test_client().get(
        "/api/mercado-communications/7/pre-sale-summary"
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["total"] == 3


def test_customer_service_view_can_request_pre_sale_translation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "execute_mercado_store_communication",
        lambda token_id, action, payload: calls.append((token_id, action, payload))
        or {"translations": ["你好"]},
    )

    response = bit_interface.app.test_client().post(
        "/api/mercado-communications/7/pre-sale-translate",
        json={"texts": ["Hola"], "source_language": "es", "target_language": "zh-CN"},
    )

    assert response.status_code == 200
    assert calls[0][0:2] == (7, "pre-sale-translate")


def test_pre_sale_workbench_module_has_filters_table_pagination_and_reply():
    template = (Path(__file__).resolve().parents[1] / "bit" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'data-tab="pre-sale"' in template
    assert 'id="tab-pre-sale"' in template
    assert 'id="pre-sale-salesperson"' in template
    assert 'id="pre-sale-group"' in template
    assert 'id="pre-sale-status-bar"' in template
    assert 'class="pre-sale-table"' in template
    assert 'id="pre-sale-pagination"' in template
    assert 'pre-sale-summary' in template
    assert 'pre-sale-answer' in template
    assert 'pre-sale-delete' in template
    assert 'function preSaleMatchedStores' in template
    assert 'pre-sale-aggregate' in template
    assert '_token_id: tokenId' in template
    assert '线程完成查询' in template
    assert 'sendPreSaleAnswerForModule(questionId, tokenId, siteId)' in template
    assert 'id="pre-sale-reply-preview"' in template
    assert 'auto_translate: true' in template
    assert 'target_language: "zh-CN"' in template
    assert "客户提问（中文翻译）" in template
    assert 'chinesePrefix = "中文："' in template


def test_customer_service_manager_can_send_claim_message(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user(
            "customer_service.view", "customer_service.manage"
        ),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "execute_mercado_store_communication",
        lambda token_id, action, payload: calls.append((token_id, action, payload))
        or {},
    )
    client = bit_interface.app.test_client()

    response = client.post(
        "/api/mercado-communications/9/claims-send",
        json={
            "claim_id": "5294629673",
            "receiver_role": "complainant",
            "message": "We can help",
        },
    )

    assert response.status_code == 200
    assert calls[0][0:2] == (9, "claims-send")
    assert calls[0][2]["receiver_role"] == "complainant"


def test_customer_service_claim_can_open_exact_store_bit_browser(monkeypatch):
    opened = []
    released = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {"rows": [{
            "id": 9,
            "display_name": "张泽文888",
            "nickname": "official-nickname",
        }]},
    )
    monkeypatch.setattr(
        bit_interface,
        "list_shop_configs",
        lambda include_ignored=True: [{
            "shop_name": "张泽文888",
            "window_id": "window-abc",
            "status": "",
        }],
    )
    monkeypatch.setattr(
        bit_interface,
        "openBrowser",
        lambda window_id, **kwargs: opened.append((window_id, kwargs))
        or {"success": True},
    )
    monkeypatch.setattr(
        bit_interface,
        "releaseBrowserLease",
        lambda window_id: released.append(window_id),
    )

    response = bit_interface.app.test_client().post(
        "/api/mercado-claims/9/open-browser",
        json={"claim_id": "5568513900"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["window_id"] == "window-abc"
    assert response.get_json()["data"]["claim_id"] == "5568513900"
    assert opened == [(
        "window-abc",
        {"api_lock_timeout": 5, "request_timeout": 20},
    )]
    assert released == ["window-abc"]


def test_customer_service_claim_browser_uses_order_shop_name_hint(monkeypatch):
    opened = []
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {"rows": [{
            "id": 9,
            "display_name": "授权自定义名称",
            "nickname": "official-nickname",
        }]},
    )
    monkeypatch.setattr(
        bit_interface,
        "list_shop_configs",
        lambda include_ignored=True: [{
            "shop_name": "订单所属店铺",
            "window_id": "window-from-order",
        }],
    )
    monkeypatch.setattr(
        bit_interface,
        "openBrowser",
        lambda window_id, **_kwargs: opened.append(window_id) or {"success": True},
    )
    monkeypatch.setattr(bit_interface, "releaseBrowserLease", lambda _window_id: None)

    response = bit_interface.app.test_client().post(
        "/api/mercado-claims/9/open-browser",
        json={"claim_id": "5568513900", "shop_name": "订单所属店铺"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["window_id"] == "window-from-order"
    assert opened == ["window-from-order"]


def test_customer_service_claim_browser_requires_exact_store_binding(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "get_current_workbench_user",
        lambda: _workbench_user("customer_service.view"),
    )
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {"rows": [{"id": 9, "display_name": "未绑定店铺"}]},
    )
    monkeypatch.setattr(bit_interface, "list_shop_configs", lambda **_kwargs: [])

    response = bit_interface.app.test_client().post(
        "/api/mercado-claims/9/open-browser",
        json={"claim_id": "5568513900"},
    )

    assert response.status_code == 400
    assert "未绑定比特浏览器窗口" in response.get_json()["message"]


def test_customer_service_supports_owner_group_aggregation_and_row_store_replies():
    template = (
        Path(__file__).resolve().parents[1] / "bit" / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="customer-service-salesperson"' in template
    assert 'id="customer-service-group"' in template
    assert "function customerServiceTargetStores()" in template
    assert "customerServiceAllRows = await customerServiceLoadPostSale(stores, search)" in template
    assert "/api/mercado-communications/customer-service-aggregate" in template
    assert "服务端 ${customerServiceQueryMetrics.workers} 线程" in template
    assert "token_id: Number(tokenId || store.id || 0)" in template
    assert 'customerServiceRequest("post-sale-send", {method: "POST", tokenId' in template
    assert 'customerServiceRequest("claims-send", {method: "POST", tokenId' in template
    assert "await loadClaimDetail(row.id, row.token_id)" in template
    assert "openClaimBitBrowser(${index},this)" in template
    assert "/api/mercado-claims/${tokenId}/open-browser" in template
    assert 'class="claim-browser-status"' in template
    assert 'shop_name: String(row.order_context?.shop_name || "")' in template
    assert 'button.textContent = "↻ 重试"' in template
    assert "旧版 Claims 1.0" in template
