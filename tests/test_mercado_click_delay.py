from bit import mercado_click_delay
from bit import mercado_appeal_runner


def test_click_delay_only_applies_to_mercado_domains(monkeypatch):
    sleeps = []
    monkeypatch.setattr(mercado_click_delay.time, "sleep", sleeps.append)

    assert mercado_click_delay.wait_after_mercado_click(
        "https://global-selling.mercadolibre.com/help"
    ) is True
    assert mercado_click_delay.wait_after_mercado_click(
        "https://www.mercadolivre.com.br/"
    ) is True
    assert mercado_click_delay.wait_after_mercado_click("https://www.1688.com/") is False

    assert sleeps == [2.0, 2.0]


def test_javascript_click_detection_handles_shadow_dom_scripts():
    assert mercado_click_delay.javascript_contains_click("target.click();") is True
    assert mercado_click_delay.javascript_contains_click("node.click ()") is True
    assert mercado_click_delay.javascript_contains_click(
        "for (const type of ['mousedown', 'mouseup', 'click']) "
        "target.dispatchEvent(new MouseEvent(type));"
    ) is True
    assert mercado_click_delay.javascript_contains_click("target.textContent") is False


def test_browser_click_delay_installers_are_idempotent():
    assert mercado_click_delay.install_selenium_click_delay() is True
    assert mercado_click_delay.install_selenium_click_delay() is True
    assert mercado_click_delay.install_playwright_click_delay() is True
    assert mercado_click_delay.install_playwright_click_delay() is True


def test_selenium_web_element_click_waits_on_mercado_page(monkeypatch):
    from selenium.webdriver.remote.webelement import WebElement

    sleeps = []
    monkeypatch.setattr(mercado_click_delay.time, "sleep", sleeps.append)

    class Parent:
        current_url = "https://global-selling.mercadolibre.com/orders"

        def execute(self, command, params=None):
            return {"value": None}

    WebElement(Parent(), "element-1").click()

    assert sleeps == [2.0]


def test_cdp_javascript_and_mouse_clicks_wait_two_seconds(monkeypatch):
    cooldowns = []
    monkeypatch.setattr(
        mercado_appeal_runner,
        "mercado_click_cooldown",
        lambda: cooldowns.append("waited"),
    )

    cdp = mercado_appeal_runner.Cdp.__new__(mercado_appeal_runner.Cdp)
    calls = []
    cdp.call = lambda method, params=None, timeout=30: (
        calls.append((method, params))
        or {"result": {"value": True}}
    )

    assert cdp.js("document.querySelector('button').click(); true") is True
    assert cdp.js("document.title") is True
    mercado_appeal_runner.mouse_click(cdp, 10, 20)

    assert cooldowns == ["waited", "waited"]
    assert [method for method, _ in calls].count("Input.dispatchMouseEvent") == 3
