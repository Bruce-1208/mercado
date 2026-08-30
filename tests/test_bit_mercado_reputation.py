from pathlib import Path

from bit import bit_db_api
from bit import bit_interface
from bit import bit_reputation_info
from bit import bit_summary_info
from bit_playwright import bit_summary_info as playwright_summary_info
from bit.mercado_reputation import (
    REPUTATION_PATH,
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


def test_full_refresh_keeps_successes_and_logs_failed_stores(monkeypatch):
    def collect(**kwargs):
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
                {"id": 11, "display_name": "泽顺店铺", "nickname": "SELLER_A"},
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
    assert database_calls[0][1] == {}
    legacy_row = database_calls[0][0][0]
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
                {"id": 21, "display_name": "选定店铺", "nickname": "SELECTED"},
                {"id": 22, "display_name": "其他店铺", "nickname": "OTHER"},
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
                        {"site_id": "MLM", "visit_stats_enabled": True},
                        {"site_id": "MLB", "visit_stats_enabled": False},
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
        "list_config_rows",
        lambda **_kwargs: [
            # 浏览器配置只负责提供窗口，不再决定运行站点。
            ("window-31", "HYBRID", "", "巴西", "", "", ""),
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
                        "direction": "增长",
                        "gradient_rate": "12%",
                        "system_warning": "正常",
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
    legacy_row = database_calls[0][0][0]
    assert legacy_row[:7] == [
        "混合店铺",
        "墨西哥",
        "绿色",
        88,
        "1.2%",
        "2.3%",
        "0.4%",
    ]
    assert legacy_row[7:12] == [
        "增长",
        "12%",
        "正常",
        "2026-08-27 23:10:00",
        "[11, 22, 33]",
    ]
    assert result["api_rows"][0]["visits"] == "[11, 22, 33]"
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


def test_account_risk_details_remove_summary_and_parent_duplicates():
    assert bit_reputation_info._normalize_account_risk_details(
        [
            "Restrictions\n1 Go to Restrictions\nListing paused because the brand is restricted",
            "Listing paused because the brand is restricted",
            "Warnings",
            "1 [Go to Warnings](https://global-selling.mercadolibre.com/account-risk?filter=warnings)",
        ]
    ) == ["Listing paused because the brand is restricted"]


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
