"""Communication date regressions without importing the database-backed console."""

from __future__ import annotations

import ast
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import time

from flask import Flask, jsonify, request
import pytest

from bit import mercado_communications as communications


WEEK = {"date_from": "2026-09-30", "date_to": "2026-10-06"}


def _console_functions(execute):
    names = {
        "_query_pre_sale_store_pages", "api_mercado_pre_sale_aggregate",
        "_query_customer_service_post_sale_bundle",
        "_query_customer_service_post_sale_store",
        "api_mercado_customer_service_aggregate",
    }
    source = Path(__file__).resolve().parents[1] / "bit" / "bit_interface.py"
    functions = []
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            functions.append(node)
    namespace = {
        "bit_db_api": SimpleNamespace(execute_mercado_store_communication=execute,
            list_mercado_message_pack_candidates=lambda _: []),
        "_authorized_token_ids_for_user": lambda: None,
        "mercado_communications": communications,
        "request": request, "jsonify": jsonify,
        "ThreadPoolExecutor": ThreadPoolExecutor, "as_completed": as_completed,
        "time": time, "json": json,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def test_calendar_range_includes_beijing_midnight_and_excludes_next_midnight():
    bounds = communications.communication_date_bounds(WEEK)
    assert communications.communication_date_matches("2026-09-29T16:00:00Z", bounds)
    assert communications.communication_date_matches("2026-10-06T15:59:59.999Z", bounds)
    assert not communications.communication_date_matches("2026-09-29T15:59:59Z", bounds)
    assert not communications.communication_date_matches("2026-10-06T16:00:00Z", bounds)
    assert not communications.communication_date_matches("invalid", bounds)


@pytest.mark.parametrize("dates", [
    {"date_from": "2026-9-30"}, {"date_to": "2026-02-30"},
    {"date_to": "2026-10-06T00:00:00Z"},
    {"date_from": "2026-10-06", "date_to": "2026-09-30"},
])
def test_invalid_calendar_ranges_are_rejected(dates):
    with pytest.raises(ValueError):
        communications.communication_date_bounds(dates)


def test_question_pagination_filters_and_stops_when_it_crosses_range_start():
    calls = []
    pages = [
        {"total": 5000, "questions": [
            {"id": 1, "date_created": "2026-10-07T00:00:00+08:00"},
            {"id": 2, "date_created": "2026-10-06T23:59:59+08:00"},
        ]},
        {"total": 5000, "questions": [
            {"id": 3, "date_created": "2026-09-29T16:00:00Z"},
            {"id": 4, "date_created": "2026-09-29T15:59:59Z"},
        ]},
    ]

    def search(filters):
        calls.append(filters)
        return pages.pop(0)

    result = communications.query_pre_sale_question_pages(search, {**WEEK, "status": "ANSWERED"})
    assert [row["id"] for row in result["questions"]] == [2, 3]
    assert result["official_total"] == 2
    assert result["source_total"] == 5000
    assert result["truncated"] is False
    assert [call["offset"] for call in calls] == [0, 2]
    assert all(call["status"] == "ANSWERED" and call["sort_types"] == "DESC" for call in calls)
    assert all("date_from" not in call and "date_to" not in call for call in calls)


def test_question_api_offset_limit_keeps_count_as_partial_period_count():
    calls = []

    def search(filters):
        calls.append(filters["offset"])
        return {"total": 2000, "questions": [
            {"id": filters["offset"] + index, "date_created": "2026-10-06T00:00:00Z"}
            for index in range(100)
        ]}

    result = communications.query_pre_sale_question_pages(search, WEEK)
    assert calls == list(range(0, 1001, 100))
    assert result["truncated"] is True
    assert result["official_total"] == 1100
    assert result["source_total"] == 2000


def test_period_summary_queries_all_statuses_once_and_counts_period_rows(monkeypatch):
    calls = []

    def search_questions(**filters):
        calls.append(filters)
        return {"total": 300, "questions": [
            {"id": 1, "status": "ANSWERED", "date_created": "2026-10-06T00:00:00Z"},
            {"id": 2, "status": "UNANSWERED", "date_created": "2026-09-30T00:00:00Z"},
            {"id": 3, "status": "UNANSWERED", "date_created": "2026-09-28T00:00:00Z"},
        ]}

    monkeypatch.setattr(communications, "_store_client", lambda *args, **kwargs: (
        SimpleNamespace(search_questions=search_questions), {"meli_user_id": "123"}
    ))
    result = communications.execute_store_communication(
        7, "pre-sale-summary", {**WEEK, "status": "ANSWERED"},
        get_token=lambda _: {}, refresh_token=lambda _: None,
    )
    assert len(calls) == 1
    assert "status" not in calls[0]
    assert result["total"] == 2
    assert result["counts"]["ANSWERED"] == result["counts"]["UNANSWERED"] == 1
    assert result["truncated"] is False


def test_post_sale_pack_pagination_finds_recent_messages_after_old_first_page():
    calls = []

    def execute(token_id, action, payload):
        calls.append(payload)
        messages = [
            {"id": str(index), "message_date": {"created": "2026-09-28T00:00:00Z"}}
            for index in range(100)
        ] if not payload["offset"] else [
            {"id": "recent", "message_date": {"received": "2026-10-06T15:59:59Z"}}
        ]
        return {"paging": {"total": 101}, "messages": messages,
                "conversation_status": {"status": "active"},
                "order_context": {"order_id": "900"}}

    funcs = _console_functions(execute)
    result = funcs["_query_customer_service_post_sale_bundle"](7, "100", WEEK)
    assert [call["offset"] for call in calls] == [0, 100]
    assert [row["id"] for row in result["messages"]] == ["recent"]
    assert result["paging"]["total"] == 1
    assert result["order_context"] == {"order_id": "900"}
    assert result["truncated"] is False


def test_post_sale_store_excludes_old_packs_and_counts_only_period_unread():
    def execute(token_id, action, payload):
        if action == "post-sale-unread":
            return {"results": [
                {"resource": "/packs/100", "count": 4, "order_context": {"order_id": "900"}},
                {"resource": "/packs/200", "count": 1},
            ]}
        if payload["pack_id"] == "200":
            messages = [{"id": "old", "message_date": {"created": "2026-09-28T00:00:00Z"}}]
        else:
            messages = [
                {"id": "incoming", "from": {"user_id": 456},
                 "message_date": {"created": "2026-10-06T00:00:00Z", "read": None}},
                {"id": "outgoing", "from": {"user_id": 123},
                 "message_date": {"created": "2026-10-06T01:00:00Z", "read": None}},
                {"id": "read", "from": {"user_id": 456},
                 "message_date": {"created": "2026-10-06T02:00:00Z", "read": "2026-10-06T03:00:00Z"}},
            ]
        return {"paging": {"total": len(messages)}, "messages": messages, "seller_id": "123"}

    result = _console_functions(execute)["_query_customer_service_post_sale_store"](7, filters=WEEK)
    assert result["unread_total"] == 1
    assert [row["pack_id"] for row in result["conversations"]] == ["100"]
    row = result["conversations"][0]
    assert row["unread_count"] == 1
    assert row["order_context"] == {"order_id": "900"}
    assert row["bundle"]["paging"]["total"] == 3


def test_post_sale_search_reports_no_date_match_for_empty_period():
    calls = []

    def execute(token_id, action, payload):
        calls.append(payload)
        return {"paging": {"total": 1}, "messages": [
            {"message_date": {"created": "2026-09-28T00:00:00Z"}}
        ]}

    result = _console_functions(execute)["_query_customer_service_post_sale_store"](7, "100", WEEK)
    assert result["date_match"] is False
    assert result["search_bundle"]["messages"] == []
    assert result["search_bundle"]["paging"]["total"] == 0
    assert calls[0]["include_order_context"] is True


def test_post_sale_message_action_attaches_searched_order_context(monkeypatch):
    monkeypatch.setattr(communications, "_store_client", lambda *args, **kwargs: (
        SimpleNamespace(get_post_sale_messages_for_seller=lambda *args, **kwargs: {"messages": []}),
        {"meli_user_id": "123"},
    ))
    context_calls = []

    def contexts(token_id, identifiers):
        context_calls.append((token_id, identifiers))
        return [{"pack_id": "100", "order_id": "900"}]

    result = communications.execute_store_communication(
        7, "post-sale-messages", {"pack_id": "100", "include_order_context": True},
        get_token=lambda _: {}, refresh_token=lambda _: None, get_order_contexts=contexts,
    )
    assert context_calls == [(7, ["100"])]
    assert result["order_context"]["order_id"] == "900"
    assert result["seller_id"] == "123"


@pytest.mark.parametrize("route, body, name", [
    ("/pre", {"token_ids": [7], "date_from": "invalid"}, "api_mercado_pre_sale_aggregate"),
    ("/post", {"token_ids": [7], "mode": "post-sale", "filters": {"date_to": "invalid"}},
     "api_mercado_customer_service_aggregate"),
])
def test_aggregate_rejects_invalid_dates_before_starting_store_requests(route, body, name):
    calls = []
    funcs = _console_functions(lambda *args: calls.append(args))
    app = Flask(__name__)
    app.add_url_rule(route, view_func=funcs[name], methods=["POST"])
    response = app.test_client().post(route, json=body)
    assert response.status_code == 400
    assert not calls


def test_pre_sale_aggregate_forwards_dates_and_returns_period_count():
    def execute(token_id, action, payload):
        assert "date_from" not in payload
        return {"total": 100, "questions": [
            {"id": 1, "date_created": "2026-10-06T00:00:00Z"},
            {"id": 2, "date_created": "2026-09-28T00:00:00Z"},
        ]}

    funcs = _console_functions(execute)
    app = Flask(__name__)
    app.add_url_rule("/pre", view_func=funcs["api_mercado_pre_sale_aggregate"], methods=["POST"])
    response = app.test_client().post("/pre", json={"token_ids": [7], **WEEK})
    assert response.status_code == 200
    result = response.get_json()["data"]["stores"][0]
    assert result["official_total"] == 1
    assert result["truncated"] is False
    assert [row["id"] for row in result["questions"]] == [1]


def test_post_sale_aggregate_applies_requested_dates_to_search_results():
    def execute(token_id, action, payload):
        return {"paging": {"total": 2}, "messages": [
            {"id": "old", "message_date": {"created": "2026-09-28T00:00:00Z"}},
            {"id": "recent", "message_date": {"created": "2026-09-29T16:00:00Z"}},
        ]}

    funcs = _console_functions(execute)
    app = Flask(__name__)
    app.add_url_rule("/post", view_func=funcs["api_mercado_customer_service_aggregate"], methods=["POST"])
    response = app.test_client().post("/post", json={
        "token_ids": [7], "mode": "post-sale", "search": "100", "filters": WEEK,
    })
    assert response.status_code == 200
    result = response.get_json()["data"]["stores"][0]
    assert result["date_match"] is True
    assert result["search_bundle"]["paging"]["total"] == 1
    assert [row["id"] for row in result["search_bundle"]["messages"]] == ["recent"]


def _capturing_claims_client(monkeypatch):
    client = communications.MercadoCommunicationsClient("test")
    calls = []

    def request_claims(method, path, **kwargs):
        calls.append((method, path, kwargs["params"]))
        return {"paging": {"total": 0}, "data": []}

    monkeypatch.setattr(client, "request", request_claims)
    return client, calls


def test_claims_client_keeps_existing_utc_calendar_default(monkeypatch):
    client, calls = _capturing_claims_client(monkeypatch)
    client.search_claims("123", **WEEK)
    assert calls[0][2]["range"] == (
        "date_created:after:2026-09-30T00:00:00.000+00:00,"
        "before:2026-10-06T23:59:59.999+00:00"
    )


def test_claims_client_converts_beijing_calendar_boundaries_to_utc(monkeypatch):
    client, calls = _capturing_claims_client(monkeypatch)
    client.search_claims("123", **WEEK, calendar_timezone=timezone(timedelta(hours=8)))
    assert calls[0][2]["range"] == (
        "date_created:after:2026-09-29T16:00:00.000+00:00,"
        "before:2026-10-06T15:59:59.999+00:00"
    )


def test_workbench_claims_use_same_beijing_period_as_questions_and_messages(monkeypatch):
    client, calls = _capturing_claims_client(monkeypatch)
    monkeypatch.setattr(communications, "_store_client", lambda *args, **kwargs: (
        client, {"meli_user_id": "123", "site_id": "MLM"}
    ))
    communications.execute_store_communication(
        7, "claims-list", WEEK,
        get_token=lambda _: {}, refresh_token=lambda _: None,
    )
    params = calls[0][2]
    assert "calendar_timezone" not in params
    lower, upper = params["range"].removeprefix("date_created:").split(",")
    range_start = datetime.fromisoformat(lower.removeprefix("after:")).timestamp()
    range_end = datetime.fromisoformat(upper.removeprefix("before:")).timestamp()
    beijing_start, beijing_end = communications.communication_date_bounds(WEEK)
    assert range_start == beijing_start
    assert range_end == pytest.approx(beijing_end - 0.001, rel=0, abs=0.000001)


def test_all_after_sales_includes_read_replied_and_old_order_packs_with_recent_messages():
    calls = []
    def execute(token_id, action, payload):
        if action == 'post-sale-unread':
            return {'results': [{'resource': '/packs/unread', 'count': 1}]}
        pack = payload['pack_id']
        calls.append(pack)
        messages = [] if pack == 'empty' else [{
            'id': pack, 'from': {'user_id': 'seller' if pack == 'replied' else 'buyer'},
            'message_date': {'created': '2026-09-28T00:00:00Z' if pack == 'old-message' else '2026-10-06T00:00:00Z',
                             'read': None if pack == 'unread' else '2026-10-06T01:00:00Z'},
        }]
        return {'seller_id': 'seller', 'messages': messages, 'paging': {'total': len(messages)}}
    funcs = _console_functions(execute)
    funcs['bit_db_api'].list_mercado_message_pack_candidates = lambda token: [
        'read', 'replied', 'old-order', 'old-message', 'empty', 'unread',
    ]
    result = funcs['_query_customer_service_post_sale_store'](7, filters=WEEK)
    assert {row['pack_id'] for row in result['conversations']} == {'read', 'replied', 'old-order', 'unread'}
    assert calls.count('unread') == 1
    assert all(row['unread_count'] == (1 if row['pack_id'] == 'unread' else 0)
               for row in result['conversations'])


def test_background_aggregate_checks_access_before_starting_any_work():
    funcs = _console_functions(lambda *args: pytest.fail('Unauthorized read'))
    funcs['_authorized_token_ids_for_user'] = lambda: {7}
    app = Flask(__name__)
    app.add_url_rule('/post', view_func=funcs['api_mercado_customer_service_aggregate'], methods=['POST'])
    assert app.test_client().post('/post', json={
        'token_ids': [8], 'mode': 'post-sale', 'background': True,
    }).status_code == 403


@pytest.mark.parametrize("mode", ["post-sale", "claims"])
def test_background_route_returns_pending_then_cached_rows_with_update_time(monkeypatch, mode):
    from bit.communication_preload import customer_service_preload, customer_service_claims_preload
    selected = customer_service_claims_preload if mode == "claims" else customer_service_preload
    other = customer_service_preload if mode == "claims" else customer_service_claims_preload
    monkeypatch.setattr(other, "read", lambda *args, **kwargs: pytest.fail("Wrong communication queue"))
    calls = []
    def read(key, query, *, refresh=False):
        calls.append((key, refresh))
        if len(calls) == 1:
            return {'pending': True}
        return {'data': {'token_id': 7, 'conversations': [{'pack_id': 'read'}]},
                'updated_at': '2026-10-06T01:00:00Z'}
    monkeypatch.setattr(selected, 'read', read)
    funcs = _console_functions(lambda *args: pytest.fail('Polling must not run inline'))
    app = Flask(__name__)
    app.add_url_rule('/post', view_func=funcs['api_mercado_customer_service_aggregate'], methods=['POST'])
    payload = {'token_ids': [7], 'mode': mode, 'background': True, 'filters': WEEK}
    first = app.test_client().post('/post', json=payload)
    assert first.get_json()['data']['pending_token_ids'] == [7]
    second = app.test_client().post('/post', json=payload)
    data = second.get_json()['data']
    assert data['pending_token_ids'] == []
    assert data['stores'][0]['conversations'] == [{'pack_id': 'read'}]
    assert data['stores'][0]['updated_at'] == '2026-10-06T01:00:00Z'
    assert first.headers['Cache-Control'] == second.headers['Cache-Control'] == 'no-store'
    assert calls[0] == calls[1]
