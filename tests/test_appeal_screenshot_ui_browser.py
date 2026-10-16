"""Real templates, synthetic data, and intercepted networking only."""
from pathlib import Path
import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401
from test_zeshun_appeal_popup_browser import popup  # noqa: F401
from test_zeshun_appeal_page_browser import page, action  # noqa: F401

@pytest.fixture(scope="module")
def browser(console_browser):
    return console_browser


ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts/appeal-screenshot-mode"


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_task_switch_keyboard_selection_and_layout(console_page, width):
    page = console_page
    page.set_default_timeout(5000)
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-tasks').classList.add('active');
    }""")
    toggle = page.locator("#daily-task-screenshot-mode")
    toggle.focus()
    page.keyboard.press("Space")
    assert toggle.is_checked()
    assert page.evaluate("getDailyTaskPayload().screenshot_mode") is True
    page.locator('#daily-task-algorithm-mode').focus()
    page.keyboard.press('Space')
    page.locator('input[name="daily-task-appeal-type"][value="投诉"]').focus()
    page.keyboard.press('Space')
    payload = page.evaluate("getDailyTaskPayload()")
    assert payload["screenshot_mode"] and payload["task_strategy"] == "algorithm"
    assert set(payload["appeal_types"]) == {"侵权", "投诉"}
    toggle.focus()
    assert toggle.evaluate("el => el === document.activeElement")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    toggle.scroll_into_view_if_needed()
    page.screenshot(path=str(ARTIFACTS / f"tasks-{width}.png"))
    page.keyboard.press("Space")
    assert page.evaluate("getDailyTaskPayload().screenshot_mode") is False


@pytest.mark.parametrize("width", [390, 1440])
def test_plugin_switch_survives_running_refresh(popup, width):
    popup.set_viewport_size({"width": width, "height": 1000})
    toggle = popup.locator("#appeal-screenshot-mode")
    toggle.focus()
    popup.keyboard.press("Space")
    assert toggle.is_checked()
    popup.locator('#appeal-sites input[value="MLB"]').check()
    popup.locator('#appeal-forms input[value="投诉"]').check()
    popup.locator('#appeal-start').click()
    popup.wait_for_function("messages.some(m => m.type === 'START_APPEAL')")
    params = popup.evaluate("messages.find(m => m.type === 'START_APPEAL').params")
    assert params["screenshot_mode"] is True
    assert "MLB" in params["sites"] and "投诉" in params["forms"]
    assert toggle.is_disabled() and toggle.is_checked()
    popup.evaluate("ZeshunAppealPopup.setVisible(true)")
    assert toggle.is_checked()
    assert popup.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    popup.screenshot(path=str(ARTIFACTS / f"plugin-{width}.png"))


def test_metrics_explicit_package_excludes_sale_and_shipping_ids(page):
    page.set_content('''<table><tr data-order-id="2000000000000001"><td>Sale #2000000000000001</td>
        <td>Package #2000000000000099</td><td>Shipping 4000000000000001</td></tr></table>''')
    result = action(page, "orders")
    assert result["pack_ids"] == ["2000000000000099"]
    assert result["ids"] == []
    assert "2000000000000001" in page.locator("tr").inner_text()
