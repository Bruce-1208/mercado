import pytest
from openpyxl import load_workbook

from bit import bit_mercado_limit as mercado_limit
from bit import bit_mercado_login as mercado_login


def test_load_shop_login_config_reads_database_config(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "require_shop_config",
        lambda **kwargs: {
            "shop_name": "测试店铺",
            "window_id": "window-1",
            "email": "shop@example.com",
        },
    )

    config = mercado_login.load_shop_login_config(
        "测试店铺",
        window_id="window-1",
    )

    assert config["email"] == "shop@example.com"
    assert config["window_id"] == "window-1"
    assert config["email_column_exists"] is True
    assert config["config_source"] == "database"


def test_load_shop_login_config_rejects_runtime_excel_path():
    with pytest.raises(RuntimeError, match="运行时不再读取"):
        mercado_login.load_shop_login_config(
            "测试店铺",
            config_path="比特配置文件.xlsx",
        )


@pytest.mark.parametrize("stage", ["email", "password", "verification", "captcha", "login"])
def test_login_detection_never_performs_automatic_login(monkeypatch, stage):
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda driver: True)
    monkeypatch.setattr(mercado_login, "detect_login_stage", lambda driver: stage)

    # object() 没有输入、点击或提交能力；若检测代码尝试自动登录，本测试会报错。
    result = mercado_login.ensure_mercado_login(object(), "龙凤呈祥")

    assert result["ok"] is False
    assert result["status"] == mercado_login.LOGIN_NOT_LOGGED_IN
    assert result["login_stage"] == stage
    assert "不执行任何自动登录操作" in result["message"]
    assert mercado_login.is_login_blocking_result(result["status"])


def test_command_line_accepts_shop_name():
    args = mercado_login.build_command_line_parser().parse_args(
        ["--shop", "龙凤呈祥", "--wait-seconds", "12", "--no-navigate"]
    )

    assert args.shop == "龙凤呈祥"
    assert args.wait_seconds == 12
    assert args.no_navigate is True


def test_command_line_accepts_all_active_login_with_three_workers():
    args = mercado_login.build_command_line_parser().parse_args(
        ["--all-active-login"]
    )

    assert args.all_active_login is True
    assert args.workers == 3


def test_command_line_accepts_single_shop_auto_login():
    args = mercado_login.build_command_line_parser().parse_args(
        [
            "--shop",
            "四季如春",
            "--auto-login",
            "--keep-browser-open",
            "--manual-login-wait-seconds",
            "180",
        ]
    )

    assert args.shop == "四季如春"
    assert args.auto_login is True
    assert args.keep_browser_open is True
    assert args.manual_login_wait_seconds == 180


def test_command_line_accepts_multiple_selected_window_ids():
    args = mercado_login.build_command_line_parser().parse_args(
        [
            "--window-id",
            "window-1",
            "--window-id",
            "window-2",
            "--no-email",
            "--keep-browser-open",
            "--manual-login-wait-seconds",
            "180",
        ]
    )

    assert args.window_ids == ["window-1", "window-2"]
    assert args.no_email is True
    assert args.keep_browser_open is True
    assert args.manual_login_wait_seconds == 180


def test_single_selected_command_keeps_browser_open(monkeypatch):
    args = mercado_login.build_command_line_parser().parse_args(
        ["--shop", "指定店铺", "--auto-login", "--keep-browser-open"]
    )
    captured = {}
    monkeypatch.setattr(
        mercado_login,
        "load_shop_login_config",
        lambda shop_name: {
            "shop_name": shop_name,
            "window_id": "window-selected",
            "email": "selected@example.com",
        },
    )

    def fake_login(config, **kwargs):
        captured.update(config=config, kwargs=kwargs)
        return {
            **config,
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
            "browser_opened": True,
            "browser_closed": False,
            "browser_kept_open": True,
        }

    monkeypatch.setattr(mercado_login, "login_one_database_shop", fake_login)
    monkeypatch.setattr(
        mercado_login,
        "sync_login_results_to_window_anomalies",
        lambda results: {},
    )

    assert mercado_login.run_single_auto_login_from_command_line(args) == 0
    assert captured["config"]["email"] == "selected@example.com"
    assert captured["kwargs"]["close_browser"] is False


def test_all_shop_command_prints_summary_without_json_scope_error(
    monkeypatch,
    capsys,
):
    class AvailableLock:
        def acquire(self, timeout=0):
            return True

        def release(self):
            return None

    monkeypatch.setattr(
        mercado_login,
        "InterProcessLock",
        lambda *args, **kwargs: AvailableLock(),
    )
    monkeypatch.setattr(
        mercado_login,
        "run_all_database_shop_logins",
        lambda **kwargs: {
            "shop_count": 67,
            "success_count": 44,
            "outcome_counts": {
                mercado_login.LOGIN_OUTCOME_ALREADY_ACTIVE: 43,
                mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS: 1,
            },
            "report_path": "login-report.xlsx",
            "email_sent": True,
            "max_workers": 3,
            "status_counts": {},
        },
    )

    exit_code = mercado_login.main(["--all-active-login"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"ok": true' in output
    assert '"shop_count": 67' in output
    assert "cannot access local variable 'json'" not in output


def test_command_line_rejects_overlapping_login_job(monkeypatch, capsys):
    class BusyLock:
        def acquire(self, timeout=0):
            return False

    monkeypatch.setattr(mercado_login, "InterProcessLock", lambda *a, **k: BusyLock())
    monkeypatch.setattr(
        mercado_login,
        "get_lock_owner",
        lambda key: {
            "owner": "bit_mercado_login:已有批次",
            "pid": 123,
            "metadata": {"target": "全部未忽略店铺"},
        },
    )

    exit_code = mercado_login.main(["--all-active-login", "--no-email"])
    output = capsys.readouterr().out

    assert exit_code == 5
    assert "已有登录检测任务运行中" in output
    assert "bit_mercado_login:已有批次" in output


def test_single_and_selected_login_jobs_use_window_scoped_locks(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "load_shop_login_config",
        lambda shop_name: {"window_id": "window-single"},
    )
    single_args = mercado_login.build_command_line_parser().parse_args(
        ["--shop", "四季如春", "--auto-login"]
    )
    selected_args = mercado_login.build_command_line_parser().parse_args(
        ["--window-id", "window-2", "--window-id", "window-1"]
    )

    single_keys = mercado_login._command_line_login_job_lock_keys(single_args)
    selected_keys = mercado_login._command_line_login_job_lock_keys(
        selected_args,
        ("window-2", "window-1"),
    )

    assert single_keys == ("mercado_login_job_window_window-single",)
    assert selected_keys == (
        "mercado_login_job_window_window-1",
        "mercado_login_job_window_window-2",
    )
    assert mercado_login.MERCADO_LOGIN_JOB_LOCK_KEY not in single_keys
    assert mercado_login.MERCADO_LOGIN_JOB_LOCK_KEY not in selected_keys


def test_single_login_stops_when_all_shop_job_is_running(monkeypatch, capsys):
    monkeypatch.setattr(
        mercado_login,
        "load_shop_login_config",
        lambda shop_name: {"window_id": "window-single"},
    )
    monkeypatch.setattr(
        mercado_login,
        "get_lock_owner",
        lambda key: (
            {
                "owner": "bit_mercado_login:全部未忽略店铺",
                "pid": 456,
                "metadata": {"target": "全部未忽略店铺"},
            }
            if key == mercado_login.MERCADO_LOGIN_JOB_LOCK_KEY
            else {}
        ),
    )

    exit_code = mercado_login.main(["--shop", "四季如春", "--auto-login"])
    output = capsys.readouterr().out

    assert exit_code == 5
    assert "请等待全店登录任务结束后重试" in output


def test_login_results_are_synced_to_window_anomalies(monkeypatch):
    upserts = []
    resolved = []
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "upsert_window_anomaly",
        lambda *args, **kwargs: upserts.append((args, kwargs)),
    )
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "resolve_window_anomaly",
        resolved.append,
    )

    summary = mercado_login.sync_login_results_to_window_anomalies(
        [
            {
                "shop_name": "成功店铺",
                "window_id": "window-success",
                "ok": True,
                "status": mercado_login.LOGIN_SUCCESS,
            },
            {
                "shop_name": "限频店铺",
                "window_id": "window-rate-limit",
                "ok": False,
                "status": mercado_login.LOGIN_FAILED,
                "login_stage": "rate_limited",
                "message": "切换节点后仍然限频",
            },
            {
                "shop_name": "验证码店铺",
                "window_id": "window-verification",
                "ok": False,
                "status": mercado_login.LOGIN_VERIFICATION_REQUIRED,
                "message": "需要验证码",
            },
            {
                "shop_name": "人机验证店铺",
                "window_id": "window-captcha",
                "ok": False,
                "status": mercado_login.LOGIN_CAPTCHA_REQUIRED,
                "login_stage": "captcha",
                "message": "需要人工处理人机验证",
            },
            {
                "shop_name": "接口超时店铺",
                "window_id": "window-timeout",
                "ok": False,
                "status": mercado_login.LOGIN_FAILED,
                "message": "timeout of 30000ms exceeded",
                "action": "执行异常",
            },
        ]
    )

    assert resolved == [
        "window-success",
    ]
    assert summary["resolved_count"] == 1
    assert summary["anomaly_count"] == 3
    assert summary["skipped_count"] == 2
    assert len(upserts) == 3
    assert [call[1]["anomaly_type"] for call in upserts] == [
        mercado_login.LOGIN_LOGGED_OUT,
        mercado_login.LOGIN_LOGGED_OUT,
        mercado_login.LOGIN_CAPTCHA_REQUIRED,
    ]
    assert all(
        call[1]["source"].startswith("bit_mercado_login｜服务器:")
        for call in upserts
    )
    assert all("执行端：服务器：" in call[1]["reason"] for call in upserts)


def test_login_check_always_uses_global_selling_home(monkeypatch):
    class Driver:
        def __init__(self):
            self.cdp_commands = []
            self.current_url = "https://www.mercadolibre.com/jms/cbt/lgz/msl/login/x/legacy-user"
            self.title = "Login"
            self.switch_to = self

        def default_content(self):
            return None

        def execute_cdp_cmd(self, command, params):
            self.cdp_commands.append((command, params))
            return {}

        def execute_script(self, script, *args):
            if "document.body" in script:
                return "Fill out your e-mail address to log in"
            return None

    driver = Driver()
    ensure_calls = []
    monkeypatch.setattr(mercado_login.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda value: True)
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login",
        lambda *args, **kwargs: ensure_calls.append((args, kwargs)) or {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "message": mercado_login.LOGIN_NOT_LOGGED_IN,
        },
    )

    result = mercado_login.ensure_mercado_login_from_home(
        driver,
        "龙凤呈祥",
        window_id="window-1",
    )

    assert driver.cdp_commands[0] == (
        "Page.navigate",
        {"url": "https://global-selling.mercadolibre.com/"},
    )
    assert all(command != "Page.stopLoading" for command, _ in driver.cdp_commands)
    assert len(ensure_calls) == 1
    assert result["login_check_url"] == mercado_login.MERCADO_HOME_URL
    assert result["login_detected_before"] is True


def test_rate_limit_detection_only_matches_designated_spanish_message():
    assert not mercado_login.is_mercado_rate_limited_page(
        state={
            "page_text": "429 Too Many Requests",
            "title": "",
            "current_url": "https://global-selling.mercadolibre.com/",
        }
    )
    assert not mercado_login.is_mercado_rate_limited_page(
        state={
            "page_text": "",
            "title": "",
            "current_url": "",
            "navigation_error": "HTTP 429 Too Many Requests",
        }
    )
    assert mercado_login.is_mercado_rate_limited_page(
        state={
            "page_text": "Hubo un error accediendo a esta página",
            "title": "Error",
            "current_url": mercado_login.MERCADO_HOME_URL,
        }
    )
    assert mercado_login.is_mercado_rate_limited_page(
        state={
            "page_text": "",
            "title": "",
            "page_source": "<h1>Hubo un error accediendo a esta pagina</h1>",
        }
    )
    assert not mercado_login.is_mercado_rate_limited_page(
        state={
            "page_text": "Access denied - I'm not a robot",
            "title": "Verification",
            "current_url": "https://global-selling.mercadolibre.com/",
        }
    )
    assert not mercado_login.is_mercado_rate_limited_page(
        state={
            "page_text": "服务调用成功，但没有找到相应数据！",
            "title": "",
            "current_url": "",
        }
    )


def test_home_login_check_switches_node_before_each_rate_limit_retry(monkeypatch):
    states = [
        {
            "page_text": "Hubo un error accediendo a esta página",
            "title": "Error",
            "current_url": mercado_login.MERCADO_HOME_URL,
        },
        {
            "page_text": "Hubo un error accediendo a esta página",
            "title": "Error",
            "current_url": mercado_login.MERCADO_HOME_URL,
        },
        {
            "page_text": "Seller home",
            "title": "Home",
            "current_url": mercado_login.MERCADO_HOME_URL,
        },
    ]

    class Driver:
        def __init__(self):
            self.navigation_count = 0
            self.switch_to = self

        def default_content(self):
            return None

        def execute_cdp_cmd(self, command, params):
            assert command == "Page.navigate"
            assert params == {"url": mercado_login.MERCADO_HOME_URL}
            self.navigation_count += 1
            return {}

    driver = Driver()
    switch_calls = []
    sleep_calls = []
    monkeypatch.setattr(mercado_login.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(
        mercado_login,
        "_page_snapshot",
        lambda value: states[value.navigation_count - 1],
    )
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda value: False)
    monkeypatch.setattr(
        mercado_limit,
        "switch_random_hongkong_node",
        lambda: switch_calls.append(True)
        or {"switched": True, "reason": "switched", "new_node": "香港测试节点"},
    )

    result = mercado_login.ensure_mercado_login_from_home(
        driver,
        "限频测试店铺",
        navigation_wait_seconds=0,
    )

    assert result["ok"] is True
    assert result["status"] == mercado_login.LOGIN_ALREADY_ACTIVE
    assert result["rate_limited"] is True
    assert result["rate_limit_retry_count"] == 2
    assert result["node_switch_result"]["switched"] is True
    assert driver.navigation_count == 3
    assert len(switch_calls) == 2
    assert len(result["node_switch_results"]) == 2
    assert sleep_calls == [30, 30]


def test_home_login_check_reports_failure_after_two_rate_limit_retries(monkeypatch):
    class Driver:
        def __init__(self):
            self.navigation_count = 0
            self.switch_to = self

        def default_content(self):
            return None

        def execute_cdp_cmd(self, command, params):
            self.navigation_count += 1
            return {}

    driver = Driver()
    switch_calls = []
    monkeypatch.setattr(mercado_login.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        mercado_login,
        "_page_snapshot",
        lambda value: {
            "page_text": "Hubo un error accediendo a esta página",
            "title": "Error",
            "current_url": mercado_login.MERCADO_HOME_URL,
        },
    )
    monkeypatch.setattr(
        mercado_limit,
        "switch_random_hongkong_node",
        lambda: switch_calls.append(True)
        or {"switched": False, "reason": "switch_failed"},
    )

    result = mercado_login.ensure_mercado_login_from_home(
        driver,
        "持续限频店铺",
        navigation_wait_seconds=0,
        rate_limit_retry_wait_seconds=0,
    )

    assert result["ok"] is False
    assert result["status"] == mercado_login.LOGIN_FAILED
    assert result["login_stage"] == "rate_limited"
    assert result["rate_limit_retry_count"] == 2
    assert "重试 2 次仍未恢复" in result["message"]
    assert result["node_switch_result"]["reason"] == "switch_failed"
    assert driver.navigation_count == 3
    assert len(switch_calls) == 2
    assert len(result["node_switch_results"]) == 2


def test_backend_page_relogs_and_reopens_original_url(monkeypatch):
    target_url = "https://global-selling.mercadolibre.com/orders"
    states = [
        {
            "current_url": "https://www.mercadolibre.com/jms/cbt/lgz/login",
            "title": "Log in",
            "page_text": "Fill out your e-mail address to log in",
        },
        {
            "current_url": target_url,
            "title": "Orders",
            "page_text": "Seller orders",
        },
    ]
    navigations = []
    login_calls = []
    status_events = []
    monkeypatch.setattr(
        mercado_login,
        "get_mercado_page_state",
        lambda _driver: states[len(navigations) - 1],
    )
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda _driver: False)
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "upsert_window_anomaly",
        lambda *args, **kwargs: status_events.append(("recorded", args, kwargs)),
    )
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "resolve_window_anomaly",
        lambda window_id: status_events.append(("resolved", window_id)),
    )

    result = mercado_login.open_mercado_backend_page(
        object(),
        target_url,
        "退出登录店铺",
        "window-1",
        settle_seconds=0,
        navigate=navigations.append,
        login_handler=lambda *args: login_calls.append(args) or {"ok": True},
        sleep=lambda _seconds: None,
    )

    assert result["ok"] is True
    assert result["login_retry_count"] == 1
    assert navigations == [target_url, target_url]
    assert len(login_calls) == 1
    assert status_events[0][0] == "recorded"
    assert status_events[0][2]["anomaly_type"] == mercado_login.LOGIN_LOGGED_OUT
    assert status_events[1] == ("resolved", "window-1")


def test_backend_page_records_logged_out_when_auto_login_fails(monkeypatch):
    target_url = "https://global-selling.mercadolibre.com/orders"
    recorded = []
    monkeypatch.setattr(
        mercado_login,
        "get_mercado_page_state",
        lambda _driver: {
            "current_url": "https://www.mercadolibre.com/jms/cbt/lgz/login",
            "title": "Log in",
            "page_text": "Fill out your e-mail address to log in",
        },
    )
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda _driver: False)
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "upsert_window_anomaly",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    result = mercado_login.open_mercado_backend_page(
        object(),
        target_url,
        "退出登录店铺",
        "window-logged-out",
        settle_seconds=0,
        navigate=lambda _url: None,
        login_handler=lambda *args: {
            "ok": False,
            "status": mercado_login.LOGIN_SAVED_PASSWORD_INCORRECT,
            "message": "默认密码错误",
        },
        anomaly_site="巴西",
        anomaly_source="订单任务",
    )

    assert result["status"] == "logged_out"
    assert len(recorded) == 1
    assert recorded[0][0][:3] == (
        "window-logged-out",
        "退出登录店铺",
        "巴西",
    )
    assert recorded[0][1]["anomaly_type"] == mercado_login.LOGIN_LOGGED_OUT
    assert recorded[0][1]["source"].startswith("订单任务｜服务器:")
    assert "执行端：服务器：" in recorded[0][1]["reason"]


def test_logged_out_result_does_not_misclassify_rate_limit_or_timeout():
    assert mercado_login.is_logged_out_result(
        {"status": mercado_login.LOGIN_VERIFICATION_REQUIRED, "login_stage": "verification"}
    )
    assert not mercado_login.is_logged_out_result(
        {
            "status": mercado_login.LOGIN_FAILED,
            "login_stage": "rate_limited",
            "message": "切换节点后仍然限频",
        }
    )
    assert not mercado_login.is_logged_out_result(
        {"status": mercado_login.LOGIN_FAILED, "message": "timeout of 30000ms exceeded"}
    )


def test_logged_out_log_records_agent_name_id_and_hostname(monkeypatch):
    recorded = []
    monkeypatch.setenv("BIT_EXECUTION_TARGET", "agent")
    monkeypatch.setenv("BIT_EXECUTION_AGENT_ID", "agent-office-01")
    monkeypatch.setenv("BIT_EXECUTION_AGENT_NAME", "办公室电脑")
    monkeypatch.setenv("BIT_EXECUTION_HOSTNAME", "OFFICE-PC")
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "upsert_window_anomaly",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    assert mercado_login.record_logged_out_anomaly(
        "window-agent",
        "Agent 店铺",
        "墨西哥",
        "侵权采集",
        "检测到登录页",
    )

    assert recorded[0][1]["source"] == "侵权采集｜Agent:办公室电脑"
    assert (
        recorded[0][1]["reason"]
        == "检测到登录页；执行端：Agent：办公室电脑；ID agent-office-01；主机 OFFICE-PC"
    )


def test_backend_page_records_captcha_with_task_source(monkeypatch):
    target_url = "https://global-selling.mercadolibre.com/reputation"
    recorded = []
    monkeypatch.setattr(
        mercado_login,
        "get_mercado_page_state",
        lambda _driver: {
            "current_url": "https://www.mercadolibre.com/jms/cbt/lgz/login",
            "title": "Log in",
            "page_text": "Fill out your e-mail address to log in",
        },
    )
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda _driver: False)
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "upsert_window_anomaly",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    result = mercado_login.open_mercado_backend_page(
        object(),
        target_url,
        "人机验证店铺",
        "window-captcha",
        settle_seconds=0,
        navigate=lambda _url: None,
        login_handler=lambda *args: {
            "ok": False,
            "status": mercado_login.LOGIN_CAPTCHA_REQUIRED,
            "login_stage": "captcha",
            "message": "检测到人机验证，需要人工处理",
        },
        sleep=lambda _seconds: None,
        anomaly_site="墨西哥",
        anomaly_source="声誉采集",
    )

    assert result["ok"] is False
    assert len(recorded) == 1
    assert recorded[0][0][:3] == (
        "window-captcha",
        "人机验证店铺",
        "墨西哥",
    )
    assert recorded[0][1]["anomaly_type"] == mercado_login.LOGIN_CAPTCHA_REQUIRED
    assert recorded[0][1]["source"].startswith("声誉采集｜服务器:")
    assert "执行端：服务器：" in recorded[0][1]["reason"]


def test_backend_page_handles_designated_limit_before_reopening(monkeypatch):
    target_url = "https://global-selling.mercadolibre.com/reputation"
    states = [
        {
            "current_url": target_url,
            "title": "Error",
            "page_text": "Hubo un error accediendo a esta página",
        },
        {
            "current_url": target_url,
            "title": "Reputation",
            "page_text": "Seller reputation",
        },
    ]
    navigations = []
    switches = []
    monkeypatch.setattr(
        mercado_login,
        "get_mercado_page_state",
        lambda _driver: states[len(navigations) - 1],
    )
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda _driver: False)
    monkeypatch.setattr(
        mercado_limit,
        "switch_random_hongkong_node",
        lambda: switches.append(True) or {"switched": True},
    )

    result = mercado_login.open_mercado_backend_page(
        object(),
        target_url,
        "限频店铺",
        settle_seconds=0,
        rate_limit_retry_wait_seconds=0,
        navigate=navigations.append,
        sleep=lambda _seconds: None,
    )

    assert result["ok"] is True
    assert result["rate_limit_retry_count"] == 1
    assert navigations == [target_url, target_url]
    assert switches == [True]


def test_separate_auto_login_uses_email_then_saved_password(monkeypatch):
    home_checks = iter(
        [
            {
                "ok": False,
                "status": mercado_login.LOGIN_NOT_LOGGED_IN,
                "login_stage": "email",
            },
            {"ok": True, "status": mercado_login.LOGIN_ALREADY_ACTIVE},
        ]
    )
    transitions = iter(["login", "password", "logged_in"])
    entered_emails = []
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: next(home_checks),
    )
    monkeypatch.setattr(
        mercado_login,
        "_fill_email_and_continue",
        lambda driver, email, **kwargs: entered_emails.append(email) or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_wait_for_stage_transition",
        lambda *args, **kwargs: next(transitions),
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_password_login_option",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_submit_browser_saved_password",
        lambda *args, **kwargs: (True, "已提交", "password"),
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "测试店铺",
        window_id="window-1",
        email="shop@example.com",
    )

    assert entered_emails == ["shop@example.com"]
    assert result["ok"] is True
    assert result["status"] == mercado_login.LOGIN_SUCCESS
    assert result["action"] == "自动登录"
    assert result["initial_login_status"] == mercado_login.INITIAL_LOGIN_INACTIVE
    assert result["program_login_result"] == mercado_login.PROGRAM_LOGIN_SUCCESS
    assert (
        result["result_category"]
        == mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS
    )


def test_auto_login_enters_login_page_before_filling_email(monkeypatch):
    home_checks = iter(
        [
            {
                "ok": False,
                "status": mercado_login.LOGIN_NOT_LOGGED_IN,
                "login_stage": "login",
            },
            {"ok": True, "status": mercado_login.LOGIN_ALREADY_ACTIVE},
        ]
    )
    transitions = iter(["email", "login", "password", "logged_in"])
    actions = []
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: next(home_checks),
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_login_entry_button",
        lambda *args, **kwargs: actions.append("login_entry") or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_fill_email_and_continue",
        lambda driver, email, **kwargs: actions.append(("email", email)) or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_password_login_option",
        lambda *args, **kwargs: actions.append("password_option") or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_submit_browser_saved_password",
        lambda *args, **kwargs: actions.append("confirm")
        or (True, "已提交", "password"),
    )
    monkeypatch.setattr(
        mercado_login,
        "_wait_for_stage_transition",
        lambda *args, **kwargs: next(transitions),
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "入口页店铺",
        window_id="window-1",
        email="shop@example.com",
    )

    assert result["status"] == mercado_login.LOGIN_SUCCESS
    assert actions == [
        "login_entry",
        ("email", "shop@example.com"),
        "password_option",
        "confirm",
    ]


def test_auto_login_fills_email_before_reporting_visible_captcha(monkeypatch):
    actions = []
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "login_stage": "captcha",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda driver, selectors: (
            object() if selectors == mercado_login.EMAIL_INPUT_SELECTORS else None
        ),
    )
    monkeypatch.setattr(
        mercado_login,
        "_submit_email_with_retries",
        lambda driver, email, **kwargs: actions.append(("email", email))
        or (True, "captcha", 1),
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "人机验证店铺",
        window_id="window-captcha",
        email="shop@example.com",
    )

    assert actions == [("email", "shop@example.com")]
    assert result["ok"] is False
    assert result["status"] == mercado_login.LOGIN_CAPTCHA_REQUIRED
    assert result["login_stage"] == "captcha"
    assert result["action"] == "自动登录未完成"
    assert "输入邮箱后出现人机验证" in result["message"]


def test_email_submission_clicks_continue_instead_of_only_pressing_enter(monkeypatch):
    class EmailInput:
        def __init__(self):
            self.keys = []

        def click(self):
            return None

        def clear(self):
            return None

        def send_keys(self, *keys):
            self.keys.append(keys)

    email_input = EmailInput()
    continue_calls = []
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda *args, **kwargs: email_input,
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_continue_button",
        lambda *args, **kwargs: continue_calls.append(True) or True,
    )

    assert mercado_login._fill_email_and_continue(
        object(),
        "shop@example.com",
        shop_name="测试店铺",
    )
    assert email_input.keys[-1] == ("shop@example.com",)
    assert all(keys != (mercado_login.Keys.ENTER,) for keys in email_input.keys)
    assert len(continue_calls) == 1


def test_continue_click_waits_for_button_delayed_by_network(monkeypatch):
    clock = [0.0]
    matching_results = iter([False, False, True])
    matching_calls = []

    monkeypatch.setattr(
        mercado_login,
        "_click_matching_control",
        lambda *args, **kwargs: matching_calls.append(True)
        or next(matching_results),
    )
    monkeypatch.setattr(mercado_login, "_visible_elements", lambda *args: [])
    monkeypatch.setattr(mercado_login.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mercado_login.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert mercado_login._click_continue_button(
        object(),
        shop_name="网络波动店铺",
        wait_seconds=3,
    )
    assert len(matching_calls) == 3
    assert clock[0] == pytest.approx(0.5)


def test_email_continue_retries_click_without_retyping_correct_email(monkeypatch):
    class EmailInput:
        def get_attribute(self, name):
            return "shop@example.com" if name == "value" else ""

    entered_emails = []
    continue_clicks = []
    transitions = iter(["email", "email", "password"])
    monkeypatch.setattr(
        mercado_login,
        "_fill_email_and_continue",
        lambda driver, email, **kwargs: entered_emails.append(email) or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda *args, **kwargs: EmailInput(),
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_continue_button",
        lambda *args, **kwargs: continue_clicks.append(True) or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_wait_for_stage_transition",
        lambda *args, **kwargs: next(transitions),
    )

    submitted, stage, attempts = mercado_login._submit_email_with_retries(
        object(),
        "shop@example.com",
        shop_name="网络波动店铺",
        wait_seconds=60,
    )

    assert submitted is True
    assert stage == "password"
    assert attempts == 3
    assert entered_emails == ["shop@example.com"]
    assert len(continue_clicks) == 2


def test_email_submission_replaces_prefilled_default_email(monkeypatch):
    class PrefilledEmailInput:
        def __init__(self):
            self.value = "default-account@example.com"
            self.select_all = False

        def click(self):
            return None

        def clear(self):
            # 模拟 Mercado 的 React 受控输入框：clear() 不报错但也不清空。
            return None

        def send_keys(self, *keys):
            if keys == (mercado_login.Keys.CONTROL, "a"):
                self.select_all = True
            elif keys == (mercado_login.Keys.BACKSPACE,):
                if self.select_all:
                    self.value = ""
                self.select_all = False
            else:
                self.value += "".join(str(key) for key in keys)

        def get_attribute(self, name):
            return self.value if name == "value" else ""

    email_input = PrefilledEmailInput()
    submitted_values = []
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda *args, **kwargs: email_input,
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_continue_button",
        lambda *args, **kwargs: submitted_values.append(email_input.value) or True,
    )

    assert mercado_login._fill_email_and_continue(
        object(),
        "database-account@example.com",
        shop_name="测试店铺",
    )
    assert submitted_values == ["database-account@example.com"]
    assert email_input.value == "database-account@example.com"


def test_email_submission_uses_native_setter_when_autofill_restores_value(monkeypatch):
    class AutofilledEmailInput:
        def __init__(self):
            self.value = "default-account@example.com"

        def click(self):
            return None

        def clear(self):
            return None

        def send_keys(self, *keys):
            # 模拟浏览器自动填充：键盘清空和输入都被立即恢复成默认账号。
            self.value = "default-account@example.com"

        def get_attribute(self, name):
            return self.value if name == "value" else ""

    class Driver:
        def execute_script(self, script, element, value):
            element.value = value
            return element.value

    email_input = AutofilledEmailInput()
    submitted_values = []
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda *args, **kwargs: email_input,
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_continue_button",
        lambda *args, **kwargs: submitted_values.append(email_input.value) or True,
    )

    assert mercado_login._fill_email_and_continue(
        Driver(),
        "database-account@example.com",
        shop_name="测试店铺",
    )
    assert submitted_values == ["database-account@example.com"]


def test_saved_password_submission_clicks_confirm(monkeypatch):
    password_input = object()
    confirm_calls = []
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda *args, **kwargs: password_input,
    )
    monkeypatch.setattr(
        mercado_login,
        "_password_input_has_saved_value",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_confirm_button",
        lambda *args, **kwargs: confirm_calls.append(True) or True,
    )

    result = mercado_login._submit_browser_saved_password(
        object(),
        shop_name="测试店铺",
    )

    assert result == (True, "已提交浏览器保存的默认密码", "password")
    assert len(confirm_calls) == 1


def test_confirm_button_uses_accessible_semantics_when_visible_text_is_empty(
    monkeypatch,
):
    clicks = []

    class ConfirmButton:
        id = "confirm-control"
        text = ""

        def get_attribute(self, name):
            if name == "aria-labelledby":
                return "password-confirm-label"
            return ""

        def click(self):
            clicks.append("clicked")

    button = ConfirmButton()
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda driver, selectors: (
            [button] if selectors == mercado_login.CLICKABLE_SELECTORS else []
        ),
    )

    assert mercado_login._click_confirm_button(
        object(), shop_name="测试店铺", wait_seconds=0
    )
    assert clicks == ["clicked"]


def test_confirm_button_falls_back_to_password_form_submit_semantics(monkeypatch):
    clicks = []

    class SubmitButton:
        id = "submit-control"

        def click(self):
            clicks.append("clicked")

    class Driver:
        def execute_script(self, *args):
            return None

    button = SubmitButton()
    monkeypatch.setattr(
        mercado_login,
        "_click_matching_control",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda driver, selectors: (
            [button] if selectors == mercado_login.CONFIRM_BUTTON_SELECTORS else []
        ),
    )

    assert mercado_login._click_confirm_button(
        Driver(), shop_name="测试店铺", wait_seconds=0
    )
    assert clicks == ["clicked"]


def test_confirm_prefers_cdp_native_pointer_for_react_button(monkeypatch):
    class ConfirmButton:
        text = "Confirm"

        def get_attribute(self, name):
            return "submit" if name == "type" else ""

        def click(self):
            pytest.fail("CDP 原生鼠标成功后不应再调用 element.click()")

    class Driver:
        def __init__(self):
            self.cdp_calls = []

        def execute_cdp_cmd(self, method, params):
            self.cdp_calls.append((method, params))
            return {}

        def execute_script(self, script, element):
            return {"x": 120.5, "y": 240.25, "clickable": True}

    button = ConfirmButton()
    driver = Driver()
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda current_driver, selectors: (
            [button] if selectors == mercado_login.CLICKABLE_SELECTORS else []
        ),
    )

    assert mercado_login._click_confirm_button(
        driver,
        shop_name="React 店铺",
        wait_seconds=0,
    )
    assert [method for method, _params in driver.cdp_calls] == [
        "Page.bringToFront",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert driver.cdp_calls[2][1]["type"] == "mousePressed"
    assert driver.cdp_calls[3][1]["type"] == "mouseReleased"


def test_third_confirm_attempt_uses_form_request_submit(monkeypatch):
    button = object()
    scripts = []

    class Driver:
        def execute_script(self, script, element):
            scripts.append(script)
            return True

        def execute_cdp_cmd(self, *args, **kwargs):
            pytest.fail("requestSubmit 成功后不应再发送 CDP 点击")

    def click_matching_control(*args, **kwargs):
        return kwargs["click_element"](button)

    monkeypatch.setattr(
        mercado_login,
        "_click_matching_control",
        click_matching_control,
    )

    assert mercado_login._click_confirm_button(
        Driver(),
        shop_name="React 店铺",
        wait_seconds=0,
        submission_attempt=3,
    )
    assert len(scripts) == 1
    assert "requestSubmit" in scripts[0]


def test_confirm_click_waits_for_button_delayed_by_network(monkeypatch):
    clock = [0.0]
    matching_results = iter([False, False, True])
    matching_calls = []

    monkeypatch.setattr(
        mercado_login,
        "_click_matching_control",
        lambda *args, **kwargs: matching_calls.append(True)
        or next(matching_results),
    )
    monkeypatch.setattr(mercado_login, "_visible_elements", lambda *args: [])
    monkeypatch.setattr(mercado_login.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mercado_login.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert mercado_login._click_confirm_button(
        object(),
        shop_name="网络波动店铺",
        wait_seconds=3,
    )
    assert len(matching_calls) == 3
    assert clock[0] == pytest.approx(0.5)


def test_confirm_submission_retries_until_password_page_changes(monkeypatch):
    confirm_attempts = []
    transitions = iter(["password", "password", "logged_in"])
    monkeypatch.setattr(
        mercado_login,
        "_click_confirm_button",
        lambda *args, **kwargs: confirm_attempts.append(
            kwargs.get("submission_attempt")
        )
        or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_wait_for_stage_transition",
        lambda *args, **kwargs: next(transitions),
    )

    stage, attempts = mercado_login._complete_confirm_submission(
        object(),
        shop_name="网络波动店铺",
        wait_seconds=60,
    )

    assert stage == "logged_in"
    assert attempts == 3
    # 首次 Confirm 由上层密码提交函数完成，这里只记录后两次补点。
    assert confirm_attempts == [2, 3]


def test_saved_password_submission_still_confirms_when_value_is_not_script_readable(
    monkeypatch,
):
    class PasswordInput:
        def click(self):
            return None

        def send_keys(self, *keys):
            return None

    confirm_calls = []
    monkeypatch.setattr(mercado_login.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        mercado_login,
        "is_mercado_rate_limited_page",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda *args, **kwargs: PasswordInput(),
    )
    monkeypatch.setattr(
        mercado_login,
        "_password_input_has_saved_value",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        mercado_login,
        "detect_login_stage",
        lambda *args, **kwargs: "password",
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_confirm_button",
        lambda *args, **kwargs: confirm_calls.append(True) or True,
    )

    result = mercado_login._submit_browser_saved_password(
        object(), wait_seconds=1, shop_name="测试店铺"
    )

    assert result == (True, "已尝试选择浏览器保存的默认密码并提交", "password")
    assert len(confirm_calls) == 1


def test_saved_password_submission_reports_rate_limit(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "is_mercado_rate_limited_page",
        lambda *args, **kwargs: True,
    )

    assert mercado_login._submit_browser_saved_password(object()) == (
        False,
        "密码页面遇到限频",
        "rate_limited",
    )


@pytest.mark.parametrize(
    ("page_text", "saved_password_detected", "expected"),
    [
        (
            "The password is incorrect. Please try again.",
            True,
            mercado_login.LOGIN_SAVED_PASSWORD_INCORRECT,
        ),
        (
            "Please enter your password",
            False,
            mercado_login.LOGIN_SAVED_PASSWORD_MISSING,
        ),
        (
            "Please enter your password",
            True,
            mercado_login.LOGIN_FAILED,
        ),
    ],
)
def test_password_page_failure_distinguishes_missing_and_incorrect_password(
    monkeypatch,
    page_text,
    saved_password_detected,
    expected,
):
    monkeypatch.setattr(
        mercado_login,
        "_page_snapshot",
        lambda driver: {
            "page_text": page_text,
            "title": "Login",
            "current_url": "https://www.mercadolibre.com/login/password",
        },
    )

    status = mercado_login._classify_password_page_failure(
        object(),
        saved_password_detected=saved_password_detected,
    )

    assert status == expected


@pytest.mark.parametrize(
    ("detail", "classified_status", "expected_message"),
    [
        (
            mercado_login.SAVED_PASSWORD_SELECTION_ATTEMPTED_DETAIL,
            mercado_login.LOGIN_SAVED_PASSWORD_MISSING,
            "未保存可用的默认密码",
        ),
        (
            mercado_login.SAVED_PASSWORD_SUBMITTED_DETAIL,
            mercado_login.LOGIN_SAVED_PASSWORD_INCORRECT,
            "页面提示密码错误",
        ),
    ],
)
def test_auto_login_reports_password_failure_reason(
    monkeypatch,
    detail,
    classified_status,
    expected_message,
):
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "login_stage": "password",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "_submit_browser_saved_password",
        lambda *args, **kwargs: (True, detail, "password"),
    )
    monkeypatch.setattr(
        mercado_login,
        "_wait_for_stage_transition",
        lambda *args, **kwargs: "password",
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_confirm_button",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        mercado_login,
        "_classify_password_page_failure",
        lambda *args, **kwargs: classified_status,
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "测试店铺",
        window_id="window-1",
        email="shop@example.com",
    )

    assert result["status"] == classified_status
    assert result["program_login_result"] == mercado_login.PROGRAM_LOGIN_FAILED
    assert result["result_category"] == mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_FAILED
    assert expected_message in result["message"]


def test_auto_login_retries_continue_then_reports_rejected(monkeypatch):
    entered_emails = []
    continue_clicks = []
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "login_stage": "email",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "_fill_email_and_continue",
        lambda driver, email, **kwargs: entered_emails.append(email) or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_wait_for_stage_transition",
        lambda *args, **kwargs: "email",
    )
    monkeypatch.setattr(
        mercado_login,
        "_first_visible_element",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_continue_button",
        lambda *args, **kwargs: continue_clicks.append(True) or True,
    )
    monkeypatch.setattr(
        mercado_login,
        "_classify_email_page_failure",
        lambda driver: mercado_login.LOGIN_EMAIL_REJECTED,
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "测试店铺",
        window_id="window-1",
        email="shop@example.com",
    )

    assert entered_emails == ["shop@example.com"]
    assert len(continue_clicks) == 2
    assert result["status"] == mercado_login.LOGIN_EMAIL_REJECTED
    assert result["result_category"] == mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_FAILED


def test_verification_method_page_is_not_misclassified_as_email(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "_page_snapshot",
        lambda driver: {
            "page_text": (
                "Choose a verification method to log in\n"
                "Password\nYou'll enter your password.\n"
                "Email\nWe'll send you a code"
            ),
            "title": "Choose a verification method to log in",
            "current_url": "https://global-selling.mercadolibre.com/login/challenges",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda driver, selectors: []
        if selectors == mercado_login.CAPTCHA_SELECTORS
        else [object()],
    )

    assert mercado_login.detect_login_stage(object()) == "login"


def test_logged_in_backend_ignores_login_words_inside_hidden_page_source(monkeypatch):
    """正常后台源码含登录路由/脚本时，不能据此把已登录页面判成登录页。"""
    monkeypatch.setattr(
        mercado_login,
        "_page_snapshot",
        lambda driver: {
            "page_text": "Seller Center\nOrders\nReputation\nSales summary",
            "title": "Global Selling | Mercado Libre",
            "current_url": "https://global-selling.mercadolibre.com/",
            "page_source": (
                "<script>const loginUrl='/login/legacy-user';</script>"
                "<template>Fill out your e-mail address to log in</template>"
            ),
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda driver, selectors: [],
    )

    assert mercado_login.is_mercado_login_page(object()) is False
    assert mercado_login.detect_login_stage(object()) == "logged_in"


def test_visible_login_page_is_still_detected_without_page_source(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "_page_snapshot",
        lambda driver: {
            "page_text": "Fill out your e-mail address to log in\nEmail\nContinue",
            "title": "Log in",
            "current_url": "https://www.mercadolibre.com/login/legacy-user",
            "page_source": "",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda driver, selectors: [],
    )

    assert mercado_login.is_mercado_login_page(object()) is True
    assert mercado_login.detect_login_stage(object()) == "login"


def test_password_option_reads_nested_button_text(monkeypatch):
    class Button:
        text = ""

        def __init__(self):
            self.clicked = False

        def get_attribute(self, name):
            if name in ("innerText", "textContent"):
                return "Password\nYou'll enter your password."
            return ""

        def click(self):
            self.clicked = True

    button = Button()
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda *args, **kwargs: [button],
    )

    assert mercado_login._click_password_login_option(object(), "测试店铺") is True
    assert button.clicked is True


def test_password_option_uses_mouse_movement_for_transparent_card_button(monkeypatch):
    actions = []

    class Button:
        def click(self):
            actions.append("fallback_click")

    class MouseActions:
        def __init__(self, driver):
            actions.append(("driver", driver))

        def move_to_element(self, element):
            actions.append(("move", element))
            return self

        def pause(self, seconds):
            actions.append(("pause", seconds))
            return self

        def click(self):
            actions.append("mouse_click")
            return self

        def perform(self):
            actions.append("perform")

    class Driver:
        def execute_script(self, script, element):
            actions.append(("scroll", element))

    button = Button()
    driver = Driver()
    monkeypatch.setattr(
        mercado_login,
        "_visible_elements",
        lambda *args, **kwargs: [button],
    )
    monkeypatch.setattr(mercado_login, "ActionChains", MouseActions)

    assert mercado_login._click_password_login_option(driver, "六六大顺") is True
    assert ("scroll", button) in actions
    assert ("move", button) in actions
    assert "mouse_click" in actions
    assert "perform" in actions
    assert "fallback_click" not in actions


def test_small_background_captcha_placeholder_is_not_a_human_verification():
    class Placeholder:
        size = {"width": 1, "height": 1}

    class VisibleCaptcha:
        size = {"width": 304, "height": 78}

    assert not mercado_login._has_visible_captcha_widget([Placeholder()])
    assert mercado_login._has_visible_captcha_widget([VisibleCaptcha()])


def test_recaptcha_footer_is_not_misclassified_as_human_verification(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "_page_snapshot",
        lambda driver: {
            "page_text": (
                "Fill out your e-mail address to log in\n"
                "Email\nContinue\nProtected by reCAPTCHA\nPrivacy\nConditions"
            ),
            "title": "Fill out your e-mail address to log in",
            "current_url": "https://www.mercadolibre.com/login/legacy-user",
        },
    )

    def visible_elements(driver, selectors):
        if selectors == mercado_login.CAPTCHA_SELECTORS:
            return []
        if selectors == mercado_login.EMAIL_INPUT_SELECTORS:
            return [object()]
        return []

    monkeypatch.setattr(mercado_login, "_visible_elements", visible_elements)

    assert mercado_login.detect_login_stage(object()) == "email"


@pytest.mark.parametrize(
    ("stage", "status", "program_result", "category"),
    [
        (
            "verification",
            mercado_login.LOGIN_VERIFICATION_REQUIRED,
            mercado_login.PROGRAM_LOGIN_VERIFICATION_REQUIRED,
            mercado_login.LOGIN_OUTCOME_VERIFICATION_REQUIRED,
        ),
        (
            "captcha",
            mercado_login.LOGIN_CAPTCHA_REQUIRED,
            mercado_login.PROGRAM_LOGIN_CAPTCHA_REQUIRED,
            mercado_login.LOGIN_OUTCOME_CAPTCHA_REQUIRED,
        ),
    ],
)
def test_unlogged_verification_and_captcha_are_classified_separately(
    monkeypatch, stage, status, program_result, category
):
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "login_stage": stage,
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "_click_password_login_option",
        lambda *args, **kwargs: False,
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "测试店铺",
        window_id="window-1",
        email="shop@example.com",
    )

    assert result["ok"] is False
    assert result["status"] == status
    assert result["initial_login_status"] == mercado_login.INITIAL_LOGIN_INACTIVE
    assert result["program_login_result"] == program_result
    assert result["result_category"] == category


def test_originally_logged_in_is_not_counted_as_program_login(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
        },
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "测试店铺",
        window_id="window-1",
        email="shop@example.com",
    )

    assert result["initial_login_status"] == mercado_login.INITIAL_LOGIN_ACTIVE
    assert (
        result["program_login_result"]
        == mercado_login.PROGRAM_LOGIN_NOT_REQUIRED
    )
    assert result["result_category"] == mercado_login.LOGIN_OUTCOME_ALREADY_ACTIVE


def test_batch_config_with_empty_email_does_not_query_database_again(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "login_stage": "email",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "load_shop_login_config",
        lambda *args, **kwargs: pytest.fail("批处理不应在子进程重复读取数据库"),
    )

    result = mercado_login.login_mercado_with_saved_password(
        object(),
        "缺邮箱店铺",
        window_id="window-2",
        email="",
    )

    assert result["ok"] is False
    assert result["status"] == mercado_login.LOGIN_EMAIL_MISSING
    assert result["program_login_result"] == mercado_login.PROGRAM_LOGIN_FAILED
    assert result["result_category"] == mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_FAILED


def test_login_one_shop_always_closes_open_browser(monkeypatch):
    from bit import bit_api, bit_runtime_lock

    events = []

    class Lease:
        acquired = False

        def acquire(self, timeout=0):
            self.acquired = True
            events.append("lease_acquired")
            return True

        def release(self):
            events.append("lease_released")
            self.acquired = False

    class Service:
        def stop(self):
            events.append("service_stopped")

    class Driver:
        service = Service()

    monkeypatch.setattr(bit_runtime_lock, "create_window_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(
        mercado_login,
        "_connect_to_open_bit_browser",
        lambda *args, **kwargs: Driver(),
    )
    monkeypatch.setattr(
        mercado_login,
        "login_mercado_with_saved_password",
        lambda *args, **kwargs: {
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
            "message": "已登录",
        },
    )
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda window_id, lease=None: events.append(("closed", window_id))
        or {"success": True},
    )

    result = mercado_login.login_one_database_shop(
        {
            "config_index": 1,
            "shop_name": "测试店铺",
            "window_id": "window-1",
            "email": "shop@example.com",
        }
    )

    assert result["ok"] is True
    assert result["browser_opened"] is True
    assert result["browser_closed"] is True
    assert result["result_category"] == mercado_login.LOGIN_OUTCOME_ALREADY_ACTIVE
    assert ("closed", "window-1") in events
    assert events[-1] == "lease_released"


def test_selected_shop_keeps_browser_open_and_still_passes_configured_email(monkeypatch):
    from bit import bit_api, bit_runtime_lock

    events = []

    class Lease:
        def acquire(self, timeout=0):
            events.append("lease_acquired")
            return True

        def release(self):
            events.append("lease_released")

    class Service:
        def stop(self):
            events.append("service_stopped")

    class Driver:
        service = Service()

    monkeypatch.setattr(bit_runtime_lock, "create_window_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(
        mercado_login,
        "_connect_to_open_bit_browser",
        lambda *args, **kwargs: Driver(),
    )

    def fake_login(driver, shop_name, **kwargs):
        events.append(("login", shop_name, kwargs.get("email")))
        return {
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
            "message": "已登录",
        }

    monkeypatch.setattr(mercado_login, "login_mercado_with_saved_password", fake_login)
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("指定店铺任务不应关闭浏览器")
        ),
    )

    result = mercado_login.login_one_database_shop(
        {
            "shop_name": "指定店铺",
            "window_id": "window-selected",
            "email": "selected@example.com",
        },
        close_browser=False,
    )

    assert ("login", "指定店铺", "selected@example.com") in events
    assert result["browser_opened"] is True
    assert result["browser_closed"] is False
    assert result["browser_close_requested"] is False
    assert result["browser_kept_open"] is True
    assert "service_stopped" in events
    assert events[-1] == "lease_released"


def test_selected_auto_login_rechecks_closes_then_rewrites_status(monkeypatch):
    from bit import bit_api, bit_runtime_lock

    events = []

    class Lease:
        def acquire(self, timeout=0):
            events.append("lease_acquired")
            return True

        def release(self):
            events.append("lease_released")

    class Driver:
        service = None

    monkeypatch.setattr(bit_runtime_lock, "create_window_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(
        mercado_login,
        "_connect_to_open_bit_browser",
        lambda *args, **kwargs: Driver(),
    )
    monkeypatch.setattr(
        mercado_login,
        "login_mercado_with_saved_password",
        lambda *args, **kwargs: events.append("auto_login_completed")
        or {
            "ok": True,
            "status": mercado_login.LOGIN_SUCCESS,
            "message": "自动登录完成",
            "initial_login_status": mercado_login.INITIAL_LOGIN_INACTIVE,
            "program_login_result": mercado_login.PROGRAM_LOGIN_SUCCESS,
            "result_category": mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS,
            "login_stage": "logged_in",
            "action": "自动登录",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: events.append("login_verified")
        or {
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
            "login_stage": "logged_in",
        },
    )
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda window_id, lease=None: events.append("browser_closed")
        or {"success": True},
    )
    monkeypatch.setattr(
        mercado_login.bit_db_api,
        "resolve_window_anomaly",
        lambda window_id: events.append("status_rewritten"),
    )

    result = mercado_login.login_one_database_shop(
        {
            "shop_name": "指定店铺",
            "window_id": "window-selected",
            "email": "selected@example.com",
        },
        close_browser=False,
    )
    sync_result = mercado_login.sync_login_results_to_window_anomalies([result])

    assert result["ok"] is True
    assert result["recheck_status"] == "logged_in"
    assert result["browser_closed"] is True
    assert result["browser_close_requested"] is True
    assert sync_result["resolved_count"] == 1
    assert events.index("auto_login_completed") < events.index("login_verified")
    assert events.index("login_verified") < events.index("browser_closed")
    assert events.index("browser_closed") < events.index("status_rewritten")


def test_selected_auto_login_recheck_failure_keeps_browser_open(monkeypatch):
    from bit import bit_api, bit_runtime_lock

    class Lease:
        def acquire(self, timeout=0):
            return True

        def release(self):
            return None

    class Driver:
        service = None

    monkeypatch.setattr(bit_runtime_lock, "create_window_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(
        mercado_login,
        "_connect_to_open_bit_browser",
        lambda *args, **kwargs: Driver(),
    )
    monkeypatch.setattr(
        mercado_login,
        "login_mercado_with_saved_password",
        lambda *args, **kwargs: {
            "ok": True,
            "status": mercado_login.LOGIN_SUCCESS,
            "message": "自动登录完成",
            "initial_login_status": mercado_login.INITIAL_LOGIN_INACTIVE,
            "program_login_result": mercado_login.PROGRAM_LOGIN_SUCCESS,
            "result_category": mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS,
            "login_stage": "logged_in",
            "action": "自动登录",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "message": "仍停留在密码页",
            "login_stage": "password",
        },
    )
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda *args, **kwargs: pytest.fail("复检失败时应保留指定店铺浏览器"),
    )

    result = mercado_login.login_one_database_shop(
        {
            "shop_name": "指定店铺",
            "window_id": "window-selected",
            "email": "selected@example.com",
        },
        close_browser=False,
    )

    assert result["ok"] is False
    assert result["recheck_status"] == "password"
    assert result["browser_closed"] is False
    assert result["browser_kept_open"] is True


def test_captcha_is_recorded_without_manual_wait(monkeypatch):
    monkeypatch.setattr(
        mercado_login.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(
            AssertionError("人机验证不应等待人工处理")
        ),
    )

    result = mercado_login._wait_for_manual_login_after_failure(
        object(),
        "验证码店铺",
        {
            "ok": False,
            "status": mercado_login.LOGIN_CAPTCHA_REQUIRED,
            "message": "检测到人机验证",
            "login_stage": "captcha",
        },
        1200,
    )

    assert result["status"] == mercado_login.LOGIN_CAPTCHA_REQUIRED
    assert result["login_stage"] == "captcha"


def test_login_one_shop_waits_for_manual_login_before_closing(monkeypatch):
    from bit import bit_api, bit_runtime_lock

    events = []

    class Lease:
        def acquire(self, timeout=0):
            events.append("lease_acquired")
            return True

        def release(self):
            events.append("lease_released")

    class Driver:
        service = None

    monkeypatch.setattr(bit_runtime_lock, "create_window_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(
        mercado_login,
        "_connect_to_open_bit_browser",
        lambda *args, **kwargs: Driver(),
    )
    monkeypatch.setattr(
        mercado_login,
        "login_mercado_with_saved_password",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_VERIFICATION_REQUIRED,
            "message": "需要人工输入验证码",
            "login_stage": "verification",
        },
    )
    monkeypatch.setattr(
        mercado_login,
        "detect_login_stage",
        lambda driver: events.append("login_rechecked") or "logged_in",
    )
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: events.append("final_login_rechecked")
        or {"ok": True, "status": mercado_login.LOGIN_ALREADY_ACTIVE},
    )
    monkeypatch.setattr(
        mercado_login.time,
        "sleep",
        lambda seconds: events.append(("slept", seconds)),
    )
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda window_id, lease=None: events.append(("closed", window_id))
        or {"success": True},
    )

    result = mercado_login.login_one_database_shop(
        {
            "shop_name": "测试店铺",
            "window_id": "window-1",
            "email": "shop@example.com",
        },
        manual_login_wait_seconds=180,
    )

    assert result["ok"] is True
    assert result["program_login_result"] == mercado_login.PROGRAM_LOGIN_MANUAL_SUCCESS
    assert result["result_category"] == mercado_login.LOGIN_OUTCOME_MANUAL_LOGIN_SUCCESS
    assert result["action"] == "人工登录"
    assert result["recheck_status"] == "logged_in"
    assert events.index(("slept", 180)) < events.index("login_rechecked")
    assert events.index("login_rechecked") < events.index("final_login_rechecked")
    assert events.index("final_login_rechecked") < events.index(("closed", "window-1"))


def test_completed_auto_login_recheck_failure_keeps_latest_anomaly(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "message": "仍停留在密码页",
            "login_stage": "password",
        },
    )

    result = mercado_login._recheck_completed_login(
        object(),
        "测试店铺",
        "window-1",
        {
            "ok": True,
            "status": mercado_login.LOGIN_SUCCESS,
            "message": "自动登录完成",
            "initial_login_status": mercado_login.INITIAL_LOGIN_INACTIVE,
            "program_login_result": mercado_login.PROGRAM_LOGIN_SUCCESS,
            "result_category": mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS,
            "login_stage": "logged_in",
            "action": "自动登录",
        },
    )

    assert result["ok"] is False
    assert result["status"] == mercado_login.LOGIN_FAILED
    assert result["login_stage"] == "password"
    assert result["recheck_status"] == "password"
    assert "复检未通过" in result["message"]


def test_completed_auto_login_recheck_exception_becomes_latest_failure(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login_from_home",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("页面失联")),
    )

    result = mercado_login._recheck_completed_login(
        object(),
        "测试店铺",
        "window-1",
        {
            "ok": True,
            "status": mercado_login.LOGIN_SUCCESS,
            "initial_login_status": mercado_login.INITIAL_LOGIN_INACTIVE,
            "program_login_result": mercado_login.PROGRAM_LOGIN_SUCCESS,
            "result_category": mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS,
        },
    )

    assert result["ok"] is False
    assert result["recheck_status"] == "recheck_error"
    assert "页面失联" in result["message"]


def test_login_one_shop_closes_after_manual_login_wait_expires(monkeypatch):
    from bit import bit_api, bit_runtime_lock

    class Lease:
        def acquire(self, timeout=0):
            return True

        def release(self):
            return None

    class Driver:
        service = None

    sleeps = []
    monkeypatch.setattr(bit_runtime_lock, "create_window_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(
        mercado_login,
        "_connect_to_open_bit_browser",
        lambda *args, **kwargs: Driver(),
    )
    monkeypatch.setattr(
        mercado_login,
        "login_mercado_with_saved_password",
        lambda *args, **kwargs: {
            "ok": False,
            "status": mercado_login.LOGIN_SAVED_PASSWORD_INCORRECT,
            "message": "默认密码错误",
            "login_stage": "password",
        },
    )
    monkeypatch.setattr(mercado_login, "detect_login_stage", lambda driver: "password")
    monkeypatch.setattr(mercado_login.time, "sleep", sleeps.append)
    monkeypatch.setattr(bit_api, "closeBrowser", lambda *a, **k: {"success": True})

    result = mercado_login.login_one_database_shop(
        {"shop_name": "测试店铺", "window_id": "window-1"},
        manual_login_wait_seconds=180,
    )

    assert result["ok"] is False
    assert sleeps == [180]
    assert "等待人工登录 180 秒后仍未登录" in result["message"]
    assert result["browser_closed"] is True


def test_connect_reopens_window_when_debugger_port_never_becomes_ready(monkeypatch):
    import requests
    from selenium import webdriver
    from bit import bit_api

    open_calls = []
    close_calls = []
    debugger_calls = []

    def fake_open(window_id):
        open_calls.append(window_id)
        return {
            "success": True,
            "data": {
                "driver": "/tmp/fake-driver",
                "http": "127.0.0.1:9222",
            },
        }

    class DebuggerResponse:
        def raise_for_status(self):
            if len(debugger_calls) <= 6:
                raise requests.ConnectionError("debugger not ready")

    def fake_get(*args, **kwargs):
        debugger_calls.append(args[0])
        return DebuggerResponse()

    class Driver:
        def implicitly_wait(self, seconds):
            return None

        def set_page_load_timeout(self, seconds):
            return None

    monkeypatch.setattr(bit_api, "openBrowser", fake_open)
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda window_id: close_calls.append(window_id) or {"success": True},
    )
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(webdriver, "Chrome", lambda *args, **kwargs: Driver())
    monkeypatch.setattr(mercado_login.time, "sleep", lambda seconds: None)

    driver = mercado_login._connect_to_open_bit_browser("window-1")

    assert isinstance(driver, Driver)
    assert open_calls == ["window-1", "window-1"]
    assert close_calls == ["window-1"]
    assert len(debugger_calls) == 7


def test_connect_retries_when_open_api_times_out(monkeypatch):
    import requests
    from selenium import webdriver
    from bit import bit_api

    open_calls = []
    close_calls = []

    def fake_open(window_id):
        open_calls.append(window_id)
        if len(open_calls) == 1:
            raise requests.ReadTimeout("open timed out")
        return {
            "success": True,
            "data": {
                "driver": "/tmp/fake-driver",
                "http": "127.0.0.1:9222",
            },
        }

    class DebuggerResponse:
        def raise_for_status(self):
            return None

    class Driver:
        def implicitly_wait(self, seconds):
            return None

        def set_page_load_timeout(self, seconds):
            return None

    monkeypatch.setattr(bit_api, "openBrowser", fake_open)
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda window_id: close_calls.append(window_id) or {"success": True},
    )
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: DebuggerResponse())
    monkeypatch.setattr(webdriver, "Chrome", lambda *args, **kwargs: Driver())
    monkeypatch.setattr(mercado_login.time, "sleep", lambda seconds: None)

    driver = mercado_login._connect_to_open_bit_browser("window-1")

    assert isinstance(driver, Driver)
    assert open_calls == ["window-1", "window-1"]
    assert close_calls == []


def test_connect_keeps_window_when_open_api_reports_internal_timeout(monkeypatch):
    from selenium import webdriver
    from bit import bit_api

    open_calls = []
    close_calls = []

    def fake_open(window_id):
        open_calls.append(window_id)
        if len(open_calls) == 1:
            return {"success": False, "msg": "timeout of 30000ms exceeded"}
        return {
            "success": True,
            "data": {
                "driver": "/tmp/fake-driver",
                "http": "127.0.0.1:9222",
            },
        }

    class DebuggerResponse:
        def raise_for_status(self):
            return None

    class Driver:
        def implicitly_wait(self, seconds):
            return None

        def set_page_load_timeout(self, seconds):
            return None

    monkeypatch.setattr(bit_api, "openBrowser", fake_open)
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda window_id: close_calls.append(window_id) or {"success": True},
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: DebuggerResponse(),
    )
    monkeypatch.setattr(webdriver, "Chrome", lambda *args, **kwargs: Driver())
    monkeypatch.setattr(mercado_login.time, "sleep", lambda seconds: None)

    driver = mercado_login._connect_to_open_bit_browser("window-dragon")

    assert isinstance(driver, Driver)
    assert open_calls == ["window-dragon", "window-dragon"]
    assert close_calls == []


def test_login_one_shop_retries_close_while_browser_is_still_opening(monkeypatch):
    from bit import bit_api, bit_runtime_lock

    class Lease:
        acquired = False

        def acquire(self, timeout=0):
            self.acquired = True
            return True

        def release(self):
            self.acquired = False

    class Driver:
        service = None

    close_responses = [
        {"success": False, "msg": "浏览器正在打开中"},
        {"success": False, "msg": "浏览器正在打开中"},
        {"success": True},
    ]
    sleeps = []
    monkeypatch.setattr(bit_runtime_lock, "create_window_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(
        mercado_login,
        "_connect_to_open_bit_browser",
        lambda *args, **kwargs: Driver(),
    )
    monkeypatch.setattr(
        mercado_login,
        "login_mercado_with_saved_password",
        lambda *args, **kwargs: {
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
            "message": "已登录",
        },
    )
    monkeypatch.setattr(
        bit_api,
        "closeBrowser",
        lambda *args, **kwargs: close_responses.pop(0),
    )
    monkeypatch.setattr(mercado_login.time, "sleep", sleeps.append)

    result = mercado_login.login_one_database_shop(
        {"shop_name": "测试店铺", "window_id": "window-1"}
    )

    assert result["browser_closed"] is True
    assert result["close_error"] == ""
    assert sleeps == [5, 5]


def test_login_status_report_contains_formulas_filters_and_statuses(tmp_path):
    report_path = tmp_path / "login-report.xlsx"
    results = [
        {
            "shop_name": "成功店铺",
            "window_id": "window-1",
            "email": "success@example.com",
            "ok": True,
            "status": mercado_login.LOGIN_SUCCESS,
            "login_stage": "logged_in",
            "action": "自动登录",
            "message": "登录成功",
            "started_at": "2026-07-23 10:00:00",
            "ended_at": "2026-07-23 10:00:05",
            "duration_seconds": 5,
            "browser_opened": True,
            "browser_closed": True,
        },
        {
            "shop_name": "验证码店铺",
            "window_id": "window-2",
            "email": "verify@example.com",
            "ok": False,
            "status": mercado_login.LOGIN_VERIFICATION_REQUIRED,
            "login_stage": "verification",
            "action": "未登录",
            "message": "需要人工处理",
            "started_at": "2026-07-23 10:00:00",
            "ended_at": "2026-07-23 10:00:05",
            "duration_seconds": 5,
            "browser_opened": True,
            "browser_closed": False,
        },
    ]

    mercado_login.write_login_status_report(results, report_path)

    workbook = load_workbook(report_path, data_only=False)
    try:
        summary = workbook["汇总"]
        details = workbook["登录明细"]
        assert summary["B4"].value == "=COUNTA('登录明细'!B:B)-1"
        assert summary["B5"].value == (
            f'=COUNTIF(\'登录明细\'!G:G,"{mercado_login.LOGIN_OUTCOME_ALREADY_ACTIVE}")'
        )
        assert summary["B6"].value == (
            f'=COUNTIF(\'登录明细\'!G:G,"{mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS}")'
        )
        assert summary["B7"].value == (
            f'=COUNTIF(\'登录明细\'!G:G,"{mercado_login.LOGIN_OUTCOME_MANUAL_LOGIN_SUCCESS}")'
        )
        assert summary["A9"].value == "其中：浏览器未保存默认密码"
        assert summary["B9"].value == (
            f'=COUNTIF(\'登录明细\'!H:H,"{mercado_login.LOGIN_SAVED_PASSWORD_MISSING}")'
        )
        assert summary["A10"].value == "其中：浏览器默认密码错误"
        assert summary["B10"].value == (
            f'=COUNTIF(\'登录明细\'!H:H,"{mercado_login.LOGIN_SAVED_PASSWORD_INCORRECT}")'
        )
        assert details.freeze_panes == "A2"
        assert details.auto_filter.ref == "A1:P3"
        assert details["E2"].value == mercado_login.INITIAL_LOGIN_INACTIVE
        assert details["F2"].value == mercado_login.PROGRAM_LOGIN_SUCCESS
        assert (
            details["G2"].value
            == mercado_login.LOGIN_OUTCOME_AUTO_LOGIN_SUCCESS
        )
        assert details["H2"].value == mercado_login.LOGIN_SUCCESS
        assert (
            details["G3"].value
            == mercado_login.LOGIN_OUTCOME_VERIFICATION_REQUIRED
        )
        assert details["H3"].value == mercado_login.LOGIN_VERIFICATION_REQUIRED
        assert details["N3"].value == "失败"
    finally:
        workbook.close()


def test_login_status_email_counts_each_program_result_separately(monkeypatch, tmp_path):
    captured = {}

    def fake_send_info(subject, body, report_path, attachment_name):
        captured.update(
            subject=subject,
            body=body,
            report_path=report_path,
            attachment_name=attachment_name,
        )
        return True

    monkeypatch.setattr(mercado_login, "send_info", fake_send_info)
    report_path = tmp_path / "login-report.xlsx"
    results = [
        {"status": mercado_login.LOGIN_ALREADY_ACTIVE, "ok": True},
        {"status": mercado_login.LOGIN_SUCCESS, "ok": True},
        {"status": mercado_login.LOGIN_EMAIL_MISSING, "ok": False},
        {"status": mercado_login.LOGIN_VERIFICATION_REQUIRED, "ok": False},
        {"status": mercado_login.LOGIN_CAPTCHA_REQUIRED, "ok": False},
        {
            "status": mercado_login.LOGIN_WINDOW_BUSY,
            "ok": False,
            "action": "未执行",
        },
    ]

    assert mercado_login.send_login_status_report(results, report_path) is True
    assert "原本已登录：1" in captured["body"]
    assert "未登录，程序登录成功：1" in captured["body"]
    assert "未登录，程序登录失败：1" in captured["body"]
    assert "未登录，程序登录遇到验证码：1" in captured["body"]
    assert "未登录，程序登录遇到人机验证：1" in captured["body"]
    assert "未完成登录判断：1" in captured["body"]
    assert "（2/6）" in captured["subject"]


def test_all_database_shop_login_uses_three_processes_and_sends_one_report(
    monkeypatch, tmp_path
):
    class Future:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    class InlineExecutor:
        workers = None

        def __init__(self, max_workers):
            InlineExecutor.workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, function, *args):
            return Future(function(*args))

    configs = [
        {
            "shop_name": f"店铺{index}",
            "window_id": f"window-{index}",
            "email": f"shop{index}@example.com",
            "sequence_no": str(index),
        }
        for index in range(1, 5)
    ]
    include_ignored_values = []
    monkeypatch.setattr(
        mercado_login,
        "list_shop_configs",
        lambda include_ignored: include_ignored_values.append(include_ignored) or configs,
    )
    monkeypatch.setattr(mercado_login, "ProcessPoolExecutor", InlineExecutor)
    monkeypatch.setattr(mercado_login, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(
        mercado_login,
        "login_one_database_shop",
        lambda config, **kwargs: {
            **config,
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
            "browser_opened": True,
            "browser_closed": True,
        },
    )
    output = tmp_path / "summary.xlsx"
    monkeypatch.setattr(
        mercado_login,
        "write_login_status_report",
        lambda results, output_path=None: str(output_path),
    )
    sent = []
    monkeypatch.setattr(
        mercado_login,
        "send_login_status_report",
        lambda results, report_path: sent.append((len(results), report_path)) or True,
    )
    synced = []
    monkeypatch.setattr(
        mercado_login,
        "sync_login_results_to_window_anomalies",
        lambda results: synced.append(len(results)) or {
            "anomaly_count": 0,
            "resolved_count": len(results),
            "error_count": 0,
        },
    )

    result = mercado_login.run_all_database_shop_logins(output_path=output)

    assert include_ignored_values == [False]
    assert InlineExecutor.workers == 3
    assert result["shop_count"] == 4
    assert result["success_count"] == 4
    assert result["email_sent"] is True
    assert result["window_anomaly_sync"]["resolved_count"] == 4
    assert synced == [1, 1, 1, 1]
    assert sent == [(4, str(output))]


def test_database_shop_login_filters_selected_window_ids(monkeypatch, tmp_path):
    class Future:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    class InlineExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, function, *args):
            return Future(function(*args))

    configs = [
        {
            "shop_name": f"店铺{index}",
            "window_id": f"window-{index}",
            "email": f"shop{index}@example.com",
            "sequence_no": str(index),
        }
        for index in range(1, 4)
    ]
    monkeypatch.setattr(
        mercado_login,
        "list_shop_configs",
        lambda include_ignored: configs,
    )
    monkeypatch.setattr(mercado_login, "ProcessPoolExecutor", InlineExecutor)
    monkeypatch.setattr(mercado_login, "as_completed", lambda futures: list(futures))
    processed = []
    monkeypatch.setattr(
        mercado_login,
        "login_one_database_shop",
        lambda config, **kwargs: processed.append(
            (
                config["window_id"],
                kwargs["manual_login_wait_seconds"],
                kwargs["close_browser"],
            )
        ) or {
            **config,
            "ok": True,
            "status": mercado_login.LOGIN_ALREADY_ACTIVE,
            "browser_opened": True,
            "browser_closed": True,
        },
    )
    output = tmp_path / "selected-summary.xlsx"
    monkeypatch.setattr(
        mercado_login,
        "write_login_status_report",
        lambda results, output_path=None: str(output_path),
    )
    monkeypatch.setattr(
        mercado_login,
        "sync_login_results_to_window_anomalies",
        lambda results: {"resolved_count": len(results)},
    )

    result = mercado_login.run_all_database_shop_logins(
        output_path=output,
        send_email=False,
        window_ids=["window-3", "window-1", "window-3"],
        manual_login_wait_seconds=180,
        close_browsers=False,
    )

    assert processed == [
        ("window-1", 180, False),
        ("window-3", 180, False),
    ]
    assert result["shop_count"] == 2
    assert result["email_sent"] is False


def test_database_shop_login_rejects_missing_selected_window(monkeypatch):
    monkeypatch.setattr(
        mercado_login,
        "list_shop_configs",
        lambda include_ignored: [
            {"shop_name": "店铺1", "window_id": "window-1", "email": "a@example.com"}
        ],
    )

    with pytest.raises(ValueError, match="window-missing"):
        mercado_login.run_all_database_shop_logins(
            send_email=False,
            window_ids=["window-1", "window-missing"],
        )
