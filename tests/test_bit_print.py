from pathlib import Path
from unittest import mock

from bit import bit_interface, bit_print


def test_build_print_jobs_filters_shop_and_configured_site_intersection():
    rows = [
        ("window-1", "店铺甲", "", "墨西哥，巴西", "", "", ""),
        ("window-2", "店铺乙", "", "巴西/智利", "", "", ""),
        ("window-2", "店铺乙", "", "智利", "", "", ""),
    ]

    jobs = bit_print.build_print_jobs(
        rows,
        selected_shops=["店铺乙"],
        selected_sites=["墨西哥", "智利"],
    )

    assert jobs == [
        {"window_id": "window-2", "shop_name": "店铺乙", "sites": ["智利"]}
    ]


def test_build_print_jobs_supports_exact_shop_site_targets_without_cross_product():
    rows = [
        ("window-1", "店铺甲", "", "墨西哥,巴西", "", "", ""),
        ("window-2", "店铺乙", "", "墨西哥,巴西", "", "", ""),
    ]

    jobs = bit_print.build_print_jobs(
        rows,
        selected_targets=[
            {"shop_name": "店铺甲", "site": "墨西哥"},
            {"shop_name": "店铺乙", "site": "巴西"},
        ],
    )

    assert jobs == [
        {"window_id": "window-1", "shop_name": "店铺甲", "sites": ["墨西哥"]},
        {"window_id": "window-2", "shop_name": "店铺乙", "sites": ["巴西"]},
    ]


def test_print_round_returns_structured_summary_and_persists_valid_records(monkeypatch):
    monkeypatch.setattr(
        bit_print,
        "list_config_rows",
        lambda include_ignored=False: [
            ("window-1", "店铺甲", "", "墨西哥,巴西", "", "", ""),
            ("window-2", "店铺乙", "", "智利", "", "", ""),
        ],
    )
    captured_jobs = []

    def fake_run_shop(job, **_kwargs):
        captured_jobs.append(job)
        return [
            bit_print._result_row(job["shop_name"], "巴西", "printed", "已提交", 1, 4),
            bit_print._result_row(job["shop_name"], "墨西哥", "no_orders", "无订单", 1, 0),
        ]

    records = []
    monkeypatch.setattr(bit_print, "_run_shop_job", fake_run_shop)
    monkeypatch.setattr(bit_print, "insert_task_record", records.extend)

    summary = bit_print.print_orders_all(
        selected_shops=["店铺甲"],
        selected_sites=["墨西哥", "巴西"],
        logger=lambda _message: None,
    )

    assert captured_jobs == [
        {
            "window_id": "window-1",
            "shop_name": "店铺甲",
            "sites": ["墨西哥", "巴西"],
        }
    ]
    assert summary["printed"] == 1
    assert summary["no_orders"] == 1
    assert summary["failed"] == 0
    assert len(records) == 2
    assert all(len(record) == 5 for record in records)
    assert records[0][:4] == ("后台打印订单", "店铺甲", "巴西", "成功")


def test_busy_browser_is_reported_as_skipped_without_opening(monkeypatch):
    class BusyLease:
        key = "bit_window_window-1"

        def acquire(self, timeout=0):
            return False

    monkeypatch.setattr(bit_print, "create_window_lease", lambda *args, **kwargs: BusyLease())
    monkeypatch.setattr(
        bit_print,
        "get_lock_owner",
        lambda _key: {"owner": "bit_reputation_info"},
    )
    open_browser = mock.Mock()
    monkeypatch.setattr(bit_print, "openBrowser", open_browser)

    results = bit_print._run_shop_job(
        {"window_id": "window-1", "shop_name": "店铺甲", "sites": ["墨西哥", "巴西"]},
        logger=lambda _message: None,
    )

    assert [result["status"] for result in results] == ["skipped", "skipped"]
    assert all("bit_reputation_info" in result["message"] for result in results)
    open_browser.assert_not_called()


def test_missing_order_controls_are_not_misreported_as_no_orders(monkeypatch):
    class Body:
        text = "Orders page temporarily unavailable"

    class Driver:
        def get(self, _url):
            pass

        def find_element(self, by, value):
            return Body()

    monkeypatch.setattr(bit_print, "_wait_for_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(bit_print, "_switch_site", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bit_print,
        "_find_clickable",
        mock.Mock(side_effect=RuntimeError("selector changed")),
    )

    with mock.patch.object(bit_print, "_interruptible_wait", return_value=False):
        try:
            bit_print._print_current_site(Driver(), "墨西哥", settle_seconds=0)
        except RuntimeError as exc:
            assert "未找到订单全选控件" in str(exc)
        else:
            raise AssertionError("页面结构异常不应被当成无订单")


def test_order_print_service_api_starts_and_stops_background_task(monkeypatch):
    started_threads = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            started_threads.append(self)

    task_lock = mock.Mock()
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=False: [
            {
                "window_id": "window-1",
                "shop_name": "店铺甲",
                "sites": "墨西哥,巴西",
                "salesperson": "张三",
            }
        ],
    )
    monkeypatch.setattr(bit_interface.bit_print, "acquire_order_print_lock", lambda **_kwargs: task_lock)
    monkeypatch.setattr(bit_interface.threading, "Thread", FakeThread)

    with bit_interface._order_print_lock:
        previous_state = dict(bit_interface._order_print_state)
        previous_stop_event = bit_interface._order_print_stop_event
        previous_logs = list(bit_interface._order_print_logs)
        bit_interface._order_print_state.update({"running": False, "status": "idle"})
        bit_interface._order_print_stop_event = None
        bit_interface._order_print_logs.clear()
    try:
        client = bit_interface.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["workbench_user"] = {"username": "tester"}

        start_response = client.post(
            "/api/order-print/start",
            json={
                "shops": ["店铺甲"],
                "sites": ["巴西"],
                # 兼容旧客户端字段，但服务端必须固定为单次执行。
                "mode": "loop",
                "interval_minutes": 120,
                "max_retries": 2,
                "retry_delay_seconds": 30,
            },
        )
        stop_response = client.post("/api/order-print/stop")
    finally:
        with bit_interface._order_print_lock:
            bit_interface._order_print_state.clear()
            bit_interface._order_print_state.update(previous_state)
            bit_interface._order_print_stop_event = previous_stop_event
            bit_interface._order_print_logs.clear()
            bit_interface._order_print_logs.extend(previous_logs)

    assert start_response.status_code == 200
    assert start_response.get_json()["data"]["params"]["selected_shops"] == ["店铺甲"]
    assert start_response.get_json()["data"]["params"]["selected_sites"] == ["巴西"]
    assert len(started_threads) == 1
    assert started_threads[0].target is bit_interface.run_order_print_job
    assert started_threads[0].args[0]["mode"] == "once"
    assert "interval_minutes" not in started_threads[0].args[0]
    assert stop_response.status_code == 200
    assert started_threads[0].args[2].is_set() is True


def test_order_print_job_runs_only_once_even_with_legacy_loop_param(monkeypatch):
    summaries = []
    task_lock = mock.Mock()
    stop_event = bit_interface.threading.Event()
    monkeypatch.setattr(
        bit_interface.bit_print,
        "print_orders_all",
        lambda **kwargs: summaries.append(kwargs) or {
            "printed": 0,
            "no_orders": 1,
            "failed": 0,
            "skipped": 0,
            "results": [],
        },
    )
    monkeypatch.setattr(
        bit_interface,
        "_load_order_print_site_last_runs",
        lambda _results=None: [],
    )

    with bit_interface._order_print_lock:
        previous_state = dict(bit_interface._order_print_state)
        previous_stop_event = bit_interface._order_print_stop_event
        bit_interface._order_print_state.update({"running": True, "status": "running"})
        bit_interface._order_print_stop_event = stop_event
    try:
        bit_interface.run_order_print_job(
            {
                "mode": "loop",
                "selected_shops": ["店铺甲"],
                "selected_sites": ["墨西哥"],
                "selected_targets": [
                    {"shop_name": "店铺甲", "site": "墨西哥"}
                ],
                "max_retries": 1,
                "retry_delay_seconds": 0,
            },
            task_lock,
            stop_event,
        )
    finally:
        with bit_interface._order_print_lock:
            bit_interface._order_print_state.clear()
            bit_interface._order_print_state.update(previous_state)
            bit_interface._order_print_stop_event = previous_stop_event

    assert len(summaries) == 1
    assert summaries[0]["selected_targets"] == [
        {"shop_name": "店铺甲", "site": "墨西哥"}
    ]
    assert task_lock.release.call_count == 1


def test_order_print_status_lists_every_configured_site_with_last_run(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=False: [
            {
                "window_id": "window-1",
                "shop_name": "店铺甲",
                "sites": "墨西哥,巴西",
            },
            {
                "window_id": "window-2",
                "shop_name": "店铺乙",
                "sites": "智利",
            },
        ],
    )
    monkeypatch.setattr(
        bit_interface,
        "db_get_latest_order_print_records",
        lambda: [
            {
                "shop_name": "店铺甲",
                "site": "巴西",
                "outcome": "成功",
                "finished_at": "2026-07-28 10:20:30",
            }
        ],
    )
    monkeypatch.setattr(
        bit_interface.bit_print,
        "get_order_print_lock_owner",
        lambda: None,
    )

    with bit_interface._order_print_lock:
        previous_state = dict(bit_interface._order_print_state)
        bit_interface._order_print_state.update(
            {"running": False, "results": [], "site_last_runs": []}
        )
    try:
        client = bit_interface.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["workbench_user"] = {"username": "tester"}
        response = client.get("/api/order-print/status")
    finally:
        with bit_interface._order_print_lock:
            bit_interface._order_print_state.clear()
            bit_interface._order_print_state.update(previous_state)

    assert response.status_code == 200
    rows = response.get_json()["data"]["site_last_runs"]
    assert [(row["shop_name"], row["site"]) for row in rows] == [
        ("店铺甲", "墨西哥"),
        ("店铺甲", "巴西"),
        ("店铺乙", "智利"),
    ]
    assert rows[0]["finished_at"] == ""
    assert rows[1]["finished_at"] == "2026-07-28 10:20:30"


def test_order_print_page_is_single_run_and_shows_all_site_last_run_times():
    template = (
        Path(bit_interface.__file__).resolve().parent / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="order-print-mode"' not in template
    assert 'id="order-print-interval"' not in template
    assert "定时循环执行" not in template
    assert "所有店铺和站点" in template
    assert 'id="order-print-site-run-body"' in template
    assert "data.site_last_runs" in template


def test_order_print_page_can_select_exact_shop_sites_and_rerun():
    template = (
        Path(bit_interface.__file__).resolve().parent / "templates" / "index.html"
    ).read_text(encoding="utf-8")

    assert "所有店铺和站点" in template
    assert 'id="order-print-site-run-all" type="checkbox"' in template
    assert 'id="rerun-selected-order-print-btn"' in template
    assert "toggleOrderPrintSiteTarget" in template
    assert "rerunSelectedOrderPrintTargets" in template
    assert "JSON.stringify(requestPayload)" in template


def test_order_print_params_validate_and_preserve_exact_shop_site_targets(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=False: [
            {
                "window_id": "window-1",
                "shop_name": "店铺甲",
                "sites": "墨西哥,巴西",
            },
            {
                "window_id": "window-2",
                "shop_name": "店铺乙",
                "sites": "墨西哥,巴西",
            },
        ],
    )

    params = bit_interface.build_order_print_params(
        {
            "targets": [
                {"shop_name": "店铺甲", "site": "墨西哥"},
                {"shop_name": "店铺乙", "site": "巴西"},
            ]
        }
    )

    assert params["selected_targets"] == [
        {"shop_name": "店铺甲", "site": "墨西哥"},
        {"shop_name": "店铺乙", "site": "巴西"},
    ]
    assert params["selected_shops"] == ("店铺甲", "店铺乙")
    assert params["selected_sites"] == ("墨西哥", "巴西")
    assert params["target"] == "2 家店铺 / 2 个店铺站点"


def test_order_print_service_rejects_unconfigured_site(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "db_list_bit_browser_configs",
        lambda include_ignored=False: [
            {"window_id": "window-1", "shop_name": "店铺甲", "sites": "墨西哥"}
        ],
    )
    client = bit_interface.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {"username": "tester"}

    response = client.post(
        "/api/order-print/start",
        json={"shops": ["店铺甲"], "sites": ["巴西"]},
    )

    assert response.status_code == 400
    assert "站点不存在" in response.get_json()["message"]
