import threading
from datetime import datetime
from unittest import mock

import pytest

from bit import bit_daily_task


def _live_infraction_rows(*site_counts):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for shop_name, site_name, count in site_counts:
        rows.extend(
            [shop_name, site_name, f"INF-{shop_name}-{site_name}-{index}", "", today, "", "", "侵权"]
            for index in range(count)
        )
    return rows


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("侵权", bit_daily_task.APPEAL_TYPE_INFRACTION),
        ("延误率", bit_daily_task.APPEAL_TYPE_DELAY),
        ("delay", bit_daily_task.APPEAL_TYPE_DELAY),
        ("取消率", bit_daily_task.APPEAL_TYPE_CANCELLATION),
        ("cancellation_rate", bit_daily_task.APPEAL_TYPE_CANCELLATION),
        ("投诉", bit_daily_task.APPEAL_TYPE_COMPLAINT),
        ("complaints", bit_daily_task.APPEAL_TYPE_COMPLAINT),
        ("混合模式", bit_daily_task.APPEAL_TYPE_MIXED),
        ("mixed", bit_daily_task.APPEAL_TYPE_MIXED),
    ],
)
def test_normalize_appeal_type(value, expected):
    assert bit_daily_task.normalize_appeal_type(value) == expected


def test_mixed_mode_runs_the_fixed_six_task_round():
    assert bit_daily_task.appeal_type_sequence("混合模式") == (
        bit_daily_task.APPEAL_TYPE_INFRACTION,
        bit_daily_task.APPEAL_TYPE_DELAY,
        bit_daily_task.APPEAL_TYPE_INFRACTION,
        bit_daily_task.APPEAL_TYPE_COMPLAINT,
        bit_daily_task.APPEAL_TYPE_INFRACTION,
        bit_daily_task.APPEAL_TYPE_CANCELLATION,
    )


def test_task_switches_can_run_multiple_tasks_without_infraction():
    assert bit_daily_task.appeal_type_sequence(["投诉", "延误率"]) == (
        bit_daily_task.APPEAL_TYPE_DELAY,
        bit_daily_task.APPEAL_TYPE_COMPLAINT,
    )


def test_task_switches_insert_infraction_after_each_other_selected_task():
    assert bit_daily_task.appeal_type_sequence(["投诉", "侵权"]) == (
        bit_daily_task.APPEAL_TYPE_INFRACTION,
        bit_daily_task.APPEAL_TYPE_COMPLAINT,
        bit_daily_task.APPEAL_TYPE_INFRACTION,
    )


def test_loop_task_stops_before_starting_next_round(monkeypatch):
    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(
        bit_daily_task,
        "run_ai_appeal_once",
        lambda *args, **kwargs: pytest.fail("停止后不应启动申诉轮次"),
    )

    result = bit_daily_task._loop_ai_appeal_locked(
        "侵权",
        stop_event=stop_event,
    )

    assert result is None


def test_daily_task_worker_writes_output_to_shared_log(monkeypatch, tmp_path):
    log_path = tmp_path / "daily-task.log"

    def fake_appeal(*_args, **_kwargs):
        print("飞黄腾达 墨西哥申诉完成<br>")
        return {"status": "success"}

    monkeypatch.setattr(bit_daily_task, "appeal_one_shop", fake_appeal)

    result = bit_daily_task._appeal_one_shop_worker_for_type(
        {"name": "飞黄腾达"},
        "侵权",
        0,
        "",
        0,
        str(log_path),
    )

    assert result == {"status": "success"}
    assert "飞黄腾达 墨西哥申诉完成" in log_path.read_text(encoding="utf-8")


def test_mixed_mode_dispatches_the_full_sequence(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bit_daily_task,
        "build_appeal_plan",
        lambda appeal_type, **_kwargs: calls.append(appeal_type) or [],
    )

    result = bit_daily_task._run_ai_appeal_once_locked("混合模式")

    assert calls == list(bit_daily_task.MIXED_APPEAL_SEQUENCE)
    assert [item["appeal_type"] for item in result] == [
        bit_daily_task._appeal_type_label(item)
        for item in bit_daily_task.MIXED_APPEAL_SEQUENCE
    ]


def test_authorized_appeal_scope_uses_site_switches_and_salesperson(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "display_name": "授权店铺",
                    "nickname": "STORE_ALIAS",
                    "site_settings": [
                        {
                            "site_id": "MLM",
                            "salesperson": "张三",
                            "group_name": "精品组",
                            "appeal_enabled": True,
                        },
                        {
                            "site_id": "MLB",
                            "salesperson": "张三",
                            "group_name": "普通组",
                            "appeal_enabled": False,
                        },
                        {
                            "site_id": "MLC",
                            "salesperson": "李四",
                            "group_name": "普通组",
                            "appeal_enabled": True,
                        },
                    ],
                }
            ]
        },
    )

    all_scope = bit_daily_task.load_authorized_appeal_shop_site_config()
    owner_scope = bit_daily_task.load_authorized_appeal_shop_site_config(["张三"])
    group_scope = bit_daily_task.load_authorized_appeal_shop_site_config(
        group_names=["精品组"]
    )

    assert all_scope["授权店铺"] == {"MX", "CL"}
    assert all_scope["store_alias"] == {"MX", "CL"}
    assert owner_scope["授权店铺"] == {"MX"}
    assert group_scope["授权店铺"] == {"MX"}


def test_authorized_appeal_scope_requires_explicit_enabled_switch(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {"display_name": "无站点配置"},
                {
                    "display_name": "未勾选店铺",
                    "site_settings": [
                        {"site_id": "MLM"},
                        {"site_id": "MLB", "appeal_enabled": False},
                    ],
                },
                {
                    "display_name": "已勾选店铺",
                    "site_settings": [
                        {"site_id": "MLC", "appeal_enabled": True},
                    ],
                },
            ]
        },
    )

    assert bit_daily_task.load_authorized_appeal_shop_site_config() == {
        "已勾选店铺": {"CL"}
    }


def test_default_infraction_plan_uses_authorization_switches_not_browser_sites(monkeypatch):
    collection_calls = []
    monkeypatch.setattr(
        bit_daily_task.mercado_infraction_sync,
        "collect_live_detection_infractions",
        lambda targets, **kwargs: collection_calls.append((targets, kwargs)) or {
            "data": _live_infraction_rows(
                ("授权店铺", "墨西哥", 3),
                ("授权店铺", "巴西", 9),
            )
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": 7,
                    "display_name": "授权店铺",
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                        {"site_id": "MLB", "appeal_enabled": False},
                    ],
                }
            ]
        },
    )
    monkeypatch.setattr(
        bit_daily_task.bit_appeal_ai,
        "load_active_shop_site_config",
        lambda: {"授权店铺": {"BR"}},
    )

    plan = bit_daily_task.build_latest_infraction_appeal_plan(top_n=10)

    assert [site["site_code"] for site in plan[0]["sites"]] == ["MX"]
    assert collection_calls[0][0] == [
        {
            "token_id": 7,
            "name": "授权店铺",
            "aliases": ["授权店铺"],
            "site_ids": ["MLM"],
        }
    ]
    assert collection_calls[0][1]["recent_days"] == 100


def test_build_latest_delay_appeal_plan_filters_and_sorts(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task,
        "get_latest_reputation_info",
        lambda: {
            "latest_submit_time": "2026-07-25 10:00:00",
            "rows": [
                {"店铺名": "店铺甲", "站点": "墨西哥", "延误率": "8.5%", "取消率": "1%"},
                {"店铺名": "店铺甲", "站点": "巴西", "延误率": "3%", "取消率": "0%"},
                {"店铺名": "店铺乙", "站点": "巴西", "延误率": "12%", "取消率": "2%"},
                {"店铺名": "已忽略店铺", "站点": "墨西哥", "延误率": "20%", "取消率": "4%"},
            ],
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "display_name": "店铺甲",
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                        {"site_id": "MLB", "appeal_enabled": True},
                    ],
                },
                {
                    "display_name": "店铺乙",
                    "site_settings": [
                        {"site_id": "MLB", "appeal_enabled": True},
                    ],
                },
            ]
        },
    )

    plan = bit_daily_task.build_latest_reputation_appeal_plan(
        "延误率",
        top_n=10,
        min_rate="5%",
    )

    assert [shop["name"] for shop in plan] == ["店铺乙", "店铺甲"]
    assert [site["site_code"] for site in plan[1]["sites"]] == ["MX"]
    assert plan[0]["sites"][0]["rate"] == pytest.approx(0.12)


def test_reputation_appeal_plan_top_n_zero_keeps_every_affected_shop(monkeypatch):
    rows = [
        {"店铺名": f"店铺{index:02d}", "站点": "墨西哥", "投诉率": "1%"}
        for index in range(35)
    ]
    monkeypatch.setattr(
        bit_daily_task,
        "get_latest_reputation_info",
        lambda: {"rows": rows},
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "display_name": f"店铺{index:02d}",
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                    ],
                }
                for index in range(35)
            ]
        },
    )

    plan = bit_daily_task.build_latest_reputation_appeal_plan(
        "投诉",
        top_n=0,
    )

    assert len(plan) == 35


def test_infraction_appeal_plan_top_n_zero_keeps_every_affected_shop(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task.mercado_infraction_sync,
        "collect_live_detection_infractions",
        lambda _targets, **_kwargs: {
            "data": _live_infraction_rows(*(
                (f"店铺{index:02d}", "墨西哥", 1)
                for index in range(35)
            ))
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": index + 1,
                    "display_name": f"店铺{index:02d}",
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                    ],
                }
                for index in range(35)
            ]
        },
    )

    plan = bit_daily_task.build_latest_infraction_appeal_plan(
        top_n=0,
    )

    assert len(plan) == 35


def test_infraction_execution_standard_uses_each_site_count_and_is_strict(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task.mercado_infraction_sync,
        "collect_live_detection_infractions",
        lambda _targets, **_kwargs: {
            "data": _live_infraction_rows(
                ("刚好达标", "墨西哥", 5),
                ("超过标准", "墨西哥", 6),
            )
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": index + 1,
                    "display_name": name,
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                        {"site_id": "MLB", "appeal_enabled": True},
                    ],
                }
                for index, name in enumerate(("刚好达标", "超过标准"))
            ]
        },
    )

    plan = bit_daily_task.build_latest_infraction_appeal_plan(
        top_n=0,
        min_infraction_count=5,
    )

    assert [shop["name"] for shop in plan] == ["超过标准"]


def test_live_infraction_scan_traverses_all_authorized_sites_then_filters_zero(monkeypatch):
    collection_calls = []
    monkeypatch.setattr(
        bit_daily_task.mercado_infraction_sync,
        "collect_live_detection_infractions",
        lambda targets, **kwargs: collection_calls.append((targets, kwargs)) or {
            "data": _live_infraction_rows(
                ("多站点店铺", "墨西哥", 6),
                ("多站点店铺", "巴西", 20),
            )
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": 9,
                    "display_name": "多站点店铺",
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                        {"site_id": "MLC", "appeal_enabled": True},
                        {"site_id": "MCO", "appeal_enabled": True},
                        {"site_id": "MLB", "appeal_enabled": False},
                    ],
                }
            ]
        },
    )

    plan = bit_daily_task.build_latest_infraction_appeal_plan(
        top_n=0,
        min_infraction_count=5,
        max_workers=7,
        log_path="task-runtime.log",
    )

    assert [shop["name"] for shop in plan] == ["多站点店铺"]
    assert {site["site_code"] for site in plan[0]["sites"]} == {"MX"}
    assert {site["site_code"]: site["count"] for site in plan[0]["sites"]} == {
        "MX": 6,
    }
    assert plan[0]["total"] == 6
    assert collection_calls[0][0][0]["token_id"] == 9
    assert set(collection_calls[0][0][0]["site_ids"]) == {"MLM", "MLC", "MCO"}
    assert collection_calls[0][1]["max_workers"] == 7


def test_infraction_plan_reports_when_every_api_store_fails(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "id": 7,
                    "display_name": "失效店铺",
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                    ],
                }
            ]
        },
    )
    monkeypatch.setattr(
        bit_daily_task.mercado_infraction_sync,
        "collect_live_detection_infractions",
        lambda *_args, **_kwargs: {
            "data": [],
            "results": [
                {
                    "store": "失效店铺",
                    "status": "error",
                    "message": "Access Token 已失效",
                }
            ],
            "failed_stores": [
                {
                    "store": "失效店铺",
                    "status": "error",
                    "message": "Access Token 已失效",
                }
            ],
        },
    )

    with pytest.raises(RuntimeError, match="全部店铺的侵权 API 读取失败"):
        bit_daily_task.build_latest_infraction_appeal_plan(top_n=0)


@pytest.mark.parametrize(
    ("appeal_type", "rate_field", "standard_name"),
    [
        ("延误率", "延误率", "min_delay_rate"),
        ("投诉", "投诉率", "min_complaint_rate"),
        ("取消率", "取消率", "min_cancellation_rate"),
    ],
)
def test_reputation_execution_standards_are_independent_and_strict(
    monkeypatch,
    appeal_type,
    rate_field,
    standard_name,
):
    monkeypatch.setattr(
        bit_daily_task,
        "get_latest_reputation_info",
        lambda: {
            "rows": [
                {"店铺名": "刚好达标", "站点": "墨西哥", rate_field: "5%"},
                {"店铺名": "超过标准", "站点": "墨西哥", rate_field: "5.01%"},
            ]
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
                    "display_name": name,
                    "site_settings": [
                        {"site_id": "MLM", "appeal_enabled": True},
                    ],
                }
                for name in ("刚好达标", "超过标准")
            ]
        },
    )

    plan = bit_daily_task.build_appeal_plan(
        appeal_type,
        top_n=0,
        **{standard_name: "5%"},
    )

    assert [shop["name"] for shop in plan] == ["超过标准"]


def test_build_latest_cancellation_plan_only_keeps_positive_rates(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task,
        "get_latest_reputation_info",
        lambda: {
            "rows": [
                {"店铺名": "店铺甲", "站点": "墨西哥", "取消率": "0%"},
                {"店铺名": "店铺甲", "站点": "巴西", "取消率": "0.7%"},
            ]
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [{
                "display_name": "店铺甲",
                "site_settings": [
                    {"site_id": "MLM", "appeal_enabled": True},
                    {"site_id": "MLB", "appeal_enabled": True},
                ],
            }]
        },
    )

    plan = bit_daily_task.build_latest_reputation_appeal_plan(
        "取消率",
    )

    assert len(plan) == 1
    assert [site["site_code"] for site in plan[0]["sites"]] == ["BR"]


def test_build_latest_complaint_plan_uses_complaint_rate(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task,
        "get_latest_reputation_info",
        lambda: {
            "rows": [
                {"店铺名": "店铺甲", "站点": "墨西哥", "投诉率": "0%"},
                {"店铺名": "店铺甲", "站点": "巴西", "投诉率": "1.2%"},
            ]
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [{
                "display_name": "店铺甲",
                "site_settings": [
                    {"site_id": "MLM", "appeal_enabled": True},
                    {"site_id": "MLB", "appeal_enabled": True},
                ],
            }]
        },
    )

    plan = bit_daily_task.build_latest_reputation_appeal_plan(
        "投诉",
    )

    assert len(plan) == 1
    assert [site["site_code"] for site in plan[0]["sites"]] == ["BR"]


@pytest.mark.parametrize(
    ("method_name", "expected_type"),
    [
        ("auto_appeal_infraction", bit_daily_task.APPEAL_TYPE_INFRACTION),
        ("auto_appeal_delay", bit_daily_task.APPEAL_TYPE_DELAY),
        ("auto_appeal_cancellation", bit_daily_task.APPEAL_TYPE_CANCELLATION),
        ("auto_appeal_complaint", bit_daily_task.APPEAL_TYPE_COMPLAINT),
    ],
)
def test_auto_appeal_methods_dispatch_independently(
    monkeypatch,
    method_name,
    expected_type,
):
    calls = []
    monkeypatch.setattr(
        bit_daily_task,
        "run_ai_appeal_once",
        lambda appeal_type, **kwargs: calls.append((appeal_type, kwargs)) or "done",
    )

    result = getattr(bit_daily_task, method_name)(top_n=5, min_rate="1%")

    assert result == "done"
    assert calls == [(expected_type, {"top_n": 5, "min_rate": "1%"})]


@pytest.mark.parametrize(
    ("appeal_type", "expected_form"),
    [
        ("侵权", "侵权"),
        ("延误率", "延误"),
        ("取消率", "取消率"),
        ("投诉", "投诉"),
    ],
)
def test_shop_executor_sends_expected_form(monkeypatch, appeal_type, expected_form):
    calls = []
    monkeypatch.setattr(
        bit_daily_task.bit_appeal_ai,
        "shensu",
        lambda name, site, form, message, validate_open=False: calls.append(
            (name, site, form, message, validate_open)
        ) or "完成",
    )
    monkeypatch.setattr(bit_daily_task, "_resolve_login_anomaly", lambda *args: None)

    result = bit_daily_task._appeal_one_shop_locked(
        {
            "name": "测试店铺",
            "total": 1,
            "sites": [{"site_code": "MX", "count": 1}],
        },
        "window-id",
        object(),
        appeal_type=appeal_type,
        site_pause=0,
        message="测试话术",
    )

    assert calls == [("测试店铺", "MX", expected_form, "测试话术", True)]
    assert result["appeal_type"] == ("延误率" if expected_form == "延误" else expected_form)


def _single_site_shop_plan():
    return {
        "name": "测试店铺",
        "total": 1,
        "sites": [{"site_code": "MX", "count": 1}],
    }


def test_appeal_one_shop_always_closes_browser_window(monkeypatch):
    lease = mock.Mock()
    lease.acquire.return_value = True
    monkeypatch.setattr(
        bit_daily_task.bit_appeal_ai,
        "get_window_id_by_shop_name",
        lambda _name: "window-id",
    )
    monkeypatch.setattr(bit_daily_task, "create_window_lease", lambda *args, **kwargs: lease)
    monkeypatch.setattr(
        bit_daily_task,
        "_appeal_one_shop_locked",
        lambda *args, **kwargs: {"name": "测试店铺", "results": []},
    )
    close_browser = mock.Mock(return_value={"success": True})
    monkeypatch.setattr(bit_daily_task, "closeBrowser", close_browser)

    result = bit_daily_task.appeal_one_shop(_single_site_shop_plan())

    assert result["name"] == "测试店铺"
    close_browser.assert_called_once_with("window-id", lease=lease)
    lease.release.assert_called_once_with()


def test_daily_appeal_worker_limit_defaults_to_fifteen(monkeypatch):
    monkeypatch.delenv("BIT_DAILY_BROWSER_WORKER_LIMIT", raising=False)
    assert bit_daily_task._daily_browser_worker_limit() == 15


def test_parallel_appeal_shop_failure_does_not_stop_other_shops(monkeypatch):
    submitted = []
    start_delays = []

    class FakeFuture:
        def __init__(self, shop):
            self.shop = shop

        def result(self):
            if self.shop["name"] == "失败店铺":
                raise RuntimeError("店铺页面异常")
            return {"name": self.shop["name"], "results": [{"result": "完成"}]}

        def cancel(self):
            return False

    class FakeExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def submit(
            self,
            _worker,
            shop,
            _appeal_type,
            _site_pause,
            _message,
            start_delay,
            _log_path,
            _stop_event,
        ):
            submitted.append(shop["name"])
            start_delays.append(start_delay)
            return FakeFuture(shop)

        def shutdown(self, **_kwargs):
            return None

    plan = [
        {"name": "失败店铺", "total": 1, "sites": []},
        {"name": "正常店铺", "total": 1, "sites": []},
    ]
    monkeypatch.setattr(bit_daily_task, "build_appeal_plan", lambda *a, **k: plan)
    monkeypatch.setattr(bit_daily_task, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(bit_daily_task, "DEFAULT_START_STAGGER_SECONDS", 0)
    monkeypatch.setattr(
        bit_daily_task,
        "wait",
        lambda pending, **_kwargs: (set(pending), set()),
    )

    results = bit_daily_task._run_ai_appeal_once_locked(
        "侵权",
        max_workers=2,
    )

    assert submitted == ["失败店铺", "正常店铺"]
    assert start_delays == [0, 0]
    assert {item["name"] for item in results} == {"失败店铺", "正常店铺"}
    assert next(item for item in results if item["name"] == "失败店铺")["error"] == "店铺页面异常"
    assert next(item for item in results if item["name"] == "正常店铺")["results"] == [
        {"result": "完成"}
    ]


def test_stop_request_terminates_appeal_pool_without_waiting(monkeypatch):
    stop_event = threading.Event()
    terminated = []
    closed = []

    class FakeFuture:
        def cancel(self):
            return True

    class FakeExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def submit(self, *_args):
            return FakeFuture()

        def shutdown(self, **kwargs):
            pytest.fail(f"停止时不应等待进程池自然结束：{kwargs}")

    plan = [{"name": "正在运行店铺", "total": 1, "sites": []}]

    def stop_on_wait(pending, **_kwargs):
        stop_event.set()
        return set(), set(pending)

    monkeypatch.setattr(bit_daily_task, "build_appeal_plan", lambda *a, **k: plan)
    monkeypatch.setattr(bit_daily_task, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(bit_daily_task, "wait", stop_on_wait)
    monkeypatch.setattr(
        bit_daily_task,
        "terminate_process_pool",
        lambda executor: terminated.append(executor),
    )
    monkeypatch.setattr(
        bit_daily_task,
        "_force_close_appeal_plan_windows",
        lambda value: closed.extend(value),
    )

    result = bit_daily_task._run_ai_appeal_once_locked(
        "侵权",
        max_workers=1,
        stop_event=stop_event,
    )

    assert result == []
    assert len(terminated) == 1
    assert closed == plan


def test_stop_signal_interrupts_long_retry_wait_immediately():
    stop_event = threading.Event()
    stop_event.set()

    assert bit_daily_task._wait_or_stop(300, stop_event) is True


def test_shop_executor_closes_browser_when_auto_login_is_triggered(monkeypatch):
    close_calls = []
    monkeypatch.setattr(
        bit_daily_task.bit_appeal_ai,
        "shensu",
        lambda *args, **kwargs: "登录态失效并触发自动登录，已终止自动找客服",
    )
    monkeypatch.setattr(
        bit_daily_task,
        "closeBrowser",
        lambda window_id, lease=None: close_calls.append((window_id, lease))
        or {"success": True},
    )
    monkeypatch.setattr(bit_daily_task, "_save_login_anomaly", lambda *args: None)

    lease = object()
    result = bit_daily_task._appeal_one_shop_locked(
        _single_site_shop_plan(),
        "window-id",
        lease,
        site_pause=0,
    )

    assert close_calls == [("window-id", lease)]
    assert result["exit_reason"] == "未登录"


def test_shop_executor_closes_browser_before_rate_limit_retry(monkeypatch):
    close_calls = []
    appeal_results = iter(
        [
            f"访问限频：{bit_daily_task.bit_appeal_ai.MERCADO_RATE_LIMIT_TEXT}",
            "完成",
        ]
    )
    monkeypatch.setattr(
        bit_daily_task.bit_appeal_ai,
        "shensu",
        lambda *args, **kwargs: next(appeal_results),
    )
    monkeypatch.setattr(
        bit_daily_task,
        "closeBrowser",
        lambda window_id, lease=None: close_calls.append((window_id, lease))
        or {"success": True},
    )
    monkeypatch.setattr(bit_daily_task.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bit_daily_task, "_resolve_login_anomaly", lambda *args: None)

    lease = object()
    result = bit_daily_task._appeal_one_shop_locked(
        _single_site_shop_plan(),
        "window-id",
        lease,
        site_pause=0,
        rate_limit_retries=1,
        rate_limit_retry_seconds=0,
    )

    assert close_calls == [("window-id", lease)]
    assert result["results"][0]["result"] == "完成"
    assert result["results"][0]["rate_limit_retries"] == 1


def test_shop_executor_closes_browser_on_unexpected_appeal_error(monkeypatch):
    close_calls = []

    def fail_appeal(*args, **kwargs):
        raise RuntimeError("客服页面崩溃")

    monkeypatch.setattr(bit_daily_task.bit_appeal_ai, "shensu", fail_appeal)
    monkeypatch.setattr(
        bit_daily_task,
        "closeBrowser",
        lambda window_id, lease=None: close_calls.append((window_id, lease))
        or {"success": True},
    )
    monkeypatch.setattr(bit_daily_task, "_resolve_login_anomaly", lambda *args: None)

    lease = object()
    result = bit_daily_task._appeal_one_shop_locked(
        _single_site_shop_plan(),
        "window-id",
        lease,
        site_pause=0,
        site_retry_attempts=1,
    )

    assert close_calls == [("window-id", lease)]
    assert result["results"][0]["result"] == "执行异常：客服页面崩溃"


@pytest.mark.parametrize(
    "backend_result",
    [
        {
            "ok": True,
            "status": "ready",
            "message": "自动登录后业务页已就绪",
            "login_retry_count": 1,
        },
        {
            "ok": True,
            "status": "ready",
            "message": "切换节点后业务页已就绪",
            "rate_limit_retry_count": 1,
        },
    ],
)
def test_ai_appeal_aborts_after_backend_recovery(backend_result):
    with pytest.raises(RuntimeError, match="终止自动找客服"):
        bit_daily_task.bit_appeal_ai._abort_ai_appeal_after_backend_recovery(
            backend_result,
            "测试店铺",
            "墨西哥",
        )


def test_manual_ai_appeal_can_continue_after_successful_backend_recovery():
    backend_result = {
        "ok": True,
        "status": "ready",
        "message": "自动登录后业务页已就绪",
        "login_retry_count": 1,
    }

    result = bit_daily_task.bit_appeal_ai._abort_ai_appeal_after_backend_recovery(
        backend_result,
        "测试店铺",
        "墨西哥",
        abort_on_recovery=False,
    )

    assert result is backend_result
