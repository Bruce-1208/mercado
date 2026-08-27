from datetime import timedelta
from pathlib import Path

from bit import bit_interface


def _authenticated_user():
    return {
        "id": 7,
        "username": "tester",
        "display_name": "测试账号",
        "role_name": "运营人员",
        "permissions": ["*"],
        "access_version": 1,
    }


def test_login_page_contains_six_hour_remember_option():
    template = Path(bit_interface.app.template_folder, "login.html").read_text(
        encoding="utf-8"
    )

    assert 'id="remember-login"' in template
    assert "记住账号密码" in template
    assert "6 小时内自动登录" in template
    assert 'autocomplete="username"' in template
    assert 'autocomplete="current-password"' in template
    assert "zeshun-remembered-username" in template


def test_remembered_login_uses_six_hour_permanent_session(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "authenticate_workbench_user",
        lambda username, password: _authenticated_user(),
    )
    bit_interface.app.config.update(TESTING=True)
    client = bit_interface.app.test_client()

    response = client.post(
        "/api/login",
        json={"username": "tester", "password": "secret", "remember": True},
    )

    assert response.status_code == 200
    assert response.get_json()["remember"] is True
    assert response.get_json()["expires_in"] == 6 * 60 * 60
    assert "Expires=" in response.headers.get("Set-Cookie", "")
    assert bit_interface.app.permanent_session_lifetime == timedelta(hours=6)
    assert bit_interface.app.config["SESSION_REFRESH_EACH_REQUEST"] is False
    with client.session_transaction() as flask_session:
        assert flask_session.permanent is True
        assert flask_session["workbench_user"]["username"] == "tester"

    login_page = client.get("/login")
    assert login_page.status_code == 302
    assert login_page.headers["Location"].endswith("/")


def test_login_without_remember_keeps_browser_session_cookie(monkeypatch):
    monkeypatch.setattr(
        bit_interface,
        "authenticate_workbench_user",
        lambda username, password: _authenticated_user(),
    )
    bit_interface.app.config.update(TESTING=True)
    client = bit_interface.app.test_client()

    response = client.post(
        "/api/login",
        json={"username": "tester", "password": "secret", "remember": False},
    )

    assert response.status_code == 200
    assert response.get_json()["remember"] is False
    assert response.get_json()["expires_in"] is None
    assert "Expires=" not in response.headers.get("Set-Cookie", "")
    with client.session_transaction() as flask_session:
        assert flask_session.permanent is False


def test_generated_session_secret_is_reused(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKBENCH_SECRET_KEY", raising=False)
    monkeypatch.delenv("WORKBENCH_SECRET_KEY_FILE", raising=False)
    secret_path = tmp_path / "workbench_secret.key"

    first = bit_interface.resolve_workbench_secret_key(secret_path)
    second = bit_interface.resolve_workbench_secret_key(secret_path)

    assert len(first) == 64
    assert second == first
    assert secret_path.read_text(encoding="utf-8") == first
