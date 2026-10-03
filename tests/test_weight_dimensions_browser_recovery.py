"""Offline recovery checks: no live browser, credentials, or product writes."""
import errno
import io
import json
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from bit import bit_runtime_lock
from erp.ai_weight_price import browser as browser_module, edge, service as service_module
from erp.ai_weight_price.browser import Browser, ZyingLoginRequired
from erp.ai_weight_price.config import validate
from erp.ai_weight_price.service import Service


@pytest.fixture
def isolated_service(tmp_path, monkeypatch):
    monkeypatch.setattr(bit_runtime_lock, "RUNTIME_LOCK_DIR", tmp_path / "locks")
    return Service(tmp_path / "state", storage_backend="sqlite", migrate_legacy_state=False)


@pytest.fixture
def discovery(monkeypatch):
    class PlaywrightError(Exception):
        pass

    pw = SimpleNamespace(chromium=Mock(), stop=Mock(), error=PlaywrightError)
    package = ModuleType("playwright")
    package.__path__ = []
    sync_api = ModuleType("playwright.sync_api")
    sync_api.Error = PlaywrightError
    sync_api.sync_playwright = lambda: SimpleNamespace(start=lambda: pw)
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    opener = Mock()
    monkeypatch.setattr(browser_module, "build_opener", lambda *args: opener)
    adapter = Browser(validate({}), threading.Event(), Mock())
    return adapter, opener, pw


@pytest.mark.parametrize("error,detail", [
    (URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")), "连接被拒绝"),
    (URLError(TimeoutError("timed out")), "连接超时"),
    (TimeoutError("timed out"), "连接超时"),
])
def test_discovery_connection_failure_has_recovery_instruction(discovery, error, detail):
    adapter, opener, pw = discovery
    opener.open.side_effect = error

    with pytest.raises(ValueError, match=detail) as result:
        adapter.__enter__()

    assert result.value.__cause__ is error
    assert "http://127.0.0.1:9222" in str(result.value)
    assert "当前执行电脑" in str(result.value)
    assert "专用 Edge" in str(result.value)
    opener.open.assert_called_once_with("http://127.0.0.1:9222/json/version", timeout=5)
    pw.chromium.connect_over_cdp.assert_not_called()
    pw.stop.assert_called_once()


@pytest.mark.parametrize("error", [
    HTTPError("http://127.0.0.1:9222/json/version", 403, "Forbidden", {}, None),
    URLError(ConnectionResetError(errno.ECONNRESET, "reset")),
    RuntimeError("adapter bug"),
])
def test_discovery_does_not_relabel_unrelated_errors(discovery, error):
    adapter, opener, pw = discovery
    opener.open.side_effect = error

    with pytest.raises(type(error)) as result:
        adapter.__enter__()

    assert result.value is error
    pw.stop.assert_called_once()


def test_malformed_discovery_response_preserves_json_error(discovery):
    adapter, opener, pw = discovery
    opener.open.return_value = io.BytesIO(b"not JSON")

    with pytest.raises(json.JSONDecodeError):
        adapter.__enter__()

    pw.stop.assert_called_once()


def test_playwright_connection_error_is_not_reported_as_port_refusal(discovery):
    adapter, opener, pw = discovery
    opener.open.return_value = io.BytesIO(json.dumps({
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test",
    }).encode())
    failure = RuntimeError("CDP protocol mismatch")
    pw.chromium.connect_over_cdp.side_effect = failure

    with pytest.raises(RuntimeError) as result:
        adapter.__enter__()

    assert result.value is failure
    pw.stop.assert_called_once()


@pytest.mark.parametrize("detail", ["Connection closed", "Connection timed out"])
def test_playwright_connection_failure_has_recovery_instruction(discovery, detail):
    adapter, opener, pw = discovery
    opener.open.return_value = io.BytesIO(json.dumps({
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test",
    }).encode())
    failure = pw.error(detail)
    pw.chromium.connect_over_cdp.side_effect = failure

    with pytest.raises(ValueError, match="浏览器会话") as result:
        adapter.__enter__()

    assert result.value.__cause__ is failure
    assert "当前执行电脑" in str(result.value)
    assert "连接被拒绝" not in str(result.value)
    pw.stop.assert_called_once()


def test_weight_dimensions_opens_edge_before_connect_and_confirms_login(isolated_service, monkeypatch):
    events = []
    adapter = Mock()
    adapter.__enter__ = Mock(side_effect=lambda: (events.append("connect"), adapter)[1])
    adapter.__exit__ = Mock(side_effect=lambda *_: events.append("disconnect"))
    adapter.confirm_login.side_effect = lambda: events.append("login")
    isolated_service.browser_factory = lambda *args: adapter
    monkeypatch.setattr(service_module, "open_edge", lambda url, root: events.append(("open", url, root)))

    with isolated_service.weight_dimensions_browser(Mock()) as connected:
        assert connected is adapter
        events.append("body")

    assert events == [
        ("open", "http://127.0.0.1:9222", isolated_service.store.root),
        "connect", "login", "body", "disconnect",
    ]
    adapter.open_login.assert_not_called()


def test_preflight_checks_login_without_product_or_saved_login_changes(isolated_service, monkeypatch):
    adapter = Mock()
    adapter.__enter__ = Mock(return_value=adapter)
    adapter.__exit__ = Mock(return_value=False)
    isolated_service.browser_factory = lambda *args: adapter
    monkeypatch.setattr(service_module, "open_edge", Mock(return_value="edge-id"))
    login = {"confirmed": True, "confirmed_at": 123}
    binding = {"identity": "previous-browser"}
    isolated_service.store.set_state("login", login)
    isolated_service.store.set_state("login_browser", binding)

    assert isolated_service.check_weight_dimensions_browser() is None

    adapter.confirm_login.assert_called_once_with()
    adapter.update_package_by_product_id.assert_not_called()
    assert isolated_service.store.state("login") == login
    assert isolated_service.store.state("login_browser") == binding


@pytest.mark.parametrize("existing_page", [True, False])
def test_missing_login_retains_recovery_page_and_releases_lock(isolated_service, monkeypatch, existing_page):
    page = Mock(url="https://meli.zying.net/#/login")
    create_page = Mock(return_value=page)

    class ConnectedBrowser(Browser):
        def __enter__(self):
            self.context = SimpleNamespace(pages=[page] if existing_page else [], new_page=create_page)
            self.pw = SimpleNamespace(stop=Mock())
            return self

    adapter = ConnectedBrowser(validate({}), threading.Event(), Mock())
    isolated_service.browser_factory = lambda *args: adapter
    monkeypatch.setattr(service_module, "open_edge", Mock(return_value="edge-id"))

    with pytest.raises(ValueError, match="当前执行电脑") as result:
        isolated_service.check_weight_dimensions_browser()

    assert isinstance(result.value.__cause__, ZyingLoginRequired)
    assert "重新点击“更新智赢产品”" in str(result.value)
    page.bring_to_front.assert_called_once_with()
    page.close.assert_not_called()
    if existing_page:
        create_page.assert_not_called()
        page.goto.assert_not_called()
    else:
        create_page.assert_called_once_with()
        page.goto.assert_called_once_with(edge.LOGIN_URL, wait_until="domcontentloaded")
    adapter.pw.stop.assert_called_once_with()
    lock = isolated_service.lock()
    assert lock.acquire()
    lock.release()


def test_login_internal_error_is_not_treated_as_missing_login(isolated_service, monkeypatch):
    adapter = Mock()
    adapter.__enter__ = Mock(return_value=adapter)
    adapter.__exit__ = Mock(return_value=False)
    failure = ValueError("DOM selector configuration is invalid")
    adapter.confirm_login.side_effect = failure
    isolated_service.browser_factory = lambda *args: adapter
    monkeypatch.setattr(service_module, "open_edge", Mock(return_value="edge-id"))

    with pytest.raises(ValueError) as result:
        isolated_service.check_weight_dimensions_browser()

    assert result.value is failure
    adapter.open_login.assert_not_called()


def test_available_edge_is_reused_without_launch(tmp_path, monkeypatch):
    launch = Mock()
    monkeypatch.setattr(edge, "debugger_identity", lambda *args: "existing-edge")
    monkeypatch.setattr(edge.subprocess, "Popen", launch)

    assert edge.open_edge("http://127.0.0.1:9222", tmp_path) == "existing-edge"

    launch.assert_not_called()
    assert not (tmp_path / "edge-profile").exists()


def test_missing_debug_port_launches_only_dedicated_visible_profile(tmp_path, monkeypatch):
    readiness = iter([None, "dedicated-edge"])
    launch = Mock()
    monkeypatch.setattr(edge, "debugger_identity", lambda *args: next(readiness))
    monkeypatch.setattr(edge, "edge_executable", lambda: "/offline/msedge")
    monkeypatch.setattr(edge.subprocess, "Popen", launch)

    assert edge.open_edge("http://127.0.0.1:9222", tmp_path) == "dedicated-edge"

    command = launch.call_args.args[0]
    assert command[0] == "/offline/msedge"
    assert f"--user-data-dir={(tmp_path / 'edge-profile').resolve()}" in command
    assert "--remote-debugging-port=9222" in command
    assert "--new-window" in command
    assert command[-1] == edge.LOGIN_URL
    assert not any("headless" in argument for argument in command)
