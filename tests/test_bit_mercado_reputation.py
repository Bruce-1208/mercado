import re
import json
from datetime import datetime, timezone
from pathlib import Path

from bit import bit_db_api
from bit import bit_interface
from bit import bit_reputation_info
from bit import bit_summary_info
from bit_playwright import bit_summary_info as playwright_summary_info
from bit.mercado_reputation import (
    ORDERS_SEARCH_PATH,
    REPUTATION_PATH,
    RIGHTS_HOLDER_CASES_PATH,
    SEVEN_DAY_RATE_DISCLAIMER,
    enrich_reputation_with_official_data,
    fetch_reputation_payload,
    fetch_store_reputation,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.responses.pop(0)


def reputation_payload():
    return {
        "user_id": 1234,
        "site_id": "CBT",
        "seller_reputation": [
            {
                "user_id": 5678,
                "site_id": "MLM",
                "logistic_type": "remote",
                "seller_reputation": {
                    "level_id": "5_green",
                    "power_seller_status": "gold",
                    "transactions": {
                        "canceled": 81,
                        "completed": 601,
                        "period": "historic",
                        "ratings": {
                            "negative": 0.04,
                            "neutral": 0.02,
                            "positive": 0.94,
                        },
                        "total": 682,
                    },
                    "metrics": {
                        "sales": {"period": "60 days", "completed": 219},
                        "claims": {"period": "60 days", "rate": 0.0166, "value": 4},
                        "delayed_handling_time": {
                            "period": "60 days",
                            "rate": 0.0877,
                            "value": 20,
                        },
                        "cancellations": {"period": "60 days", "rate": 0, "value": 1},
                    },
                },
            }
        ],
    }


def test_official_reputation_endpoint_and_response_are_normalized():
    http = FakeSession([FakeResponse(reputation_payload())])

    result = fetch_store_reputation(
        7,
        get_token=lambda _token_id: {
            "display_name": "测试店铺",
            "nickname": "SELLER_TEST",
            "access_token": "access-secret",
        },
        http=http,
    )

    assert http.gets[0][0].endswith(REPUTATION_PATH)
    assert http.gets[0][1]["headers"]["Authorization"] == "Bearer access-secret"
    assert result["display_name"] == "测试店铺"
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["site_name"] == "墨西哥"
    assert row["level_name"] == "绿色"
    assert row["claims_rate_percent"] == 1.66
    assert row["delayed_handling_rate_percent"] == 8.77
    assert row["cancellations_rate_percent"] == 0
    assert row["rating_positive_percent"] == 94
    assert "access_token" not in result
    assert "refresh_token" not in result


def test_unauthorized_response_refreshes_token_once_and_retries():
    http = FakeSession(
        [
            FakeResponse({"message": "invalid token"}, status_code=401),
            FakeResponse(reputation_payload()),
        ]
    )
    token = {
        "display_name": "刷新店铺",
        "access_token": "expired-access",
    }
    refreshes = []

    def refresh(token_id):
        refreshes.append(token_id)
        token["access_token"] = "fresh-access"

    result = fetch_store_reputation(
        9,
        get_token=lambda _token_id: dict(token),
        refresh_token=refresh,
        http=http,
    )

    assert result["total"] == 1
    assert refreshes == [9]
    assert [call[1]["headers"]["Authorization"] for call in http.gets] == [
        "Bearer expired-access",
        "Bearer fresh-access",
    ]


def test_payload_request_uses_direct_client_contract():
    http = FakeSession([FakeResponse(reputation_payload())])

    payload = fetch_reputation_payload("token-value", http=http, timeout=17)

    assert payload["user_id"] == 1234
    assert http.gets[0][1]["timeout"] == 17
    assert http.gets[0][1]["headers"]["Accept"] == "application/json"


def test_official_supplement_uses_orders_status_infractions_and_rights_holder_apis():
    http = FakeSession([
        FakeResponse({
            "cases": [
                {"case_id": "case-1", "item_id": "MLM123"},
                {"case_id": "case-2", "item_id": "MLB456"},
            ],
            "paging": {"total": 2, "limit": 50},
        }),
        FakeResponse({
            "site_status": "active",
            "status": {"sell": {"allow": True}, "list": {"allow": True}},
        }),
        FakeResponse({"paging": {"total": 10}}),
        FakeResponse({"paging": {"total": 15}}),
        FakeResponse({"paging": {"total": 3}}),
    ])
    rows = [{"user_id": "seller-mlm", "site_id": "MLM"}]

    enrich_reputation_with_official_data(
        rows,
        "official-token",
        now=datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc),
        http=http,
    )

    row = rows[0]
    assert row["site_status_display"] == "正常"
    assert row["orders_previous_7d"] == 10
    assert row["orders_current_7d"] == 15
    assert row["direction"] == "增长"
    assert row["gradient_rate"] == "50%"
    assert row["gradient_source"] == "official_orders_api"
    assert row["gradient_disclaimer"] == SEVEN_DAY_RATE_DISCLAIMER
    assert row["infraction_count"] == 3
    assert row["rights_holder_count"] == 1
    assert row["infraction_recent_days"] == 100
    requested_paths = [call[0] for call in http.gets]
    assert requested_paths[0].endswith(RIGHTS_HOLDER_CASES_PATH)
    assert requested_paths[1].endswith("/users/seller-mlm")
    assert requested_paths[2].endswith(ORDERS_SEARCH_PATH)
    assert requested_paths[3].endswith(ORDERS_SEARCH_PATH)
    assert requested_paths[4].endswith(
        "/marketplace/moderations/infractions/seller-mlm"
    )
    assert http.gets[2][1]["params"]["seller"] == "seller-mlm"
    assert http.gets[4][1]["params"]["date_created_since"] == "2026-05-27"


def test_official_gradient_is_not_overwritten_by_browser_auxiliary():
    api_rows = [{
        "store_name": "官方店铺",
        "site_id": "MLM",
        "direction": "增长",
        "gradient_rate": "25%",
        "gradient_source": "official_orders_api",
    }]
    database_rows = [["官方店铺", "墨西哥", "绿色", 1, "0%", "0%", "0%", "增长", "25%", "正常", "", "[]"]]
    auxiliary_rows = [{
        "store_name": "官方店铺",
        "site": "墨西哥",
        "direction": "下滑",
        "gradient_rate": "-99%",
        "system_warning": "正常",
        "updated_at": "2026-09-04 00:00:00",
        "visits": "[1,2,3]",
    }]

    bit_reputation_info._merge_api_auxiliary_rows(
        api_rows, database_rows, auxiliary_rows
    )

    assert api_rows[0]["direction"] == "增长"
    assert api_rows[0]["gradient_rate"] == "25%"
    assert database_rows[0][7:9] == ["增长", "25%"]
    assert database_rows[0][11] == "[1,2,3]"


def test_reputation_counts_follow_latest_api_infraction_dashboard(monkeypatch):
    monkeypatch.setattr(
        bit_reputation_info,
        "current_infraction_counts_by_token_site",
        lambda days: {
            "days": days,
            "last_synced_at": "2026-09-04 08:00:00",
            "counts": {
                (7, "MLM"): {
                    "infraction_count": 4,
                    "rights_holder_count": 2,
                },
            },
        },
    )
    rows = [{
        "token_id": 7,
        "site_id": "MLM",
        "infraction_count": 99,
        "rights_holder_count": 88,
    }]

    bit_reputation_info._attach_latest_api_infraction_counts(rows, 100)

    assert rows[0]["infraction_count"] == 4
    assert rows[0]["rights_holder_count"] == 2
    assert rows[0]["infraction_source"] == "official_infraction_dashboard"
    assert rows[0]["infraction_last_synced_at"] == "2026-09-04 08:00:00"


def test_latest_reputation_rows_include_salesperson_and_account_group(monkeypatch):
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {"rows": [{
            "id": 7,
            "display_name": "分组店铺",
            "nickname": "GROUPED",
            "site_settings": [{
                "site_id": "MLM",
                "salesperson": "张三",
                "group_name": "精品组",
            }],
        }]},
    )
    data = {"rows": [{"店铺名": "分组店铺", "站点": "墨西哥"}]}

    bit_interface._attach_reputation_token_ids(data)

    assert data["rows"][0]["token_id"] == 7
    assert data["rows"][0]["业务员"] == "张三"
    assert data["rows"][0]["账户组"] == "精品组"


def test_remote_database_client_calls_server_side_reputation_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, "DB_MODE", "api")
    monkeypatch.setattr(
        bit_db_api,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {"rows": []},
    )

    result = bit_db_api.get_mercado_store_reputation(12)

    assert result == {"rows": []}
    assert calls == [
        ("GET", "/api/db/mercado-tokens/12/reputation", {"timeout": 60})
    ]


def test_console_template_keeps_old_reputation_and_adds_api_panel():
    template = (
        Path(__file__).resolve().parents[1] / "bit" / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="tab-reputation"' in template
    assert 'id="tab-api-reputation"' in template
    assert 'id="api-reputation-fetch"' in template
    assert 'id="api-reputation-log"' in template
    assert 'id="api-reputation-status-text"' in template
    assert "/api/mercado-reputation/refresh" in template
    assert "/api/mercado-reputation/status" in template
    assert 'id="api-reputation-store"' not in template
    assert "近100天侵权" in template
    assert "近100天权利人" in template
    assert "row.infraction_count" in template
    assert "row.rights_holder_count" in template
    assert 'row["侵权数量"]' in template
    assert 'row["权利人数量"]' in template
    reputation_table = re.search(
        r'<table class="reputation-table reputation-data-table">(.*?)</table>',
        template,
        re.DOTALL,
    )
    api_reputation_table = re.search(
        r'<table class="reputation-table api-reputation-data-table">(.*?)</table>',
        template,
        re.DOTALL,
    )
    plain_reputation_tables = re.findall(
        r'<table class="reputation-table">(.*?)</table>',
        template,
        re.DOTALL,
    )
    assert reputation_table is not None
    assert 'id="reputation-body"' in reputation_table.group(1)
    assert api_reputation_table is not None
    assert 'id="api-reputation-body"' in api_reputation_table.group(1)
    assert len(re.findall(r'data-sort-key="[^"]+"', api_reputation_table.group(1))) == 13
    assert len(re.findall(r'data-filter-key="[^"]+"', api_reputation_table.group(1))) == 13
    assert "function filterApiReputationColumn(key, value)" in template
    assert "function sortApiReputationBy(key)" in template
    assert "function resetApiReputationView()" in template
    assert "zeshun-api-reputation-view-v1" in template
    assert 'tabName === "api-reputation" && !apiReputationLoaded' in template
    assert "正在读取上一次 API 声誉数据" in template
    assert any(
        'id="infraction-body"' in table for table in plain_reputation_tables
    )
    assert ".reputation-data-table td:nth-child(12) { width: 360px; }" in template
    assert "Math.round(canvas.getBoundingClientRect().width || 0)" in template
    assert ".reputation-data-table td:nth-child(10) { width: 72px; }" in template
    assert ".reputation-data-table td:nth-child(11) { width: 84px; }" in template
    assert "function conciseAccountRiskWarningText(value)" in template
    assert "const text = conciseAccountRiskWarningText(value);" in template
    assert 'id="reputation-warning-popover"' in template
    assert "function showReputationWarningDetail(cell, event)" in template
    assert "reputation-warning-preview" in template
    assert "七天变化率由官方订单 API 自算" in template
    assert "每天 00:00（24 点）和 12:00 自动刷新，并发 10" in template
    assert "下次自动刷新 ${data.next_auto_refresh_at}" in template
    assert "站点状态" in reputation_table.group(1)
    assert "openReputationBrowser(this)" in template
    assert "/api/reputation/${tokenId}/open-browser" in template
    assert 'id="reputation-salesperson-filter"' in template
    assert 'id="reputation-group-filter"' in template
    assert 'id="reputation-name-search"' in template
    assert "function applyReputationFilters()" in template
    assert "暂无符合业务员、账号组和名字筛选条件的声誉数据" in template
    assert 'data-field="reputation_update_enabled"' in template
    assert 'data-field="bulk_reputation_update_enabled"' in template


def test_console_reputation_route_returns_normalized_data_without_token(monkeypatch):
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "get_mercado_store_reputation",
        lambda token_id: {
            "token_id": token_id,
            "display_name": "控制台店铺",
            "rows": [{"site_id": "MLM", "level_id": "5_green"}],
        },
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 1,
            "username": "tester",
            "display_name": "测试员",
        }

    response = client.get("/api/mercado-tokens/22/reputation")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["data"]["token_id"] == 22
    assert "access_token" not in response.get_data(as_text=True)


def test_reputation_button_opens_matching_bitbrowser_on_reputation_page(monkeypatch):
    opened = []
    released = []
    debugger_requests = []

    class DebuggerResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return json.dumps({
                "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"
            }).encode("utf-8")

    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {"rows": [{
            "id": 22,
            "display_name": "控制台店铺",
            "nickname": "SELLER_22",
        }]},
    )
    monkeypatch.setattr(
        bit_interface,
        "list_shop_configs",
        lambda include_ignored=True: [{
            "shop_name": "控制台店铺",
            "window_id": "window-22",
        }],
    )
    monkeypatch.setattr(
        bit_interface,
        "openBrowser",
        lambda window_id, **kwargs: opened.append((window_id, kwargs)) or {
            "success": True,
            "data": {"http": "127.0.0.1:9222"},
        },
    )
    monkeypatch.setattr(
        bit_interface,
        "releaseBrowserLease",
        lambda window_id: released.append(window_id),
    )

    def open_debugger(request, timeout):
        debugger_requests.append((request, timeout))
        return DebuggerResponse()

    monkeypatch.setattr(bit_interface, "urlopen", open_debugger)
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 1,
            "username": "tester",
            "display_name": "测试员",
        }

    response = client.post(
        "/api/reputation/22/open-browser",
        json={"shop_name": "控制台店铺"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["window_id"] == "window-22"
    assert payload["data"]["target_url"] == bit_reputation_info.REPUTATION_URL
    assert opened == [(
        "window-22",
        {"api_lock_timeout": 5, "request_timeout": 20},
    )]
    assert released == ["window-22"]
    request, timeout = debugger_requests[0]
    assert request.get_method() == "PUT"
    assert timeout == 10
    assert request.full_url.startswith("http://127.0.0.1:9222/json/new?")
    assert "global-selling.mercadolibre.com%2Freputation" in request.full_url


def test_full_refresh_keeps_successes_and_logs_failed_stores(monkeypatch):
    collection_options = {}

    def collect(**kwargs):
        collection_options.update(kwargs)
        kwargs["log_callback"]("成功店铺：成功，返回 1 个站点")
        kwargs["log_callback"]("失败店铺：失败，授权已失效")
        kwargs["progress_callback"]({"event": "initialized", "total_stores": 2})
        kwargs["progress_callback"]({
            "event": "store_success",
            "token_id": 1,
            "store_name": "成功店铺",
            "rows": [
                {
                    "token_id": 1,
                    "store_name": "成功店铺",
                    "site_id": "MLM",
                    "site_name": "墨西哥",
                }
            ],
        })
        kwargs["progress_callback"]({
            "event": "store_failure",
            "token_id": 2,
            "store_name": "失败店铺",
            "error": "授权已失效",
        })
        return {
            "api_rows": [
                {
                    "token_id": 1,
                    "store_name": "成功店铺",
                    "site_id": "MLM",
                    "site_name": "墨西哥",
                }
            ],
            "failures": [
                {
                    "token_id": 2,
                    "store_name": "失败店铺",
                    "error": "授权已失效",
                }
            ],
            "total_stores": 2,
            "completed_stores": 2,
            "success_stores": 1,
            "failed_stores": 1,
            "total_sites": 1,
        }

    monkeypatch.setattr(bit_interface.bit_reputation_info, "main", collect)
    monkeypatch.setattr(bit_interface, "_persist_api_reputation_snapshot", lambda: True)
    with bit_interface._api_reputation_lock:
        bit_interface._api_reputation_logs.clear()
        bit_interface._api_reputation_state.update({
            "running": True,
            "status": "running",
            "message": "测试中",
            "started_at": "2026-08-27 20:00:00",
            "finished_at": "",
            "elapsed_seconds": 0,
            "total_stores": 0,
            "completed_stores": 0,
            "success_stores": 0,
            "failed_stores": 0,
            "total_sites": 0,
            "rows": [],
            "failures": [],
        })

    bit_interface._run_all_api_reputation_refresh()
    result = bit_interface._api_reputation_snapshot()

    assert result["status"] == "partial"
    assert result["total_stores"] == 2
    assert result["completed_stores"] == 2
    assert result["success_stores"] == 1
    assert result["failed_stores"] == 1
    assert result["total_sites"] == 1
    assert result["rows"][0]["store_name"] == "成功店铺"
    assert result["failures"][0]["store_name"] == "失败店铺"
    assert any("成功店铺：成功" in line for line in result["logs"])
    assert any("失败店铺：失败" in line for line in result["logs"])
    assert result["elapsed_seconds"] >= 0
    assert collection_options["max_workers"] == 10
    assert collection_options["collect_browser_auxiliary"] is True


def test_api_reputation_auto_refresh_boundaries_are_noon_and_midnight():
    assert bit_interface._next_api_reputation_run(
        datetime(2026, 9, 7, 11, 59, 59)
    ) == datetime(2026, 9, 7, 12, 0, 0)
    assert bit_interface._next_api_reputation_run(
        datetime(2026, 9, 7, 12, 0, 0)
    ) == datetime(2026, 9, 8, 0, 0, 0)
    assert bit_interface._next_api_reputation_run(
        datetime(2026, 9, 7, 23, 59, 59)
    ) == datetime(2026, 9, 8, 0, 0, 0)


def test_api_reputation_last_snapshot_survives_service_restart(tmp_path):
    state_path = tmp_path / "api-reputation.json"
    with bit_interface._api_reputation_lock:
        previous_state = dict(bit_interface._api_reputation_state)
        previous_logs = list(bit_interface._api_reputation_logs)
        bit_interface._api_reputation_state.update({
            "running": False,
            "status": "partial",
            "message": "全量更新完成：成功 1 家，失败 1 家",
            "started_at": "2026-08-31 20:00:00",
            "finished_at": "2026-08-31 20:00:08",
            "elapsed_seconds": 8,
            "total_stores": 2,
            "completed_stores": 2,
            "success_stores": 1,
            "failed_stores": 1,
            "total_sites": 1,
            "rows": [{"store_name": "上次店铺", "site_id": "MLM"}],
            "failures": [{"store_name": "失败店铺", "error": "超时"}],
        })
        bit_interface._api_reputation_logs.clear()
        bit_interface._api_reputation_logs.append("[20:00:08] 上次任务完成")

    try:
        assert bit_interface._persist_api_reputation_snapshot(state_path) is True
        loaded = bit_interface._load_api_reputation_snapshot(state_path)
    finally:
        with bit_interface._api_reputation_lock:
            bit_interface._api_reputation_state.clear()
            bit_interface._api_reputation_state.update(previous_state)
            bit_interface._api_reputation_logs.clear()
            bit_interface._api_reputation_logs.extend(previous_logs)

    assert loaded["running"] is False
    assert loaded["status"] == "partial"
    assert loaded["finished_at"] == "2026-08-31 20:00:08"
    assert loaded["rows"] == [{"store_name": "上次店铺", "site_id": "MLM"}]
    assert loaded["logs"] == ["[20:00:08] 上次任务完成"]


def test_api_reputation_uses_latest_database_rows_before_first_manual_refresh(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_get_latest_reputation_info",
        lambda: {
            "latest_submit_time": "2026-08-31 21:30:00",
            "rows": [
                {
                    "店铺名": "默认店铺",
                    "站点": "墨西哥",
                    "声誉颜色": "绿色",
                    "总单量": 321,
                    "投诉率": "1.25%",
                    "延误率": "2.5%",
                    "取消率": "0%",
                    "侵权数量": 3,
                    "权利人数量": 2,
                }
            ],
        },
    )
    with bit_interface._api_reputation_lock:
        previous_state = dict(bit_interface._api_reputation_state)
        previous_attempted = bit_interface._api_reputation_database_hydration_attempted
        bit_interface._api_reputation_state.clear()
        bit_interface._api_reputation_state.update(bit_interface._api_reputation_default_state())
        bit_interface._api_reputation_database_hydration_attempted = False

    try:
        assert bit_interface._hydrate_api_reputation_from_database() is True
        loaded = bit_interface._api_reputation_snapshot()
    finally:
        with bit_interface._api_reputation_lock:
            bit_interface._api_reputation_state.clear()
            bit_interface._api_reputation_state.update(previous_state)
            bit_interface._api_reputation_database_hydration_attempted = previous_attempted

    assert loaded["message"] == "已展示上一次入库的 API 声誉数据"
    assert loaded["finished_at"] == "2026-08-31 21:30:00"
    assert loaded["rows"][0] == {
        "store_name": "默认店铺",
        "site_name": "墨西哥",
        "level_name": "绿色",
        "sales_completed": 321,
        "claims_rate_percent": 1.25,
        "delayed_handling_rate_percent": 2.5,
        "cancellations_rate_percent": 0.0,
        "infraction_count": 3,
        "rights_holder_count": 2,
    }


def test_default_reputation_collection_uses_api_and_writes_legacy_table(monkeypatch):
    database_calls = []
    task_calls = []
    api_calls = []
    logs = []

    monkeypatch.setattr(
        bit_reputation_info,
        "list_mercado_store_tokens",
        lambda: {
                "rows": [
                    {
                        "id": 11,
                        "display_name": "泽顺店铺",
                        "nickname": "SELLER_A",
                        "site_settings": [
                            {
                                "site_id": "MLM",
                                "reputation_update_enabled": True,
                                "visit_stats_enabled": True,
                            },
                        ],
                    },
                ]
        },
    )

    def fetch(token_id):
        api_calls.append(token_id)
        return {
            "rows": [
                {
                    "site_id": "MLM",
                    "site_name": "墨西哥",
                    "logistic_type": "remote",
                    "level_id": "5_green",
                    "level_name": "绿色",
                    "transaction_total": 682,
                    "sales_completed": 219,
                    "claims_rate_percent": 1.66,
                    "delayed_handling_rate_percent": 8.77,
                    "cancellations_rate_percent": 0,
                }
            ]
        }

    monkeypatch.setattr(bit_reputation_info, "get_mercado_store_reputation", fetch)
    monkeypatch.setattr(
        bit_reputation_info,
        "get_latest_infraction_info",
        lambda _days: {
            "recent_days": 100,
            "summary": [
                {
                    "店铺名": "泽顺店铺",
                    "站点": "墨西哥",
                    "侵权": 3,
                    "权利人": 2,
                }
            ],
        },
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_execute_reputation_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("默认流程不应启动浏览器声誉采集")
        ),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "inset_reputation_info",
        lambda rows, **kwargs: database_calls.append((rows, kwargs)),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "insert_task_record",
        lambda rows: task_calls.append(rows),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "write_unreadable_site_report",
        lambda *_args, **_kwargs: None,
    )

    result = bit_reputation_info.main(
        max_workers=4,
        retry_failed=False,
        export_excel=False,
        send_email=False,
        collect_browser_auxiliary=False,
        log_callback=logs.append,
    )

    assert api_calls == [11]
    assert result["total_stores"] == 1
    assert result["success_stores"] == 1
    assert result["failed_stores"] == 0
    assert result["total_sites"] == 1
    assert result["api_rows"][0]["store_name"] == "泽顺店铺"
    assert result["api_rows"][0]["infraction_count"] == 3
    assert result["api_rows"][0]["rights_holder_count"] == 2
    assert result["api_rows"][0]["infraction_recent_days"] == 100
    assert database_calls[0][1] == {}
    legacy_row = next(
        row for row in database_calls[0][0] if row[1] == "墨西哥"
    )
    assert legacy_row[:7] == [
        "泽顺店铺",
        "墨西哥",
        "绿色",
        219,
        "1.66%",
        "8.77%",
        "0%",
    ]
    assert legacy_row[9] == "正常"
    assert legacy_row[11] == "[]"
    assert task_calls[0][0][0:4] == (
        "获取声誉信息",
        "泽顺店铺",
        "墨西哥",
        "成功",
    )
    assert any("声誉混合更新完成" in line for line in logs)


def test_selected_api_reputation_update_merges_only_returned_site(monkeypatch):
    database_calls = []
    monkeypatch.setattr(
        bit_reputation_info,
        "list_mercado_store_tokens",
        lambda: {
                "rows": [
                    {
                        "id": 21,
                        "display_name": "选定店铺",
                        "nickname": "SELECTED",
                        "site_settings": [
                            {"site_id": "MLM", "reputation_update_enabled": True, "visit_stats_enabled": False},
                            {"site_id": "MLB", "reputation_update_enabled": True, "visit_stats_enabled": True},
                        ],
                    },
                    {
                        "id": 22,
                        "display_name": "其他店铺",
                        "nickname": "OTHER",
                        "site_settings": [
                            {"site_id": "MLB", "reputation_update_enabled": True, "visit_stats_enabled": True},
                        ],
                    },
                ]
        },
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "get_mercado_store_reputation",
        lambda token_id: {
            "rows": [
                {
                    "site_id": "MLM",
                    "site_name": "墨西哥",
                    "level_name": "绿色",
                    "sales_completed": 10,
                },
                {
                    "site_id": "MLB",
                    "site_name": "巴西",
                    "level_name": "黄色",
                    "sales_completed": 20,
                },
            ]
        },
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "get_latest_infraction_info",
        lambda _days: {"recent_days": 100, "summary": []},
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "inset_reputation_info",
        lambda rows, **kwargs: database_calls.append((rows, kwargs)),
    )
    monkeypatch.setattr(bit_reputation_info, "insert_task_record", lambda _rows: None)
    monkeypatch.setattr(
        bit_reputation_info,
        "write_unreadable_site_report",
        lambda *_args, **_kwargs: None,
    )

    result = bit_reputation_info.get_reputation_info_all(
        selected_shops=["SELECTED"],
        selected_sites=["巴西"],
        retry_failed=False,
        export_excel=False,
        send_email=False,
        collect_browser_auxiliary=False,
    )

    assert result["total_stores"] == 1
    assert [row["site_id"] for row in result["api_rows"]] == ["MLB"]
    rows, kwargs = database_calls[0]
    assert [(row[0], row[1]) for row in rows] == [("选定店铺", "巴西")]
    assert kwargs == {
        "merge_latest": True,
        "replace_targets": [("选定店铺", "巴西")],
    }


def test_reputation_scope_requires_explicit_visit_stats_switch():
    assert bit_reputation_info._token_enabled_site_codes(
        {"display_name": "无站点配置"},
        "visit_stats_enabled",
    ) == set()
    assert bit_reputation_info._token_enabled_site_codes(
        {
            "site_settings": [
                {"site_id": "MLM"},
                {"site_id": "MLB", "visit_stats_enabled": False},
                {"site_id": "MLC", "visit_stats_enabled": "true"},
            ]
        },
        "visit_stats_enabled",
    ) == {"MLC"}


def test_reputation_scope_requires_explicit_reputation_update_switch():
    assert bit_reputation_info._token_enabled_site_codes(
        {
            "site_settings": [
                {"site_id": "MLM", "visit_stats_enabled": True},
                {"site_id": "MLB", "reputation_update_enabled": False},
                {"site_id": "MLC", "reputation_update_enabled": "true"},
            ]
        },
        "reputation_update_enabled",
    ) == {"MLC"}


def test_system_warning_is_derived_from_official_api_status_and_errors():
    assert bit_reputation_info._official_api_system_warning({
        "site_status_display": "暂停销售",
        "official_api_errors": ["七天变化率：接口限频"],
    }) == "站点状态：暂停销售；七天变化率：接口限频"
    assert bit_reputation_info._official_api_system_warning({
        "site_status_display": "正常",
    }) == "正常"


def test_collection_options_keep_reputation_and_traffic_switches_independent(monkeypatch):
    monkeypatch.setattr(
        bit_interface.bit_db_api,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": 41,
                    "display_name": "仅声誉店铺",
                    "site_settings": [
                        {
                            "site_id": "MLM",
                            "reputation_update_enabled": True,
                            "visit_stats_enabled": False,
                        }
                    ],
                },
                {
                    "id": 42,
                    "display_name": "仅流量店铺",
                    "site_settings": [
                        {
                            "site_id": "MLB",
                            "reputation_update_enabled": False,
                            "visit_stats_enabled": True,
                        }
                    ],
                },
            ]
        },
    )

    options = bit_interface._collection_config_options()

    assert [row["shop_name"] for row in options["shops"]] == ["仅声誉店铺"]
    assert [row["shop_name"] for row in options["infraction_shops"]] == ["仅流量店铺"]
    parsed = bit_interface._parse_collection_request(
        {"shops": ["仅声誉店铺"], "sites": ["墨西哥"]},
        authorization_flag="reputation_update_enabled",
    )
    assert parsed["selected_shops"] == ("仅声誉店铺",)
    assert parsed["selected_sites"] == ("墨西哥",)


def test_hybrid_collection_merges_browser_traffic_without_reputation_page(monkeypatch):
    database_calls = []
    captured_browser_rows = []
    monkeypatch.setattr(
        bit_reputation_info,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": 31,
                    "display_name": "混合店铺",
                    "nickname": "HYBRID",
                    "site_settings": [
                        {
                            "site_id": "MLM",
                            "reputation_update_enabled": True,
                            "visit_stats_enabled": True,
                        },
                        {
                            "site_id": "MLB",
                            "reputation_update_enabled": True,
                            "visit_stats_enabled": False,
                        },
                    ],
                },
            ]
        },
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "get_mercado_store_reputation",
        lambda _token_id: {
            "rows": [
                {
                    "site_id": "MLM",
                    "site_name": "墨西哥",
                    "level_name": "绿色",
                    "sales_completed": 88,
                    "claims_rate_percent": 1.2,
                    "delayed_handling_rate_percent": 2.3,
                    "cancellations_rate_percent": 0.4,
                    "direction": "增长",
                    "gradient_rate": "12%",
                    "gradient_source": "official_orders_api",
                    "site_status_display": "正常",
                },
                {
                    "site_id": "MLB",
                    "site_name": "巴西",
                    "level_name": "黄色",
                    "sales_completed": 99,
                },
            ]
        },
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "get_latest_infraction_info",
        lambda _days: {
            "recent_days": 100,
            "summary": [
                {
                    "店铺名": "混合店铺",
                    "站点": "墨西哥",
                    "侵权": 7,
                    "权利人": 4,
                }
            ],
        },
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "list_config_rows",
        lambda **_kwargs: [
            # 浏览器配置只负责提供窗口，不再决定 API 声誉运行站点。
            ("window-31", "HYBRID", "", "墨西哥", "", "", ""),
        ],
    )

    def collect_auxiliary(rows, **_kwargs):
        captured_browser_rows.extend(rows)
        row = rows[0]
        return {
            bit_reputation_info.row_key(row): (
                row,
                [
                        {
                            "store_name": "混合店铺",
                            "site": "墨西哥",
                            "direction": "浏览器方向不应使用",
                            "gradient_rate": "99%",
                            "system_warning": "浏览器告警不应使用",
                        "updated_at": "2026-08-27 23:10:00",
                        "visits": "[11, 22, 33]",
                        "error": "",
                    }
                ],
                [
                    (
                        "获取声誉辅助信息",
                        "混合店铺",
                        "墨西哥",
                        "成功",
                        "2026-08-27 23:10:00",
                    )
                ],
            )
        }

    monkeypatch.setattr(
        bit_reputation_info,
        "_execute_reputation_auxiliary_rows",
        collect_auxiliary,
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_open_reputation_page_with_validation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("混合更新不得打开 reputation 页面")
        ),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "inset_reputation_info",
        lambda rows, **kwargs: database_calls.append((rows, kwargs)),
    )
    monkeypatch.setattr(bit_reputation_info, "insert_task_record", lambda _rows: None)
    monkeypatch.setattr(
        bit_reputation_info,
        "write_unreadable_site_report",
        lambda *_args, **_kwargs: None,
    )

    result = bit_reputation_info.main(
        retry_failed=False,
        export_excel=False,
        send_email=False,
    )

    assert captured_browser_rows == [
        ("window-31", "混合店铺", "", "墨西哥", "", "", ""),
    ]
    legacy_row = next(
        row for row in database_calls[0][0] if row[1] == "墨西哥"
    )
    assert legacy_row[:7] == [
        "混合店铺",
        "墨西哥",
        "绿色",
        88,
        "1.2%",
        "2.3%",
        "0.4%",
    ]
    assert legacy_row[7:10] == [
        "增长",
        "12%",
        "正常",
    ]
    assert legacy_row[10] != "2026-08-27 23:10:00"
    assert legacy_row[11] == "[11, 22, 33]"
    mexico_api_row = next(
        row for row in result["api_rows"] if row["site_id"] == "MLM"
    )
    assert mexico_api_row["visits"] == "[11, 22, 33]"
    assert mexico_api_row["infraction_count"] == 7
    assert mexico_api_row["rights_holder_count"] == 4
    assert mexico_api_row["infraction_recent_days"] == 100
    assert result["failed_stores"] == 0


def test_auxiliary_browser_collector_opens_summary_not_reputation(monkeypatch):
    opened_urls = []
    selected = []
    wait_values = iter(["库存提醒", "Increased 6%"])

    class WaitResult:
        def __init__(self, text):
            self.text = text

    class FakeWait:
        def __init__(self, *_args, **_kwargs):
            pass

        def until(self, _condition):
            return WaitResult(next(wait_values))

    monkeypatch.setattr(
        bit_reputation_info,
        "_open_collection_backend_page",
        lambda _driver, url, **_kwargs: opened_urls.append(url) or {},
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_select_country",
        lambda _driver, site, name, **kwargs: selected.append((site, name, kwargs)),
    )
    monkeypatch.setattr(bit_reputation_info, "WebDriverWait", FakeWait)
    monkeypatch.setattr(
        bit_reputation_info,
        "get_visits_info",
        lambda *_args, **_kwargs: [101, 202, 303],
    )

    result = bit_reputation_info.get_reputation_auxiliary_info(
        "window-id",
        "测试店铺",
        "墨西哥",
        driver=object(),
    )

    assert opened_urls == [bit_reputation_info.SALES_SUMMARY_URL]
    assert bit_reputation_info.REPUTATION_URL not in opened_urls
    assert selected == [
        (
            "墨西哥",
            "测试店铺",
            {
                "recovery_url": bit_reputation_info.SALES_SUMMARY_URL,
                "structure_context": "销售汇总页面",
            },
        )
    ]
    assert result["direction"] == "增长"
    assert result["gradient_rate"] == "6%"
    assert result["system_warning"] == "库存提醒"
    assert result["visits"] == "[101, 202, 303]"


def test_traffic_collector_only_opens_metrics_page(monkeypatch):
    opened_urls = []
    selected = []
    monkeypatch.setattr(
        bit_reputation_info,
        "_open_collection_backend_page",
        lambda _driver, url, **_kwargs: opened_urls.append(url) or {},
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_select_country",
        lambda _driver, site, name, **kwargs: selected.append((site, name, kwargs)),
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "get_visits_info",
        lambda *_args, **_kwargs: [101, 202, 303],
    )

    result = bit_reputation_info.get_reputation_traffic_info(
        "window-id",
        "测试店铺",
        "墨西哥",
        driver=object(),
    )

    assert opened_urls == [bit_reputation_info.METRICS_URL]
    assert selected == [
        (
            "墨西哥",
            "测试店铺",
            {
                "recovery_url": bit_reputation_info.METRICS_URL,
                "structure_context": "流量页面",
            },
        )
    ]
    assert result["visits"] == "[101, 202, 303]"
    assert "system_warning" not in result
    assert "direction" not in result


def test_account_risk_summary_detects_restrictions_and_warnings():
    assert bit_reputation_info._account_risk_kinds_from_summary(
        "1 Go to Restrictions\n1 go to warnings"
    ) == ["restrictions", "warnings"]
    assert bit_reputation_info._account_risk_kinds_from_summary(
        "",
        [
            "https://global-selling.mercadolibre.com/account-risk?filter=warnings"
        ],
    ) == ["warnings"]
    assert bit_reputation_info._account_risk_kinds_from_summary(
        "Active restrictions\nRestrictions\n1\nWarnings\n0"
    ) == ["restrictions"]
    assert bit_reputation_info._account_risk_kinds_from_summary(
        "Active restrictions\nRestrictions 0\nWarnings 2"
    ) == ["warnings"]
    assert bit_reputation_info._account_risk_kinds_from_summary(
        "Account status\nRequires attention\nAvoid future restrictions."
    ) == ["restrictions"]
    assert bit_reputation_info._account_risk_kinds_from_summary(
        "Restrictions 0\nWarnings 0"
    ) == []


def test_account_risk_details_remove_summary_and_parent_duplicates():
    assert bit_reputation_info._normalize_account_risk_details(
        [
            "Restrictions\n1 Go to Restrictions\nListing paused because the brand is restricted",
            "Listing paused because the brand is restricted",
            "Warnings",
            "1 [Go to Warnings](https://global-selling.mercadolibre.com/account-risk?filter=warnings)",
        ]
    ) == ["Listing paused because the brand is restricted"]


def test_account_risk_details_keep_only_title_and_time():
    assert bit_reputation_info._normalize_account_risk_details(
        [
            "Your account has been suspended from selling\n"
            "August 11, at 00:30\n"
            "Paused Features\n"
            "Edit Mercado Libre listings"
        ]
    ) == ["Your account has been suspended from selling August 11, at 00:30"]


def test_collect_account_risk_details_opens_each_filter_and_keeps_details_only(monkeypatch):
    opened_urls = []
    detail_batches = iter(
        [
            ["Restriction detail"],
            ["Warning detail", "Restriction detail"],
        ]
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_open_collection_backend_page",
        lambda _driver, url, **_kwargs: opened_urls.append(url) or {},
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_wait_account_risk_details",
        lambda _driver: next(detail_batches),
    )

    result = bit_reputation_info._collect_account_risk_detail_text(
        object(),
        ["restrictions", "warnings"],
        window_id="window-id",
        name="测试店铺",
        site="巴西",
    )

    assert opened_urls == [
        bit_reputation_info.ACCOUNT_RISK_URLS["restrictions"],
        bit_reputation_info.ACCOUNT_RISK_URLS["warnings"],
    ]
    assert result == "Restriction detail\nWarning detail"


def test_auxiliary_replaces_account_risk_count_with_details(monkeypatch):
    wait_values = iter(["1 Go to Restrictions", "Decreased 4%"])

    class WaitResult:
        def __init__(self, text):
            self.text = text

    class FakeWait:
        def __init__(self, *_args, **_kwargs):
            pass

        def until(self, _condition):
            return WaitResult(next(wait_values))

    monkeypatch.setattr(
        bit_reputation_info,
        "_open_collection_backend_page",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(bit_reputation_info, "_select_country", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bit_reputation_info, "WebDriverWait", FakeWait)
    monkeypatch.setattr(
        bit_reputation_info,
        "_get_account_risk_links",
        lambda _driver: [bit_reputation_info.ACCOUNT_RISK_URLS["restrictions"]],
    )
    monkeypatch.setattr(
        bit_reputation_info,
        "_collect_account_risk_detail_text",
        lambda *_args, **_kwargs: "商品因受限品牌被暂停\n请移除相关品牌信息",
    )
    monkeypatch.setattr(bit_reputation_info, "get_visits_info", lambda *_args, **_kwargs: [])

    result = bit_reputation_info.get_reputation_auxiliary_info(
        "window-id",
        "测试店铺",
        "巴西",
        driver=object(),
    )

    assert result["system_warning"] == "商品因受限品牌被暂停\n请移除相关品牌信息"
    assert "Go to Restrictions" not in result["system_warning"]


def test_legacy_summary_schedulers_delegate_to_official_api(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_reputation_info,
        "main",
        lambda **kwargs: calls.append(kwargs) or {"source": "official-api"},
    )

    selenium_result = bit_summary_info.get_reputation_info_all(max_workers=2)
    playwright_result = playwright_summary_info.get_reputation_info_all(max_workers=3)

    assert selenium_result == {"source": "official-api"}
    assert playwright_result == {"source": "official-api"}
    assert calls == [{"max_workers": 2}, {"max_workers": 3}]
