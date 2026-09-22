"""Regressions from the 2026-09-21/22 Agent logs; no live submissions."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests

from bit import bit_appeal_ai as ai, bit_daily_task as daily, bit_db_api as api
from bit.local_agent_hub import LocalAgentStore


def test_client_worker_import_does_not_require_server_dbutils(tmp_path):
    script = '''
import importlib.abc, sys
class NoDBUtils(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "dbutils" or fullname.startswith("dbutils."):
            raise ModuleNotFoundError("Agent does not ship dbutils")
sys.meta_path.insert(0, NoDBUtils())
import requests
def no_network(*args, **kwargs):
    raise AssertionError("Agent import must not access business APIs")
requests.Session.request = no_network
from bit import bit_interface
assert bit_interface.RUNTIME_SETTINGS.is_client
bit_interface.close_all_pools()
assert "dbutils" not in sys.modules
'''
    env = dict(os.environ, BIT_RUNTIME_ROLE="client", BIT_EXECUTION_TARGET="agent",
               BIT_BACKGROUND_SERVICES_DISABLED="1",
               AI_WEIGHT_PRICE_DATA_DIR=str(tmp_path / "ai-weight-price"),
               BIT_DB_API_BASE_URL="https://example.invalid", WORKBENCH_SECRET_KEY="test-secret")
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def response(status, payload=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode() if payload is not None else b"<html>Bad gateway</html>"
    result._content_consumed = True
    result.headers["Content-Type"] = "application/json" if payload is not None else "text/html"
    return result


@pytest.mark.parametrize("failure", [500, 502, 503, 504, requests.ReadTimeout("offline")])
def test_read_recovers_transient_failure(monkeypatch, failure):
    calls, sleeps = [], []

    def request(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return response(failure)
        return response(200, {"status": "success", "data": {"rows": []}})

    monkeypatch.setattr(api.DB_API_SESSION, "request", request)
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    assert api._request("GET", "/api/db/mercado-tokens") == {"rows": []}
    assert len(calls) == 2
    assert sleeps == [1]


@pytest.mark.parametrize("method,expected", [("GET", 3), ("POST", 1)])
def test_retries_are_bounded_and_never_replay_writes(monkeypatch, method, expected):
    calls = []
    monkeypatch.setattr(api.DB_API_SESSION, "request", lambda *a, **k: calls.append(a) or response(502))
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="502"):
        api._request(method, "/api/db/official-infractions/live/jobs")
    assert len(calls) == expected


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_terminal_even_with_html_body(monkeypatch, status):
    calls = []
    monkeypatch.setattr(api.DB_API_SESSION, "request", lambda *a, **k: calls.append(a) or response(status))
    monkeypatch.setattr(api.time, "sleep", lambda _: pytest.fail("auth must not retry"))
    with pytest.raises(api.DatabaseAPIAuthError, match=str(status)):
        api._request("GET", "/api/db/mercado-tokens")
    assert len(calls) == 1


def test_daily_round_does_not_loop_on_revoked_authorization(monkeypatch):
    def fail(*args, **kwargs):
        raise api.DatabaseAPIAuthError("任务租约失效")

    monkeypatch.setattr(daily, "run_ai_appeal_once", fail)
    monkeypatch.setattr(daily.time, "sleep", lambda _: pytest.fail("must exit before next round"))
    with pytest.raises(api.DatabaseAPIAuthError):
        daily._loop_ai_appeal_locked("侵权", max_rounds=2)


def test_poll_does_not_retry_authorization_failure(monkeypatch):
    monkeypatch.setattr(api, "DB_MODE", "api")
    calls = []

    def request(method, path, **kwargs):
        calls.append(method)
        if method == "POST":
            return {"job_id": "test-job"}
        raise api.DatabaseAPIAuthError("任务租约失效")

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr(api.time, "sleep", lambda _: pytest.fail("auth must not retry"))
    with pytest.raises(api.DatabaseAPIAuthError):
        api.collect_live_detection_infractions([])
    assert calls == ["POST", "GET"]


def test_lease_expired_worker_receives_stop_without_resurrecting_job(tmp_path):
    store = LocalAgentStore(tmp_path / "hub.sqlite3")
    store.heartbeat("agent-old", name="旧 Agent", now=100)
    store.enqueue_job("expired-job", "agent-old", "daily_task", {}, now=101)
    store.claim_job("agent-old", now=102, lease_seconds=60)
    assert store.reap_expired_jobs(now=200, lease_seconds=60) == 1
    store.heartbeat("agent-old", name="旧 Agent", now=201)
    store.append_event("expired-job", "agent-old", content="Forbidden", now=202)
    assert store.cancellation_job_ids("agent-old") == ["expired-job"]
    assert store.cancellation_job_ids("agent-other") == []
    assert store.get_job("expired-job")["status"] == "error"


@pytest.mark.parametrize("workers,windows,expected", [("15", "3", 3), ("2", "5", 2), ("bad", "4", 4), ("30", "bad", 3)])
def test_daily_workers_respect_admission_capacity(monkeypatch, workers, windows, expected):
    monkeypatch.setenv("BIT_DAILY_BROWSER_WORKER_LIMIT", workers)
    monkeypatch.setenv("BIT_BROWSER_MAX_OPEN_WINDOWS", windows)
    assert daily._daily_browser_worker_limit() == expected


@pytest.fixture(scope="module")
def offline_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.mark.parametrize("page_kind", ["help_without_switcher", "hidden_option", "visible_option", "opened_option"])
def test_site_switch_clicks_only_visible_site_options_once(monkeypatch, offline_browser, page_kind):
    page = offline_browser.new_page()
    page.route("**/*", lambda route: route.abort())
    html = '<a href="#article">Country Brazil help article</a>'
    if page_kind in {"hidden_option", "visible_option", "opened_option"}:
        display = "none" if page_kind != "visible_option" else "block"
        html += f'<ul id="nav-header-cbt__switcher" style="display:{display}"><li><span data-value="MLB-remote">Brazil</span></li></ul>'
    if page_kind == "opened_option":
        html += '<button class="nav-header-cbt__trigger" onclick="document.querySelector(\'ul\').style.display=\'block\'">Choose site</button>'
    page.set_content(html)
    page.evaluate("""() => {
        window.clicks = [];
        document.addEventListener('click', e => window.clicks.push(e.target.tagName));
    }""")
    driver = SimpleNamespace(
        execute_script=lambda script, *args: page.evaluate(
            "([script,args]) => Function(script).apply(null,args)", [script, list(args)]),
        refresh=lambda: None,
    )
    checks = iter([False, True])
    monkeypatch.setattr(ai, "verify_selected_site", lambda *a: next(checks))
    monkeypatch.setattr(ai.time, "sleep", lambda _: None)
    try:
        changed = ai.select_mercado_site_fast(driver, "测试", "巴西")
        assert changed is (page_kind in {"visible_option", "opened_option"})
        expected = {"visible_option": ["LI"], "opened_option": ["BUTTON", "LI"]}.get(page_kind, [])
        assert page.evaluate("window.clicks") == expected
    finally:
        page.close()
