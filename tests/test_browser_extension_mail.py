import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

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


def test_browser_extension_notification_routes(
    monkeypatch, isolated_legacy_console_user, subscribed_workbench_account,
):
    from bit import bit_interface as workbench

    workbench.app.config.update(TESTING=True, SECRET_KEY="extension-test-secret")
    workbench.app.secret_key = "extension-test-secret"
    user = {"id": 7, "username": "collector", "display_name": "采集员", "access_version": 0}
    calls = []
    monkeypatch.setattr(workbench.browser_extension_mail, "get_public_settings", lambda *args: {"configured": True})
    monkeypatch.setattr(workbench.browser_extension_mail, "save_settings", lambda *args: calls.append("save") or {"configured": True})
    monkeypatch.setattr(workbench.browser_extension_mail, "send_test", lambda *args: calls.append("test") or {"sent": True})
    monkeypatch.setattr(workbench.browser_extension_mail, "send_alert", lambda *args: calls.append("alert") or {"sent": True})
    monkeypatch.setattr(workbench.account_webhook, "send_event", lambda *args: {"sent": False, "disabled": True})
    client = workbench.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["workbench_user"] = user

    assert client.put("/api/account-integrations/email", json={}).status_code == 200
    assert client.post("/api/account-integrations/email/test").status_code == 200
    token = workbench.create_browser_extension_token(user)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(
        "/api/browser-extension/notifications/send", headers=headers,
        json={"event_type": "rate_limit", "message": "HTTP 429"},
    ).status_code == 200
    assert calls == ["save", "test", "alert"]


def test_incident_receipt_survives_process_cache_reset(tmp_path):
    path = tmp_path / 'mail.json'
    mail.save_settings(7, {
        'enabled': True, 'sender_email': 'notice@qq.com', 'receiver_email': 'ops@example.com',
        'smtp_host': 'smtp.qq.com', 'smtp_port': 465, 'smtp_security': 'ssl', 'smtp_password': 'code',
    }, 'secret', path)
    event = {'event_type': 'task_blocked', 'source': 'AI核重核价', 'message': '验证码', 'incident_id': 'run-unique-pause-1'}
    FakeSMTP.messages.clear()
    assert mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP)['sent']
    mail._recent.clear()
    assert mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP)['deduplicated']
    assert len(FakeSMTP.messages) == 1
    assert mail.send_alert(7, {**event, 'incident_id': 'run-unique-pause-2'}, 'secret', path, smtp_ssl=FakeSMTP)['sent']
    assert len(FakeSMTP.messages) == 2


def test_awp_requires_enabled_email(tmp_path):
    with pytest.raises(ValueError, match='必须启用邮件'):
        mail.require_alert_email(7, 'secret', tmp_path / 'missing.json')


@pytest.fixture
def incident_mail(tmp_path, monkeypatch):
    from bit import bit_runtime_lock

    monkeypatch.setattr(bit_runtime_lock, 'RUNTIME_LOCK_DIR', tmp_path / 'locks')
    monkeypatch.setattr(mail, '_recent', {})
    path = tmp_path / 'mail.json'
    mail.save_settings(7, {
        'enabled': True, 'sender_email': 'notice@qq.com', 'receiver_email': 'ops@example.com',
        'smtp_host': 'smtp.qq.com', 'smtp_port': 465, 'smtp_security': 'ssl', 'smtp_password': 'code',
    }, 'secret', path)
    event = {'event_type': 'task_blocked', 'source': 'AI核重核价', 'message': '验证码',
             'incident_id': 'concurrent-pause'}
    digest = hashlib.sha256(f'{path.resolve()}|7|{event["incident_id"]}'.encode()).hexdigest()
    receipt = path.parent / 'mail-receipts' / (digest + '.json')
    FakeSMTP.messages.clear()
    return path, event, digest, receipt


def test_sent_incident_is_confirmed_even_while_its_lock_is_held(incident_mail):
    from bit.bit_runtime_lock import InterProcessLock

    path, event, digest, _receipt = incident_mail
    assert mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP)['sent']
    held = InterProcessLock('awp_mail_' + digest)
    assert held.acquire()
    try:
        result = mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP)
        assert result == {'sent': False, 'deduplicated': True}
        assert len(FakeSMTP.messages) == 1
    finally:
        held.release()


def test_receipt_written_during_lock_contention_is_confirmed(incident_mail, monkeypatch):
    from bit import bit_runtime_lock

    path, event, _digest, receipt = incident_mail

    def complete_other_sender(_lock):
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'sent_at': 100}), encoding='utf-8')
        return False

    monkeypatch.setattr(bit_runtime_lock.InterProcessLock, 'acquire', complete_other_sender)
    assert mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP) == {
        'sent': False, 'deduplicated': True,
    }
    assert not FakeSMTP.messages


def test_concurrent_incident_sends_one_email_and_confirms_on_retry(incident_mail):
    path, event, _digest, receipt = incident_mail
    sending = threading.Event()
    finish = threading.Event()

    class BlockingSMTP(FakeSMTP):
        def send_message(self, message):
            sending.set()
            assert finish.wait(5), 'test did not release SMTP sender'
            super().send_message(message)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(mail.send_alert, 7, event, 'secret', path, smtp_ssl=BlockingSMTP)
        try:
            assert sending.wait(5)
            assert mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP) == {
                'sent': False, 'pending': True,
            }
            assert not receipt.exists()
        finally:
            finish.set()
        assert first.result(timeout=5)['sent']

    assert mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP)['deduplicated']
    assert len(FakeSMTP.messages) == 1


def test_incident_smtp_failure_releases_lock_and_allows_retry(incident_mail):
    path, event, _digest, receipt = incident_mail

    class FailedSMTP(FakeSMTP):
        def send_message(self, _message):
            raise TimeoutError('SMTP temporarily unavailable')

    with pytest.raises(TimeoutError, match='SMTP temporarily unavailable'):
        mail.send_alert(7, event, 'secret', path, smtp_ssl=FailedSMTP)
    assert not receipt.exists()
    assert mail.send_alert(7, event, 'secret', path, smtp_ssl=FakeSMTP)['sent']
    assert len(FakeSMTP.messages) == 1
