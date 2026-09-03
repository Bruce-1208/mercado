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


def test_zeshun_brand_logo_is_used_on_login_and_workbench():
    template_dir = Path(bit_interface.app.template_folder)
    login_template = (template_dir / "login.html").read_text(encoding="utf-8")
    workbench_template = (template_dir / "index.html").read_text(encoding="utf-8")
    logo_path = Path(bit_interface.app.static_folder, "images", "zeshun-monogram-gold.png")

    for template in (login_template, workbench_template):
        assert "images/zeshun-monogram-gold.png" in template
        assert "武汉泽顺 Logo" in template

    assert "武汉·泽顺商贸有限公司" in login_template
    assert "Wuhan Zeshun Trading Co., Ltd." in login_template
    assert logo_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_interface_hot_reload_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("BIT_INTERFACE_HOT_RELOAD", raising=False)
    monkeypatch.delattr(bit_interface.sys, "frozen", raising=False)

    assert bit_interface.interface_hot_reload_enabled() is True
    assert bit_interface.interface_hot_reload_enabled("true") is True
    assert bit_interface.interface_hot_reload_enabled("0") is False
    assert bit_interface.is_werkzeug_reloader_child(
        {"WERKZEUG_RUN_MAIN": "true"}
    ) is True
    assert bit_interface.is_werkzeug_reloader_child({}) is False
    assert bit_interface.app.config["TEMPLATES_AUTO_RELOAD"] is True
    assert bit_interface.app.config["SEND_FILE_MAX_AGE_DEFAULT"] == 0

    response = bit_interface.app.test_client().get("/login")
    assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate"
    assert response.headers["Pragma"] == "no-cache"


def test_interface_hot_reload_is_disabled_for_frozen_executable(monkeypatch):
    monkeypatch.setattr(bit_interface.sys, "frozen", True, raising=False)
    monkeypatch.setenv("BIT_INTERFACE_HOT_RELOAD", "1")

    assert bit_interface.interface_hot_reload_enabled() is False
    assert bit_interface.interface_hot_reload_enabled("true") is False


def test_interface_main_enables_frozen_multiprocessing_support(monkeypatch):
    events = []
    monkeypatch.setattr(
        bit_interface.multiprocessing,
        "freeze_support",
        lambda: events.append("freeze-support"),
    )
    monkeypatch.setattr(
        bit_interface,
        "run_interface_server",
        lambda: events.append("server") or True,
    )

    assert bit_interface.run_interface_main() is True
    assert events == ["freeze-support", "server"]


def test_hot_reload_parent_owns_lock_and_child_starts_services(monkeypatch):
    events = []
    run_options = []

    class FakeLock:
        def __init__(self, *args, **kwargs):
            events.append("lock-created")

        def acquire(self, timeout=0):
            events.append("lock-acquired")
            return True

        def release(self):
            events.append("lock-released")

    monkeypatch.setenv("BIT_INTERFACE_HOT_RELOAD", "1")
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.setattr(bit_interface, "InterProcessLock", FakeLock)
    monkeypatch.setattr(
        bit_interface,
        "start_interface_background_services",
        lambda: events.append("services-started"),
    )
    monkeypatch.setattr(bit_interface.app, "run", lambda **kwargs: run_options.append(kwargs))

    assert bit_interface.run_interface_server() is True
    assert events == ["lock-created", "lock-acquired", "lock-released"]
    assert run_options[0]["use_reloader"] is True
    assert run_options[0]["use_debugger"] is False

    events.clear()
    run_options.clear()
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")

    assert bit_interface.run_interface_server() is True
    assert events == ["services-started"]
    assert run_options[0]["use_reloader"] is True


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
