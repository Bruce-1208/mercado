import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask

from erp.ai_weight_price import credentials
from erp.ai_weight_price.config import validate
from erp.ai_weight_price.models import Models
from erp.ai_weight_price.service import Service
from erp.ai_weight_price.web import create_blueprint


def test_erp_writeback_is_enabled_by_default():
    assert validate({})["writeback_enabled"] is True


def test_authorized_remote_console_can_save_erp_writeback_setting(tmp_path):
    service = Service(tmp_path)
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(create_blueprint(service, authorize=lambda _permission: None))
    config = service.config.load()
    config["writeback_enabled"] = False

    response = app.test_client().put(
        "/api/ai-weight-price/config",
        base_url="https://wuhanzeshun.com",
        json=config,
        headers={"X-AWP-Request": "1"},
    )

    assert response.status_code == 200
    assert response.get_json()["writeback_enabled"] is False
    assert service.config.load()["writeback_enabled"] is False


def test_process_value_wins_and_new_user_value_is_not_cached(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", " process-value ")
    monkeypatch.setattr(credentials, "windows_user_environment", lambda name: "saved-value")
    assert credentials.api_key("TEST_MODEL_KEY") == "process-value"
    monkeypatch.delenv("TEST_MODEL_KEY")
    assert credentials.api_key("TEST_MODEL_KEY") == "saved-value"
    monkeypatch.setattr(credentials, "windows_user_environment", lambda name: "rotated-value")
    assert credentials.api_key("TEST_MODEL_KEY") == "rotated-value"


def test_windows_user_lookup_reads_only_requested_variable_and_handles_missing(monkeypatch):
    registry = MagicMock()
    fake = SimpleNamespace(HKEY_CURRENT_USER=1, REG_SZ=1, REG_EXPAND_SZ=2,
                           OpenKey=MagicMock(return_value=registry),
                           QueryValueEx=MagicMock(return_value=(" saved-value ", 1)))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert credentials.windows_user_environment("TEST_MODEL_KEY") == "saved-value"
    fake.OpenKey.assert_called_once_with(fake.HKEY_CURRENT_USER, "Environment")
    fake.QueryValueEx.assert_called_once_with(registry.__enter__.return_value, "TEST_MODEL_KEY")
    fake.QueryValueEx.side_effect = FileNotFoundError()
    assert credentials.windows_user_environment("TEST_MODEL_KEY") == ""


def test_preflight_and_actual_request_share_persisted_key(monkeypatch, tmp_path):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "windows_user_environment", lambda name: "test-token")
    config = validate({})
    Service(tmp_path).preflight(config, "pipeline")
    requests = []
    def post(url, **kwargs):
        requests.append(kwargs)
        return SimpleNamespace(ok=True, json=lambda: {"choices": [{"message": {"content": "OK"}}]})
    monkeypatch.setattr("erp.ai_weight_price.models.requests.post", post)
    logs = []
    assert Models(config, logs.append).call(config["model"], "Reply OK") == "OK"
    assert requests[0]["headers"]["Authorization"] == "Bearer test-token"
    assert "test-token" not in str(logs)


@pytest.mark.parametrize("failure", [False, True])
def test_live_connection_check_keeps_secrets_out_of_state_and_response(monkeypatch, tmp_path, failure):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "windows_user_environment", lambda name: "test-token")
    class Model:
        def call(self, model, prompt):
            if failure:
                raise RuntimeError("private-provider-response test-token")
            return "OK"
    service = Service(tmp_path, models_factory=lambda *args: Model())
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(service))
    response = app.test_client().post("/api/ai-weight-price/model/check", json={}, headers={"X-AWP-Request": "1"})
    assert response.status_code == (400 if failure else 200)
    state = service.status()["model_connection"]
    assert state["configured"] is True
    assert state["ok"] is not failure
    serialized = json.dumps([response.get_json(), state, service.store.logs()])
    assert "test-token" not in serialized and "private-provider-response" not in serialized


def test_server_routes_use_account_key_without_persisting_it(monkeypatch, tmp_path):
    from flask import session

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "windows_user_environment", lambda name: "")
    model_configs = []
    service = Service(tmp_path, models_factory=lambda config, log: (
        model_configs.append(dict(config)) or SimpleNamespace(call=lambda *args: "OK")
    ))
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(create_blueprint(
        service, authorize=lambda permission: None, server_execution=True,
        resolve_api_key=lambda: {1: "account-one", 2: "account-two"}.get(
            session["workbench_user"]["id"], ""
        ),
    ))
    client = app.test_client()
    base_url = "https://console.example.com"
    headers = {"X-AWP-Request": "1"}
    calls = []
    monkeypatch.setattr(service, "require_login", lambda config: None)
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(service, "continue_after_human", lambda **kwargs: calls.append(kwargs))
    for user_id in (1, 2):
        with client.session_transaction(base_url=base_url) as state:
            state["workbench_user"] = {"id": user_id, "role_key": "super_admin"}
        client.get("/api/ai-weight-price/status", base_url=base_url)
        service.store.add({"erp_goods_id": "item", "title": "test"})
        for path, body in (
            ("start", {"mode": "pipeline", "selection": {"start_page": 1, "end_page": 1}}),
            ("tasks/item/retry", {}),
            ("continue", {"acknowledged": True}),
            ("model/check", {}),
        ):
            response = client.post("/api/ai-weight-price/" + path,
                                   json={**body, "runtime_api_key": "untrusted-key"},
                                   headers=headers, base_url=base_url)
            assert response.status_code == 200, response.get_json()
        expected = {1: "account-one", 2: "account-two"}[user_id]
        assert all(call["runtime_api_key"] == expected for call in calls[-3:])
        assert model_configs[-1]["_runtime_api_key"] == expected
        status = client.get("/api/ai-weight-price/status", base_url=base_url).get_json()
        assert status["model_connection"]["configured"] is True
        serialized = json.dumps([status, service.config.load(), service.store.logs()])
        assert expected not in serialized
        assert "_runtime_api_key" not in service.config.load()
