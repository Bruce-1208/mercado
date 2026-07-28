"""Mercado 后台限频识别与节点切换的唯一入口。

只有页面出现 Mercado 指定的西班牙语错误文案时才视为限频。
HTTP 429、``Too Many Requests``、``Access denied`` 等其他文案都不在
这里做限频推断，避免把登录、验证码或普通网络异常误判为限频。
"""

import re
import time
import unicodedata

from bit.bit_clash import switch_random_hongkong_node
from bit.bit_utils import get_now_time


MERCADO_RATE_LIMIT_TEXT = "Hubo un error accediendo a esta página"
_NORMALIZED_RATE_LIMIT_TEXT = "hubo un error accediendo a esta pagina"
MERCADO_LOGIN_URL_MARKERS = ("/login", "/lgz/", "/legacy-user")
MERCADO_LOGIN_TEXT_MARKERS = (
    "fill out your e-mail address to log in",
    "fill out your email address to log in",
    "log in to your account",
    "sign in to your account",
    "enter your e-mail address",
    "enter your email address",
    "ingresa tu e-mail",
    "ingrese su e-mail",
    "iniciar sesión",
    "iniciar sesion",
    "iniciar sessão",
    "iniciar sessao",
    "填写您的电子邮件地址以登录",
    "登录您的账户",
    "登录你的账户",
    "请登录",
)


def _normalize_visible_text(value):
    """去掉重音和多余空白，但不扩大限频文案的匹配范围。"""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def is_mercado_rate_limited_text(value):
    """只识别指定的 Mercado 西语限频文案。"""
    return _NORMALIZED_RATE_LIMIT_TEXT in _normalize_visible_text(value)


def get_mercado_page_state(driver):
    """读取 Selenium 页面的限频判断所需状态。"""
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    try:
        page_text = driver.execute_script(
            "return document.body ? document.body.innerText : '';"
        ) or ""
    except Exception:
        page_text = ""
    try:
        current_url = driver.current_url or ""
    except Exception:
        current_url = ""
    try:
        title = driver.title or ""
    except Exception:
        title = ""
    try:
        page_source = driver.page_source or ""
    except Exception:
        page_source = ""
    return {
        "page_text": str(page_text),
        "current_url": str(current_url),
        "title": str(title),
        "page_source": str(page_source),
    }


def get_playwright_mercado_page_state(page, timeout=3000):
    """读取 Playwright 页面的后台状态，与 Selenium 共用判断规则。"""
    try:
        page_text = page.locator("body").inner_text(timeout=timeout) or ""
    except Exception:
        page_text = ""
    try:
        current_url = page.url or ""
    except Exception:
        current_url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        page_source = page.content() or ""
    except Exception:
        page_source = ""
    return {
        "page_text": str(page_text),
        "current_url": str(current_url),
        "title": str(title),
        "page_source": str(page_source),
    }


def is_mercado_rate_limited_page(driver=None, state=None):
    """仅在可见页面出现指定西语时识别为限频。

    错误页偶尔没有可见正文；这时才回退检查源码，避免正常页的
    脚本或缓存文案造成误判。URL、导航异常和状态码不参与判断。
    """
    if state is None:
        if driver is None:
            state = {}
        else:
            state = get_mercado_page_state(driver)
    else:
        state = dict(state)

    page_text = str(state.get("page_text") or "")
    title = str(state.get("title") or "")
    if is_mercado_rate_limited_text(f"{page_text}\n{title}"):
        return True
    if page_text.strip() or title.strip():
        return False

    page_source = state.get("page_source")
    if page_source is None and driver is not None:
        try:
            page_source = driver.page_source or ""
        except Exception:
            page_source = ""
    return is_mercado_rate_limited_text(page_source)


def is_mercado_logged_out_state(state):
    """识别 Mercado 后台是否已跳转到登录页。"""
    state = dict(state or {})
    current_url = _normalize_visible_text(state.get("current_url"))
    visible_text = _normalize_visible_text(
        f"{state.get('page_text') or ''}\n{state.get('title') or ''}"
    )
    normalized_markers = tuple(
        _normalize_visible_text(marker) for marker in MERCADO_LOGIN_TEXT_MARKERS
    )
    return any(marker in current_url for marker in MERCADO_LOGIN_URL_MARKERS) or any(
        marker in visible_text for marker in normalized_markers
    )


def is_mercado_logged_out_page(driver=None, state=None):
    """读取页面并识别 Mercado 登录态是否已失效。"""
    if state is None:
        state = get_mercado_page_state(driver) if driver is not None else {}
    return is_mercado_logged_out_state(state)


def get_mercado_backend_status(driver=None, state=None):
    """返回 ``logged_out``、``rate_limited`` 或 ``ready``。

    登录 URL 优先级高于页面错误文案，避免把已退出登录的窗口
    盲目切换 IP。
    """
    if state is None:
        state = get_mercado_page_state(driver) if driver is not None else {}
    if is_mercado_logged_out_state(state):
        return "logged_out"
    if is_mercado_rate_limited_page(driver=driver, state=state):
        return "rate_limited"
    return "ready"


def process_mercado_rate_limit(
    driver=None,
    state=None,
    *,
    name="",
    site="",
    retry_count=0,
    max_retries=2,
    retry_wait_seconds=30,
    switcher=None,
    after_switch=None,
    sleep=None,
):
    """检测并处理一次 Mercado 限频。

    返回值中 ``retry`` 表示调用方应重新打开页面；``exhausted``
    表示已用完允许的节点切换次数。非指定西语页不切换 IP。
    """
    retry_count = max(0, int(retry_count))
    max_retries = max(0, int(max_retries))
    if not is_mercado_rate_limited_page(driver=driver, state=state):
        return {
            "rate_limited": False,
            "retry": False,
            "exhausted": False,
            "retry_count": retry_count,
            "node_switch_result": {},
        }

    if retry_count >= max_retries:
        return {
            "rate_limited": True,
            "retry": False,
            "exhausted": True,
            "retry_count": retry_count,
            "node_switch_result": {},
        }

    label = " ".join(
        part for part in (str(name or "").strip(), str(site or "").strip()) if part
    )
    print(
        f"{get_now_time()} {label} 检测到指定西语限频页，"
        "正在切换香港节点".strip(),
        flush=True,
    )
    switcher = switch_random_hongkong_node if switcher is None else switcher
    try:
        node_switch_result = switcher() or {}
    except Exception as exc:
        node_switch_result = {
            "switched": False,
            "reason": "exception",
            "error": str(exc),
        }
    if after_switch is not None:
        try:
            after_switch()
        except Exception as exc:
            node_switch_result = dict(node_switch_result)
            node_switch_result["after_switch_error"] = str(exc)

    next_retry_count = retry_count + 1
    wait_seconds = max(0, float(retry_wait_seconds))
    print(
        f"{get_now_time()} {label} 限频后准备第 "
        f"{next_retry_count}/{max_retries} 次重试，等待 {wait_seconds:g} 秒".strip(),
        flush=True,
    )
    sleeper = time.sleep if sleep is None else sleep
    sleeper(wait_seconds)
    return {
        "rate_limited": True,
        "retry": True,
        "exhausted": False,
        "retry_count": next_retry_count,
        "node_switch_result": dict(node_switch_result),
    }
