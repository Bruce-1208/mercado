import json

import pytest

from bit import browser_extension_mail as mail


class FakeSMTP:
    messages = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self, **_kwargs):
        return None

    def login(self, username, password):
        self.username = username
        self.password = password

    def send_message(self, message):
        self.messages.append(message)


def test_mail_settings_encrypt_authorization_code_and_send_deduplicated_alert(tmp_path):
    path = tmp_path / "mail.json"
    secret = "test-workbench-secret"
    saved = mail.save_settings(7, {
        "enabled": True,
        "sender_email": "notice@qq.com",
        "receiver_email": "operator@example.com",
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_password": "smtp-authorization-code",
    }, secret, path)

    raw = path.read_text(encoding="utf-8")
    assert "smtp-authorization-code" not in raw
    assert saved["configured"] is True
    assert saved["password_configured"] is True

    FakeSMTP.messages.clear()
    event = {"event_type": "slider_verification", "source": "美客多商品采集", "message": "请完成滑块验证"}
    first = mail.send_alert(7, event, secret, path, smtp_ssl=FakeSMTP)
    second = mail.send_alert(7, event, secret, path, smtp_ssl=FakeSMTP)
    assert first["sent"] is True
    assert second["deduplicated"] is True
    assert len(FakeSMTP.messages) == 1
    assert "滑块或人机验证" in FakeSMTP.messages[0]["Subject"]
    assert "operator@example.com" == FakeSMTP.messages[0]["To"]


def test_mail_settings_preserve_saved_password_when_update_leaves_it_blank(tmp_path):
    path = tmp_path / "mail.json"
    secret = "test-secret"
    initial = {
        "enabled": True, "sender_email": "notice@163.com", "receiver_email": "ops@example.com",
        "smtp_host": "smtp.163.com", "smtp_port": 465, "smtp_security": "ssl",
        "smtp_password": "first-code",
    }
    mail.save_settings(8, initial, secret, path)
    mail.save_settings(8, {**initial, "enabled": False, "smtp_password": ""}, secret, path)

    FakeSMTP.messages.clear()
    result = mail.send_test(8, secret, path, smtp_ssl=FakeSMTP)
    assert result["sent"] is True
    assert FakeSMTP.messages[0]["From"] == "notice@163.com"


@pytest.mark.parametrize("field,value", [
    ("sender_email", "bad"), ("receiver_email", "bad"),
    ("smtp_host", "https://smtp.qq.com"), ("smtp_port", 70000),
    ("smtp_security", "plain"),
])
def test_mail_settings_reject_invalid_values(tmp_path, field, value):
    payload = {
        "enabled": True, "sender_email": "notice@qq.com", "receiver_email": "ops@example.com",
        "smtp_host": "smtp.qq.com", "smtp_port": 465, "smtp_security": "ssl",
        "smtp_password": "code",
    }
    payload[field] = value
    with pytest.raises(ValueError):
        mail.save_settings(9, payload, "secret", tmp_path / "mail.json")


def test_browser_extension_notification_routes(monkeypatch):
    from bit import bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    user = {"id": 7, "username": "collector", "display_name": "采集员", "access_version": 0}
    token = workbench.create_browser_extension_token(user)
    headers = {"Authorization": f"Bearer {token}"}
    calls = []
    monkeypatch.setattr(workbench.browser_extension_mail, "get_public_settings", lambda *args: {"configured": True})
    monkeypatch.setattr(workbench.browser_extension_mail, "save_settings", lambda *args: calls.append("save") or {"configured": True})
    monkeypatch.setattr(workbench.browser_extension_mail, "send_test", lambda *args: calls.append("test") or {"sent": True})
    monkeypatch.setattr(workbench.browser_extension_mail, "send_alert", lambda *args: calls.append("alert") or {"sent": True})
    client = workbench.app.test_client()

    assert client.get("/api/browser-extension/notifications/settings").status_code == 401
    assert client.get("/api/browser-extension/notifications/settings", headers=headers).status_code == 200
    assert client.put("/api/browser-extension/notifications/settings", headers=headers, json={}).status_code == 200
    assert client.post("/api/browser-extension/notifications/test", headers=headers).status_code == 200
    assert client.post(
        "/api/browser-extension/notifications/send", headers=headers,
        json={"event_type": "rate_limit", "message": "HTTP 429"},
    ).status_code == 200
    assert calls == ["save", "test", "alert"]
