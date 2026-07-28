"""为 Mercado 页面自动化点击统一增加冷却时间。"""

import functools
import re
import time
from urllib.parse import urlparse


MERCADO_CLICK_DELAY_SECONDS = 2.0
_JAVASCRIPT_CLICK_PATTERN = re.compile(r"\.click\s*\(", re.IGNORECASE)
_JAVASCRIPT_CLICK_EVENT_PATTERN = re.compile(r"['\"]click['\"]", re.IGNORECASE)


def is_mercado_url(url):
    try:
        host = (urlparse(str(url or "")).hostname or "").casefold()
    except Exception:
        return False
    return any(
        marker in host
        for marker in (
            "mercadolibre.",
            "mercadolivre.",
            "mercadopago.",
            "mercadoshops.",
        )
    )


def javascript_contains_click(script):
    text = str(script or "")
    return bool(
        _JAVASCRIPT_CLICK_PATTERN.search(text)
        or (
            "dispatchEvent" in text
            and _JAVASCRIPT_CLICK_EVENT_PATTERN.search(text)
        )
    )


def mercado_click_cooldown():
    time.sleep(MERCADO_CLICK_DELAY_SECONDS)


def wait_after_mercado_click(url=""):
    if not is_mercado_url(url):
        return False
    mercado_click_cooldown()
    return True


def _safe_url(target):
    try:
        value = getattr(target, "current_url", "")
        if callable(value):
            value = value()
        if value:
            return str(value)
    except Exception:
        pass
    try:
        value = getattr(target, "url", "")
        if callable(value):
            value = value()
        if value:
            return str(value)
    except Exception:
        pass
    try:
        impl = getattr(target, "_impl_obj", None)
        frame = getattr(impl, "_frame", None) or impl
        page = getattr(frame, "_page", None)
        value = getattr(page, "url", "")
        if callable(value):
            value = value()
        return str(value or "")
    except Exception:
        return ""


def _wait_for_target(target, fallback_url=""):
    return wait_after_mercado_click(_safe_url(target) or fallback_url)


def install_selenium_click_delay():
    """覆盖 Selenium 的真实元素点击与含 click() 的 JS 执行入口。"""
    try:
        from selenium.webdriver.remote.webdriver import WebDriver
        from selenium.webdriver.remote.webelement import WebElement
    except ImportError:
        return False

    if not getattr(WebElement, "_mercado_click_delay_installed", False):
        original_element_click = WebElement.click

        @functools.wraps(original_element_click)
        def delayed_element_click(element, *args, **kwargs):
            parent = getattr(element, "_parent", None)
            before_url = _safe_url(parent)
            result = original_element_click(element, *args, **kwargs)
            _wait_for_target(parent, before_url)
            return result

        WebElement.click = delayed_element_click
        WebElement._mercado_click_delay_installed = True

    if not getattr(WebDriver, "_mercado_js_click_delay_installed", False):
        original_execute_script = WebDriver.execute_script

        @functools.wraps(original_execute_script)
        def delayed_execute_script(driver, script, *args):
            has_click = javascript_contains_click(script)
            before_url = _safe_url(driver) if has_click else ""
            result = original_execute_script(driver, script, *args)
            if has_click and result is not False:
                _wait_for_target(driver, before_url)
            return result

        WebDriver.execute_script = delayed_execute_script
        WebDriver._mercado_js_click_delay_installed = True

    return True


def _install_playwright_click_method(target_class, method_name):
    marker = f"_mercado_{method_name}_delay_installed"
    if getattr(target_class, marker, False):
        return
    original = getattr(target_class, method_name)

    @functools.wraps(original)
    def delayed(target, *args, **kwargs):
        before_url = _safe_url(target)
        result = original(target, *args, **kwargs)
        _wait_for_target(target, before_url)
        return result

    setattr(target_class, method_name, delayed)
    setattr(target_class, marker, True)


def _install_playwright_evaluate_method(target_class, method_name="evaluate"):
    marker = f"_mercado_{method_name}_click_delay_installed"
    if getattr(target_class, marker, False):
        return
    original = getattr(target_class, method_name)

    @functools.wraps(original)
    def delayed(target, expression, *args, **kwargs):
        has_click = javascript_contains_click(expression)
        before_url = _safe_url(target) if has_click else ""
        result = original(target, expression, *args, **kwargs)
        if has_click and result is not False:
            _wait_for_target(target, before_url)
        return result

    setattr(target_class, method_name, delayed)
    setattr(target_class, marker, True)


def install_playwright_click_delay():
    """覆盖 Playwright 同步 API 的元素、鼠标和 JS 点击入口。"""
    try:
        from playwright.sync_api import ElementHandle, Frame, Locator, Mouse, Page
    except ImportError:
        return False

    for target_class in (Locator, ElementHandle, Page, Frame, Mouse):
        if hasattr(target_class, "click"):
            _install_playwright_click_method(target_class, "click")
    for target_class in (Page, Frame, ElementHandle, Locator):
        if hasattr(target_class, "evaluate"):
            _install_playwright_evaluate_method(target_class)
    return True
