import json
from pathlib import Path

from bit import account_webhook


def test_webhook_settings_are_encrypted_and_account_bound(tmp_path, monkeypatch):
    config = tmp_path / "webhooks.json"
    public = account_webhook.save_settings(
        7,
        {
            "enabled": True,
            "url": "https://hooks.example.com/zeshun",
            "secret": "account-secret",
        },
        "flask-secret",
        config,
    )

    assert public["configured"] is True
    assert public["secret_configured"] is True
    assert account_webhook.get_public_settings(8, "flask-secret", config)["configured"] is False
    raw = config.read_text(encoding="utf-8")
    assert "account-secret" not in raw
    assert config.stat().st_mode & 0o777 == 0o600

    captured = {}

    class Response:
        status = 204

    def fake_open(request, timeout):
        captured.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr(account_webhook, "_is_public_destination", lambda _host: True)
    result = account_webhook.send_test(
        7, "flask-secret", config, urlopen_impl=fake_open
    )

    assert result["sent"] is True
    assert captured["timeout"] == 15
    assert captured["request"].full_url == "https://hooks.example.com/zeshun"
    assert captured["request"].headers["X-zeshun-event"] == "test"
    assert captured["request"].headers["X-zeshun-signature"].startswith("sha256=")
    assert json.loads(captured["request"].data)["event"] == "test"


def test_integration_page_documents_every_supported_credential():
    template = (
        Path(__file__).resolve().parents[1]
        / "bit"
        / "templates"
        / "integration_settings.html"
    ).read_text(encoding="utf-8")

    for value in (
        "deepseek-v4-pro",
        "qwen3-vl-flash",
        "qwen3-vl-plus",
        "qwen-plus",
        "rembg / isnet-general-use",
        "doubao-seedance-2-5-260628",
        "wan3.0-video-prime",
        "SMTP",
        "Webhook",
    ):
        assert value in template
    assert "/api/account-integrations/tokens" in template
    assert "/api/account-integrations/email" in template
    assert "/api/account-integrations/webhook" in template
    assert "简易配置教程" in template


def test_account_integration_api_uses_logged_in_user_id(monkeypatch):
    from bit import bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="integration-secret")
    workbench.app.secret_key = "integration-secret"
    captured = {}

    def save_tokens(user_id, payload, secret_key):
        captured.update(user_id=user_id, payload=payload, secret_key=secret_key)
        return {"deepseek_configured": True}

    monkeypatch.setattr(workbench.browser_extension_models, "save_settings", save_tokens)
    client = workbench.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = {
            "id": 31,
            "username": "account-owner",
            "access_version": 0,
        }

    response = client.put(
        "/api/account-integrations/tokens",
        json={"deepseek_api_key": "private-key"},
    )

    assert response.status_code == 200
    assert captured == {
        "user_id": 31,
        "payload": {"deepseek_api_key": "private-key"},
        "secret_key": "integration-secret",
    }
    assert "private-key" not in response.get_data(as_text=True)


def test_plugin_alert_dispatches_email_and_webhook(monkeypatch):
    from bit import bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="integration-secret")
    workbench.app.secret_key = "integration-secret"
    user = {"id": 41, "username": "collector", "access_version": 0}
    token = workbench.create_browser_extension_token(user)
    calls = []
    monkeypatch.setattr(
        workbench.browser_extension_mail,
        "send_alert",
        lambda user_id, event, _secret: calls.append(("email", user_id, event)) or {"sent": True},
    )
    monkeypatch.setattr(
        workbench.account_webhook,
        "send_event",
        lambda user_id, event, _secret: calls.append(("webhook", user_id, event)) or {"sent": True},
    )
    client = workbench.app.test_client()
    response = client.post(
        "/api/browser-extension/notifications/send",
        headers={"Authorization": f"Bearer {token}"},
        json={"event_type": "task_blocked", "message": "等待人工处理"},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["sent"] is True
    assert [call[0] for call in calls] == ["email", "webhook"]
    assert all(call[1] == 41 for call in calls)
