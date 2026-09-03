from bit import bit_mercado_login
from bit_playwright import common
from selenium import webdriver


def test_playwright_auto_login_uses_authorization_email(monkeypatch):
    captured = {}

    class Driver:
        def implicitly_wait(self, seconds):
            captured["implicit_wait"] = seconds

    class Service:
        def __init__(self, path):
            captured["driver_path"] = path

        def stop(self):
            captured["service_stopped"] = True

    monkeypatch.setattr(
        bit_mercado_login,
        "load_shop_login_config",
        lambda shop_name, window_id="": {
            "shop_name": shop_name,
            "window_id": window_id,
            "email": "authorization@example.com",
        },
    )
    monkeypatch.setattr(
        bit_mercado_login,
        "login_mercado_with_saved_password",
        lambda driver, shop_name, **kwargs: captured.update(
            driver=driver,
            shop_name=shop_name,
            **kwargs,
        ) or {"ok": True},
    )
    monkeypatch.setattr(webdriver, "Chrome", lambda **_kwargs: Driver())
    monkeypatch.setattr("selenium.webdriver.chrome.service.Service", Service)

    session = common.BitPlaywrightSession("window-1")
    session.shop_name = "授权店铺"
    session.open_result = {"data": {"driver": "driver.exe", "http": "127.0.0.1:9222"}}

    assert session.auto_login_mercado(wait_seconds=20) == {"ok": True}
    assert captured["email"] == "authorization@example.com"
    assert captured["window_id"] == "window-1"
    assert captured["service_stopped"] is True
