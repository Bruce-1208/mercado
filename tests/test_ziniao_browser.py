from unittest.mock import Mock

import pytest
from bit import ziniao_browser as zn, bit_api, bit_config, bit_daily_task


def test_list_and_unambiguous_authorization_matching(monkeypatch):
    monkeypatch.setattr(zn, "request", lambda action: {"browserList": [
        {"browserOauth": "opaque-a", "browserName": "店铺甲（MX）"},
        {"browserOauth": "opaque-b", "browserName": "店铺乙"},
    ]})
    rows = zn.list_browsers()
    assert bit_config._resolve_authorized_window_id({"display_name": "店铺甲MX"}, rows) == "ziniao:opaque-a"
    with pytest.raises(RuntimeError, match="多个"):
        zn.resolve_aliases(["店铺甲MX", "店铺乙"], rows)
    with pytest.raises(RuntimeError, match="未找到"):
        zn.resolve_aliases(["店铺丙"], rows)
    with pytest.raises(RuntimeError, match="多个"):
        zn.resolve_aliases(["店铺乙"], rows + [rows[1]])


def test_namespaced_open_close_never_call_bitbrowser(monkeypatch, tmp_path):
    driver = tmp_path / "webdriver"
    driver.touch()
    calls = []
    def request(action, **kw):
        calls.append((action, kw))
        return {"debuggingPort": 9911, "browserPath": str(tmp_path), "ipDetectionPage": "http://offline/check"}
    monkeypatch.setattr(zn, "request", request)
    monkeypatch.delenv("ZINIAO_DRIVER_PATH", raising=False)
    monkeypatch.setattr(bit_api, "current_thread_window_lease", lambda _: Mock(acquired=True))
    monkeypatch.setattr(bit_api, "_post_browser_mutation", lambda *a, **kw: pytest.fail("called BitBrowser"))
    monkeypatch.setattr(bit_api.lifecycle, "ensure_browser_reaper", lambda: pytest.fail("started BitBrowser reaper"))
    opened = bit_api.openBrowser("ziniao:opaque-id")
    assert opened["data"]["http"] == "127.0.0.1:9911"
    assert opened["data"]["driver"] == str(driver)
    assert bit_api.closeBrowser("ziniao:opaque-id")["success"]
    assert [c[0] for c in calls] == ["startBrowser", "stopBrowser"]
    assert all(c[1]["browserOauth"] == "opaque-id" for c in calls)


def test_incomplete_open_closes_only_target(monkeypatch):
    calls = []
    def request(action, **kw):
        calls.append((action, kw))
        return {"debuggingPort": 0}
    monkeypatch.setattr(zn, "request", request)
    with pytest.raises(RuntimeError, match="调试端口"):
        zn.open_browser("ziniao:a")
    assert calls[-1][0] == "stopBrowser"
    assert calls[-1][1]["browserOauth"] == "a"


def test_credentials_and_local_transport(monkeypatch):
    for name in ("COMPANY", "USERNAME", "PASSWORD"):
        monkeypatch.setenv("ZINIAO_" + name, "private-" + name)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.post.return_value.json.return_value = {"statusCode": 0, "browserList": []}
    monkeypatch.setattr(zn.requests, "Session", lambda: session)
    assert zn.list_browsers() == []
    assert session.trust_env is False
    assert session.post.call_args.args[0].startswith("http://127.0.0.1:")
    session.post.return_value.json.return_value = {"statusCode": -1, "password": "private-PASSWORD"}
    with pytest.raises(RuntimeError) as exc:
        zn.list_browsers()
    assert "private" not in str(exc.value)
    monkeypatch.delenv("ZINIAO_PASSWORD")
    with pytest.raises(RuntimeError, match="ZINIAO_PASSWORD"):
        zn.list_browsers()


def test_task_snapshot_selects_ziniao_without_bit_fallback(monkeypatch):
    monkeypatch.setattr(zn, "list_browsers", lambda: [{"id": "ziniao:a", "name": "店铺甲"}])
    monkeypatch.setattr(bit_daily_task, "_load_bit_browser_snapshot", lambda *a: pytest.fail("BitBrowser fallback"))
    monkeypatch.setattr(bit_daily_task, "list_mercado_store_tokens", lambda: {})
    def resolve(name, **kw):
        return zn.resolve_aliases([name], kw["browsers"])
    monkeypatch.setattr(bit_config, "get_window_id_by_shop_name", resolve)
    result = bit_daily_task._resolve_appeal_plan_window_ids([{"name": "店铺甲"}, {"name": "不存在"}], browser_provider="ziniao")
    assert result[0]["window_id"] == "ziniao:a"
    assert result[1]["window_id"] == ""
    assert result[1]["window_lookup_error"]


def test_ip_check_required():
    with pytest.raises(RuntimeError, match="IP 检测页"):
        zn.check_ip(Mock(), "")


@pytest.mark.parametrize("mode,strategy,types", [
    ("once", "normal", ["侵权"]),
    ("loop", "normal", ["混合模式"]),
    ("once", "algorithm", ["侵权", "投诉"]),
])
def test_provider_reaches_every_round_without_real_actions(monkeypatch, mode, strategy, types):
    seen = []
    monkeypatch.setattr(bit_daily_task, "build_appeal_plan", lambda *a, **kw: [{"name": "模拟店铺"}])
    def resolve(plan, **kw):
        seen.append(kw["browser_provider"])
        return None  # Stop before any worker or business action is created.
    monkeypatch.setattr(bit_daily_task, "_resolve_appeal_plan_window_ids", resolve)
    fn = bit_daily_task.loop_ai_appeal if mode == "loop" else bit_daily_task.run_ai_appeal_once
    fn(types, browser_provider="ziniao", task_strategy=strategy,
       _task_lock=Mock(acquired=True), **({"max_rounds": 1} if mode == "loop" else {}))
    assert seen == ["ziniao"] * (6 if types == ["混合模式"] else 1)


def test_web_params_and_execution_forward_provider(monkeypatch, tmp_path):
    import threading
    from bit import bit_interface as web
    monkeypatch.setattr(web, "_request_execution_target", lambda data: "agent")
    monkeypatch.setattr(web, "get_current_workbench_user", lambda: {"role_key": "super_admin"})
    with web.app.test_request_context('/'):
        params = web.build_daily_task_params({"browser_provider": "ziniao", "mode": "once"})
        assert params["browser_provider"] == "ziniao"
        assert web.build_daily_task_params({})["browser_provider"] == "bitbrowser"
        with pytest.raises(ValueError):
            web.build_daily_task_params({"browser_provider": "unknown"})
    run = Mock(return_value=[])
    monkeypatch.setattr(bit_daily_task, "run_ai_appeal_once", run)
    web.execute_daily_task(params, Mock(acquired=True), threading.Event(), 'offline', tmp_path/'task.log', {})
    assert run.call_args.kwargs["browser_provider"] == "ziniao"
    assert "紫鸟" in web._daily_task_display_name(params)
