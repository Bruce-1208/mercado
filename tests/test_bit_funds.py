from pathlib import Path

from bit import bit_interface, bit_mercado_login, bit_mysql, bit_pago_info


def test_pago_all_jobs_can_bypass_default_shop_limit_and_use_configured_sites(monkeypatch):
    monkeypatch.setattr(
        bit_pago_info,
        "list_config_rows",
        lambda include_ignored=False: [
            ("window-1", "春风得意", "", "墨西哥,巴西", "1", "张三", ""),
            ("window-2", "龙凤呈祥", "", "哥伦比亚/智利", "2", "李四", ""),
        ],
    )
    monkeypatch.setattr(bit_pago_info, "_get_shop_limit", lambda: 1)

    limited_jobs, limit = bit_pago_info._build_pago_collection_jobs(apply_shop_limit=True)
    all_jobs, all_limit = bit_pago_info._build_pago_collection_jobs(apply_shop_limit=False)
    owner_jobs, _ = bit_pago_info._build_pago_collection_jobs(
        apply_shop_limit=False,
        salesperson="李四",
    )

    assert limit == 1
    assert len(limited_jobs) == 1
    assert all_limit is None
    assert [job[0][1] for job in all_jobs] == ["春风得意", "龙凤呈祥"]
    assert all_jobs[0][1] == ["墨西哥", "巴西"]
    assert [job[0][1] for job in owner_jobs] == ["龙凤呈祥"]
    assert "阿根廷" not in all_jobs[0][1]
    assert "乌拉圭" not in all_jobs[0][1]


def test_pago_single_shop_refreshes_every_configured_site_only(monkeypatch):
    monkeypatch.setattr(
        bit_pago_info,
        "list_config_rows",
        lambda include_ignored=False: [
            ("window-1", "春风得意", "", "墨西哥,巴西", "1", "张三", ""),
        ],
    )
    captured = {}

    def fake_collect(jobs, **kwargs):
        row, sites = jobs[0]
        captured["row"] = row
        captured["sites"] = sites
        return [
            ["春风得意", site, "10.00", "2.00", "成功", "now", ""]
            for site in sites
        ], [("获取款项信息", "春风得意", site, "成功", "now") for site in sites]

    inserted = []
    task_records = []
    monkeypatch.setattr(bit_pago_info, "_collect_pago_jobs", fake_collect)
    monkeypatch.setattr(bit_pago_info, "_safe_insert_pago_info", inserted.extend)
    monkeypatch.setattr(bit_pago_info, "_safe_insert_task_record", task_records.extend)

    rows = bit_pago_info.get_pago_info_for_shop(
        shop_name="春风得意",
        window_id="window-1",
    )

    assert captured["sites"] == ["墨西哥", "巴西"]
    assert "阿根廷" not in captured["sites"]
    assert "乌拉圭" not in captured["sites"]
    assert rows == inserted
    assert len(task_records) == 2


def test_latest_pago_info_joins_owner_and_configured_sites(monkeypatch):
    monkeypatch.setattr(
        bit_mysql,
        "list_mercado_store_tokens",
        lambda: {
            "rows": [{
                "id": 1,
                "display_name": "春风得意",
                "site_settings": [
                    {"site_id": "MLM", "salesperson": "张三"},
                    {"site_id": "MLB", "salesperson": "张三"},
                ],
            }],
        },
    )

    class FakeCursor:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return [
                {
                    "店铺名": "春风得意",
                    "站点": "墨西哥",
                    "已释放美元": "US$ 1,200.50",
                    "未释放美元": "US$ 20.25",
                    "状态": "成功",
                    "更新时间": "2026-07-27 10:00:00",
                    "提交时间": "2026-07-27 10:01:00",
                }
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: FakeConnection())

    data = bit_mysql.get_latest_pago_info("张三")

    assert data["shop_total"] == 1
    assert data["released_total"] == "1,200.50"
    assert data["pending_total"] == "20.25"
    assert [row["站点"] for row in data["rows"]] == ["墨西哥", "巴西"]
    assert data["rows"][0]["店铺归属人"] == "张三"
    assert data["rows"][1]["状态"] == "无数据"


def test_funds_api_supports_owner_filter(monkeypatch):
    captured = []
    monkeypatch.setattr(
        bit_interface,
        "db_get_latest_pago_info",
        lambda salesperson="": captured.append(salesperson) or {"rows": []},
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.get("/api/funds/latest?salesperson=%E5%BC%A0%E4%B8%89")

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
    assert captured == ["张三"]
    assert response.headers["Cache-Control"] == "no-store"


def test_funds_single_shop_api_starts_background_refresh(monkeypatch):
    started = []
    monkeypatch.setattr(
        bit_interface,
        "list_shop_configs",
        lambda include_ignored=False: [
            {
                "shop_name": "春风得意",
                "window_id": "window-1",
                "sites": "墨西哥,巴西",
                "salesperson": "张三",
            },
        ],
    )

    class FakeThread:
        def __init__(self, target, kwargs, daemon):
            started.append({"target": target, "kwargs": kwargs, "daemon": daemon})

        def start(self):
            started[-1]["started"] = True

    monkeypatch.setattr(bit_interface.threading, "Thread", FakeThread)
    with bit_interface._fund_collect_lock:
        previous_state = dict(bit_interface._fund_collect_state)
        previous_event = bit_interface._fund_collect_stop_event
        bit_interface._fund_collect_state.update({"running": False, "status": "idle"})
        bit_interface._fund_collect_stop_event = None
    try:
        client = bit_interface.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["workbench_user"] = {"username": "tester"}
        response = client.post(
            "/api/funds/collect",
            json={
                "all_shops": False,
                "window_ids": ["window-1"],
            },
        )
    finally:
        with bit_interface._fund_collect_lock:
            bit_interface._fund_collect_state.clear()
            bit_interface._fund_collect_state.update(previous_state)
            bit_interface._fund_collect_stop_event = previous_event

    assert response.status_code == 200
    assert started[0]["started"] is True
    assert started[0]["kwargs"]["all_shops"] is False
    assert started[0]["kwargs"]["selected_window_ids"] == ("window-1",)
    assert started[0]["kwargs"]["salesperson"] == ""
    assert started[0]["kwargs"]["max_workers"] == 10
    assert not started[0]["kwargs"]["stop_event"].is_set()


def test_funds_all_shops_is_limited_to_selected_salesperson(monkeypatch):
    started = []
    monkeypatch.setattr(
        bit_interface,
        "list_shop_configs",
        lambda include_ignored=False: [
            {"shop_name": "春风得意", "window_id": "window-1", "salesperson": "张三"},
            {"shop_name": "龙凤呈祥", "window_id": "window-2", "salesperson": "李四"},
        ],
    )

    class FakeThread:
        def __init__(self, target, kwargs, daemon):
            started.append({"target": target, "kwargs": kwargs, "daemon": daemon})

        def start(self):
            started[-1]["started"] = True

    monkeypatch.setattr(bit_interface.threading, "Thread", FakeThread)
    with bit_interface._fund_collect_lock:
        previous_state = dict(bit_interface._fund_collect_state)
        previous_event = bit_interface._fund_collect_stop_event
        bit_interface._fund_collect_state.update({"running": False, "status": "idle"})
        bit_interface._fund_collect_stop_event = None
    try:
        client = bit_interface.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["workbench_user"] = {"username": "tester"}
        response = client.post(
            "/api/funds/collect",
            json={"all_shops": True, "salesperson": "张三"},
        )
    finally:
        with bit_interface._fund_collect_lock:
            bit_interface._fund_collect_state.clear()
            bit_interface._fund_collect_state.update(previous_state)
            bit_interface._fund_collect_stop_event = previous_event

    assert response.status_code == 200
    assert started[0]["kwargs"]["all_shops"] is True
    assert started[0]["kwargs"]["selected_window_ids"] == ()
    assert started[0]["kwargs"]["salesperson"] == "张三"
    assert response.get_json()["data"]["target_count"] == 1


def test_funds_collection_can_be_stopped(monkeypatch):
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    stop_event = bit_interface.threading.Event()
    with bit_interface._fund_collect_lock:
        previous_state = dict(bit_interface._fund_collect_state)
        previous_event = bit_interface._fund_collect_stop_event
        bit_interface._fund_collect_state.update({"running": True, "status": "running"})
        bit_interface._fund_collect_stop_event = stop_event
    try:
        response = client.post("/api/funds/collect/stop", json={})
        state = response.get_json()["data"]
    finally:
        with bit_interface._fund_collect_lock:
            bit_interface._fund_collect_state.clear()
            bit_interface._fund_collect_state.update(previous_state)
            bit_interface._fund_collect_stop_event = previous_event

    assert response.status_code == 200
    assert stop_event.is_set()
    assert state["status"] == "stopping"


def test_pago_login_expiry_runs_existing_auto_login_then_retries(monkeypatch):
    row = ("window-1", "春风得意", "", "墨西哥,巴西", "1", "张三", "mail@example.com")
    outcomes = {
        ("window-1", "春风得意"): (
            row,
            [["春风得意", "墨西哥", "", "", "未登录", "now", ""]],
            [("获取款项信息", "春风得意", "墨西哥", "失败：登录失效", "now")],
        )
    }
    login_configs = []
    retried_sites = []

    monkeypatch.setattr(
        bit_mercado_login,
        "login_one_database_shop",
        lambda config, **kwargs: login_configs.append(config) or {"ok": True},
    )

    def fake_retry(retry_row, sites, stop_event=None):
        retried_sites.extend(sites)
        return (
            [["春风得意", site, "10", "2", "成功", "now", ""] for site in sites],
            [("获取款项信息", "春风得意", site, "成功", "now") for site in sites],
        )

    monkeypatch.setattr(bit_pago_info, "_run_pago_for_browser", fake_retry)
    repaired = bit_pago_info._repair_pago_logins(outcomes)

    assert login_configs[0]["window_id"] == "window-1"
    assert retried_sites == ["墨西哥", "巴西"]
    assert all(item[3] == "成功" for item in repaired[("window-1", "春风得意")][2])


def test_funds_ui_contains_row_multi_select_owner_scope_stop_and_single_refresh():
    template = (
        Path(bit_interface.CURRENT_DIR) / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'data-tab="funds"' in template
    assert 'id="fund-all-toggle"' in template
    assert 'id="fund-owner-filter"' in template
    assert 'id="fund-select-all"' in template
    assert 'class="fund-row-select"' in template
    assert 'id="stop-funds-btn"' in template
    assert 'selectedFundWindowIds' in template
    assert 'salesperson: String(fundOwnerFilter?.value || "")' in template
    assert 'class="secondary fund-refresh-action"' in template
    assert 'fetch("/api/funds/collect"' in template
    assert 'fetch("/api/funds/collect/stop"' in template
