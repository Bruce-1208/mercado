from unittest import mock

import pytest

from bit import bit_daily_task


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


def test_mixed_mode_runs_every_other_task_followed_by_infraction():
    assert bit_daily_task.appeal_type_sequence("混合模式") == (
        bit_daily_task.APPEAL_TYPE_INFRACTION,
        bit_daily_task.APPEAL_TYPE_DELAY,
        bit_daily_task.APPEAL_TYPE_INFRACTION,
        bit_daily_task.APPEAL_TYPE_COMPLAINT,
        bit_daily_task.APPEAL_TYPE_INFRACTION,
        bit_daily_task.APPEAL_TYPE_CANCELLATION,
        bit_daily_task.APPEAL_TYPE_INFRACTION,
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


def test_default_infraction_plan_uses_authorization_switches_not_browser_sites(monkeypatch):
    monkeypatch.setattr(
        bit_daily_task,
        "get_latest_infraction_info",
        lambda _days: {
            "summary": [
                {"店铺名": "授权店铺", "站点": "墨西哥", "总数": 3},
                {"店铺名": "授权店铺", "站点": "巴西", "总数": 9},
            ]
        },
    )
    monkeypatch.setattr(
        bit_daily_task,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [
                {
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
    summary = [
        {"店铺名": f"店铺{index:02d}", "站点": "墨西哥", "总数": 1}
        for index in range(35)
    ]
    monkeypatch.setattr(
        bit_daily_task,
        "get_latest_infraction_info",
        lambda _recent_days: {"latest_submit_time": "2026-08-23 10:00:00", "summary": summary},
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

    plan = bit_daily_task.build_latest_infraction_appeal_plan(
        top_n=0,
    )

    assert len(plan) == 35


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
