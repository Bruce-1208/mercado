"""Mercado 店铺登录态检测，供申诉、声誉和侵权任务共用。"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from selenium.webdriver.common.by import By

from bit.bit_send_mail import send_info
from bit.bit_utils import get_bit_path, get_now_time


LOGIN_URL_MARKERS = ("/login", "/lgz/", "/legacy-user")
LOGIN_TEXT_MARKERS = (
    "fill out your e-mail address to log in",
    "fill out your email address to log in",
    "enter your e-mail address",
    "enter your email address",
    "ingresa tu e-mail",
    "ingrese su e-mail",
    "iniciar sesión",
    "iniciar sesion",
    "填写您的电子邮件地址以登录",
)
VERIFICATION_TEXT_MARKERS = (
    "enter the verification code",
    "enter verification code",
    "enter the security code",
    "we sent you a code",
    "code sent to your email",
    "check your email for a code",
    "verify your identity",
    "confirm your identity",
    "verify it's you",
    "we will send you a code",
    "ingresa el código",
    "ingrese el código",
    "código de verificación",
    "codigo de verificacion",
    "código de seguridad",
    "verifica tu identidad",
    "confirma tu identidad",
    "digite o código",
    "código de verificação",
    "verifique sua identidade",
    "验证码",
    "输入代码",
)
CAPTCHA_TEXT_MARKERS = (
    "i'm not a robot",
    "i am not a robot",
    "im not a robot",
    "no soy un robot",
    "não sou um robô",
    "nao sou um robo",
    "人机验证",
    "我不是机器人",
    "recaptcha",
    "hcaptcha",
)

EMAIL_INPUT_SELECTORS = (
    "input[type='email']",
    "#user_id",
    "input[name='email']",
    "input[name='user_id']",
    "input[autocomplete='username']",
    "input[placeholder*='mail' i]",
    "input[id*='email']",
    "input[id*='user']",
)
PASSWORD_INPUT_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
    "#password",
)
CODE_INPUT_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name*='code']",
    "input[id*='code']",
    "input[name*='otp']",
    "input[id*='otp']",
)
CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[title*='recaptcha' i]",
    "iframe[src*='hcaptcha']",
    "iframe[title*='hcaptcha' i]",
    ".g-recaptcha",
    ".h-captcha",
    "[data-sitekey]",
)
LOGIN_ALREADY_ACTIVE = "已登录"
LOGIN_NOT_LOGGED_IN = "未登录"
LOGIN_VERIFICATION_REQUIRED = "需要验证码"
LOGIN_CAPTCHA_REQUIRED = "需要人机验证"
LOGIN_FAILED = "登录状态检测失败"
MERCADO_HOME_URL = "https://global-selling.mercadolibre.com/"
LOGIN_EVENT_LOG_PATH = get_bit_path() / "logs" / "mercado_unlogged_shops.jsonl"
_LOGIN_EVENT_LOG_GUARD = threading.Lock()


def _normalized_header(value):
    return "".join(str(value or "").strip().lower().split())


def load_shop_login_config(shop_name, window_id="", config_path=None):
    """按表头读取店铺窗口及邮箱，不依赖固定列号。"""
    path = Path(config_path or (get_bit_path() / "比特配置文件.xlsx"))
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        headers = next(rows, ())
        header_map = {
            _normalized_header(value): index
            for index, value in enumerate(headers)
            if _normalized_header(value)
        }

        def column(*names):
            for name in names:
                index = header_map.get(_normalized_header(name))
                if index is not None:
                    return index
            return None

        window_column = column("窗口ID", "窗口 id", "浏览器窗口ID")
        name_column = column("账号名", "店铺名", "窗口名称")
        email_column = column("邮箱", "电子邮箱", "美客多邮箱", "登录邮箱", "email", "e-mail")

        if name_column is None and window_column is None:
            raise RuntimeError("比特配置文件缺少“账号名”或“窗口ID”表头")

        requested_name = str(shop_name or "").strip()
        requested_window = str(window_id or "").strip()
        for row in rows:
            row_name = (
                str(row[name_column] or "").strip()
                if name_column is not None and name_column < len(row)
                else ""
            )
            row_window = (
                str(row[window_column] or "").strip()
                if window_column is not None and window_column < len(row)
                else ""
            )
            matches = (
                row_window == requested_window
                if requested_window
                else bool(requested_name and row_name == requested_name)
            )
            if not matches:
                continue
            email = (
                str(row[email_column] or "").strip()
                if email_column is not None and email_column < len(row)
                else ""
            )
            return {
                "shop_name": row_name or requested_name,
                "window_id": row_window or requested_window,
                "email": email,
                "email_column_exists": email_column is not None,
                "config_path": str(path),
            }
    finally:
        workbook.close()

    raise RuntimeError(f"未在比特配置文件中找到店铺：{shop_name or window_id}")


def _page_snapshot(driver):
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
    return {
        "page_text": str(page_text),
        "current_url": str(current_url),
        "title": str(title),
    }


def _is_visible_enabled(element):
    try:
        return element.is_displayed() and element.is_enabled()
    except Exception:
        return False


def _visible_elements_in_current_context(driver, selectors):
    elements = []
    seen = set()
    for selector in selectors:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in found:
            marker = getattr(element, "id", None) or id(element)
            if marker in seen:
                continue
            seen.add(marker)
            if _is_visible_enabled(element):
                elements.append(element)

    if elements:
        return elements

    # Selenium 的普通 find_elements 不会穿透 Web Component 的 Shadow DOM。
    try:
        shadow_elements = driver.execute_script(
            """
            const selectors = arguments[0];
            const results = [];
            const seen = new Set();
            function addMatches(root) {
                if (!root || !root.querySelectorAll) return;
                for (const selector of selectors) {
                    let nodes = [];
                    try { nodes = root.querySelectorAll(selector); } catch (_) {}
                    for (const node of nodes) {
                        if (!seen.has(node)) {
                            seen.add(node);
                            results.push(node);
                        }
                    }
                }
                let descendants = [];
                try { descendants = root.querySelectorAll('*'); } catch (_) {}
                for (const node of descendants) {
                    if (node.shadowRoot) addMatches(node.shadowRoot);
                }
            }
            addMatches(document);
            return results;
            """,
            list(selectors),
        ) or []
    except Exception:
        shadow_elements = []
    for element in shadow_elements:
        marker = getattr(element, "id", None) or id(element)
        if marker in seen:
            continue
        seen.add(marker)
        if _is_visible_enabled(element):
            elements.append(element)
    return elements


def _search_visible_elements_in_frames(driver, selectors, depth=0, max_depth=4):
    elements = _visible_elements_in_current_context(driver, selectors)
    if elements or depth >= max_depth:
        return elements

    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except Exception:
        frames = []
    for frame in frames:
        try:
            if not frame.is_displayed():
                continue
            driver.switch_to.frame(frame)
        except Exception:
            continue
        elements = _search_visible_elements_in_frames(
            driver,
            selectors,
            depth=depth + 1,
            max_depth=max_depth,
        )
        if elements:
            # 保持在找到控件的 frame 中，后续输入、点击必须使用同一上下文。
            return elements
        try:
            driver.switch_to.parent_frame()
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return []


def _visible_elements(driver, selectors):
    # 登录探测需要测试许多候选选择器。若继承业务代码的 10 秒隐式等待，
    # 每个未命中的选择器都会单独等待，最终可能耗时数分钟。
    implicit_wait_changed = False
    try:
        driver.implicitly_wait(0)
        implicit_wait_changed = True
    except Exception:
        pass
    try:
        # 优先搜索当前 frame，用于判断登录控件所处的页面上下文。
        elements = _visible_elements_in_current_context(driver, selectors)
        if elements:
            return elements
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        elements = _search_visible_elements_in_frames(driver, selectors)
        if elements:
            return elements
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return []
    finally:
        if implicit_wait_changed:
            try:
                driver.implicitly_wait(10)
            except Exception:
                pass


def is_mercado_login_page(driver):
    if not driver:
        return False
    state = _page_snapshot(driver)
    combined = "\n".join(state.values()).casefold()
    if _visible_elements(
        driver,
        EMAIL_INPUT_SELECTORS
        + PASSWORD_INPUT_SELECTORS
        + CODE_INPUT_SELECTORS
        + CAPTCHA_SELECTORS,
    ):
        return True
    return any(
        marker in combined
        for marker in (
            LOGIN_URL_MARKERS
            + LOGIN_TEXT_MARKERS
            + VERIFICATION_TEXT_MARKERS
            + CAPTCHA_TEXT_MARKERS
        )
    )


def detect_login_stage(driver):
    """返回 captcha/email/password/verification/login/logged_in。"""
    if _visible_elements(driver, CAPTCHA_SELECTORS):
        return "captcha"
    if _visible_elements(driver, CODE_INPUT_SELECTORS):
        return "verification"
    if _visible_elements(driver, PASSWORD_INPUT_SELECTORS):
        return "password"
    if _visible_elements(driver, EMAIL_INPUT_SELECTORS):
        return "email"

    state = _page_snapshot(driver)
    combined = "\n".join(state.values()).casefold()
    if any(marker in combined for marker in CAPTCHA_TEXT_MARKERS):
        return "captcha"
    if any(marker in combined for marker in VERIFICATION_TEXT_MARKERS):
        return "verification"
    if any(marker in combined for marker in LOGIN_URL_MARKERS + LOGIN_TEXT_MARKERS):
        return "login"
    return "logged_in"


def _result(ok, status, message, **extra):
    return {"ok": bool(ok), "status": status, "message": message, **extra}


def ensure_mercado_login(
    driver,
    shop_name,
    window_id="",
    config_path=None,
    default_password=None,
    wait_seconds=20,
    alert_sender=None,
):
    """兼容旧调用名：仅检测登录状态，绝不输入账号、密码或提交表单。"""
    del window_id, config_path, default_password, wait_seconds, alert_sender
    if not is_mercado_login_page(driver):
        return _result(True, LOGIN_ALREADY_ACTIVE, LOGIN_ALREADY_ACTIVE)

    stage = detect_login_stage(driver)
    print(
        f"{get_now_time()} {shop_name} 登录页面识别阶段：{stage}",
        flush=True,
    )
    stage_labels = {
        "captcha": LOGIN_CAPTCHA_REQUIRED,
        "verification": LOGIN_VERIFICATION_REQUIRED,
        "email": "邮箱登录页",
        "password": "密码登录页",
        "login": "登录页",
    }
    detail = stage_labels.get(stage, stage)
    return _result(
        False,
        LOGIN_NOT_LOGGED_IN,
        f"{shop_name} 未登录（{detail}），不执行任何自动登录操作",
        login_stage=stage,
    )


def ensure_mercado_login_from_home(
    driver,
    shop_name,
    window_id="",
    config_path=None,
    default_password=None,
    wait_seconds=20,
    navigation_wait_seconds=5,
    alert_sender=None,
):
    """固定访问 Global Selling 首页，仅判断登录状态，不执行自动登录。"""
    print(
        f"{get_now_time()} {shop_name} 正在访问美客多首页检测登录状态："
        f"{MERCADO_HOME_URL}<br>",
        flush=True,
    )
    navigation_error = ""
    navigation_method = ""
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # 附着到 BitBrowser 的 ChromeDriver 在 driver.get() 上可能长期等待页面资源。
    # CDP Page.navigate 只发出导航命令，不等待 load 事件，因此适合登录态探测。
    try:
        driver.execute_cdp_cmd("Page.navigate", {"url": MERCADO_HOME_URL})
        navigation_method = "cdp"
    except Exception as cdp_error:
        try:
            driver.execute_script(
                "window.location.replace(arguments[0]);",
                MERCADO_HOME_URL,
            )
            navigation_method = "javascript"
        except Exception as js_error:
            navigation_error = f"CDP导航失败：{cdp_error}；JS导航失败：{js_error}"

    settle_seconds = max(0, float(navigation_wait_seconds))
    settle_deadline = time.monotonic() + settle_seconds
    while time.monotonic() < settle_deadline:
        remaining = max(0, int(settle_deadline - time.monotonic()))
        print(
            f"{get_now_time()} {shop_name} 首页导航已发出"
            f"（{navigation_method or '失败'}），等待页面稳定，剩余 {remaining} 秒",
            flush=True,
        )
        time.sleep(min(1, max(0, settle_deadline - time.monotonic())))

    # 不调用 Page.stopLoading：部分 BitBrowser/ChromeDriver 组合会在该 CDP
    # 命令上无限等待。登录探测使用 0 秒隐式等待，无需停止页面加载。
    print(
        f"{get_now_time()} {shop_name} 首页稳定等待结束，开始扫描登录控件",
        flush=True,
    )

    login_detected = is_mercado_login_page(driver)
    state = _page_snapshot(driver)
    current_url = str(state.get("current_url") or "").casefold()
    reached_mercado = "mercadolibre.com" in current_url

    if navigation_error and not reached_mercado:
        result = _result(
            False,
            LOGIN_FAILED,
            f"无法打开美客多首页进行登录检测：{navigation_error}",
        )
    elif not reached_mercado:
        result = _result(
            False,
            LOGIN_FAILED,
            f"首页导航后未进入 Mercado 页面：{state.get('current_url') or '空地址'}",
        )
    elif login_detected:
        result = ensure_mercado_login(
            driver,
            shop_name,
            window_id=window_id,
            config_path=config_path,
            default_password=default_password,
            wait_seconds=wait_seconds,
            alert_sender=alert_sender,
        )
    else:
        result = _result(True, LOGIN_ALREADY_ACTIVE, LOGIN_ALREADY_ACTIVE)

    return {
        **result,
        "login_detected_before": login_detected,
        "navigation_error": navigation_error,
        "navigation_method": navigation_method,
        "login_check_url": MERCADO_HOME_URL,
    }


def is_login_blocking_result(value):
    text = str(value or "")
    return any(
        marker in text
        for marker in (
            "未登录",
            LOGIN_VERIFICATION_REQUIRED,
            LOGIN_CAPTCHA_REQUIRED,
            LOGIN_FAILED,
        )
    )


def unlogged_entries_from_task_records(records):
    """从任务记录五元组中提取未登录店铺，按店铺去重并合并站点。"""
    entries = {}
    for record in records or []:
        if not isinstance(record, (list, tuple)) or len(record) < 4:
            continue
        status = str(record[3] or "")
        if "未登录" not in status and "登录失效" not in status:
            continue
        shop_name = str(record[1] or "").strip()
        site = str(record[2] or "").strip()
        if not shop_name:
            continue
        entry = entries.setdefault(
            shop_name,
            {"shop_name": shop_name, "sites": set(), "details": set()},
        )
        if site:
            entry["sites"].add(site)
        if status:
            entry["details"].add(status)
    return [
        {
            "shop_name": entry["shop_name"],
            "sites": sorted(entry["sites"]),
            "details": sorted(entry["details"]),
        }
        for entry in sorted(entries.values(), key=lambda item: item["shop_name"])
    ]


def _normalize_unlogged_entries(entries):
    normalized = {}
    for raw in entries or []:
        if isinstance(raw, str):
            raw = {"shop_name": raw}
        if not isinstance(raw, dict):
            continue
        shop_name = str(raw.get("shop_name") or raw.get("name") or "").strip()
        if not shop_name:
            continue
        entry = normalized.setdefault(
            shop_name,
            {"shop_name": shop_name, "sites": set(), "details": set()},
        )
        sites = raw.get("sites") or ([raw.get("site")] if raw.get("site") else [])
        details = raw.get("details") or ([raw.get("detail")] if raw.get("detail") else [])
        for site in sites:
            if str(site or "").strip():
                entry["sites"].add(str(site).strip())
        for detail in details:
            if str(detail or "").strip():
                entry["details"].add(str(detail).strip())
    return [
        {
            "shop_name": entry["shop_name"],
            "sites": sorted(entry["sites"]),
            "details": sorted(entry["details"]),
        }
        for entry in sorted(normalized.values(), key=lambda item: item["shop_name"])
    ]


def record_unlogged_shop_summary(task_name, entries, log_path=None):
    """把本批次未登录店铺写入 JSONL 日志，一批一行。"""
    normalized = _normalize_unlogged_entries(entries)
    if not normalized:
        return ""
    path = Path(log_path or LOGIN_EVENT_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "task_name": str(task_name or "美客多任务"),
        "shop_count": len(normalized),
        "shops": normalized,
    }
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with _LOGIN_EVENT_LOG_GUARD:
        with path.open("a", encoding="utf-8") as file:
            file.write(line)
    print(f"{get_now_time()} 未登录店铺日志已写入：{path}<br>")
    return str(path)


def send_unlogged_shop_summary(task_name, entries, log_path=None):
    """所有店铺任务结束后发送一封未登录店铺汇总邮件。"""
    normalized = _normalize_unlogged_entries(entries)
    if not normalized:
        print(f"{get_now_time()} {task_name} 本批次没有未登录店铺，无需发送提醒<br>")
        return False

    path = record_unlogged_shop_summary(task_name, normalized, log_path=log_path)
    lines = [
        f"任务：{task_name}",
        f"结束时间：{get_now_time()}",
        f"未登录店铺数量：{len(normalized)}",
        "",
    ]
    for index, entry in enumerate(normalized, start=1):
        sites = "、".join(entry["sites"]) or "全部/未指定站点"
        details = "；".join(entry["details"]) or "检测到未登录状态"
        lines.append(f"{index}. {entry['shop_name']}｜站点：{sites}｜{details}")
    body = "\n".join(lines)
    sent = send_info(
        f"美客多未登录店铺提醒：{task_name}（{len(normalized)}家）",
        body,
        path,
        Path(path).name if path else "",
    )
    if sent is False:
        print(f"{get_now_time()} {task_name} 未登录店铺汇总邮件发送失败<br>")
        return False
    print(f"{get_now_time()} {task_name} 未登录店铺汇总邮件已发送<br>")
    return True


def build_command_line_parser():
    """构建真实 BitBrowser 登录状态检测的命令行参数。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="打开指定比特店铺并检测 Mercado 登录状态，不执行自动登录。"
    )
    parser.add_argument(
        "--shop",
        required=True,
        help="比特配置文件中的账号名，例如：龙凤呈祥",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=30,
        help="兼容旧命令保留；只检测登录状态时不会用于自动登录",
    )
    parser.add_argument(
        "--page-load-timeout",
        type=int,
        default=20,
        help="打开美客多 Global Selling 首页的超时秒数，默认 20",
    )
    parser.add_argument(
        "--no-navigate",
        action="store_true",
        help="不打开 Global Selling 首页，只检测比特窗口当前页面",
    )
    return parser


def run_login_test_from_command_line(args):
    """执行命令行登录状态检测；不自动登录，也不关闭浏览器窗口。"""
    import json
    from urllib.parse import urlparse

    import requests
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    from bit.bit_api import openBrowser
    from bit.bit_runtime_lock import create_window_lease

    shop_name = str(args.shop or "").strip()
    print(
        f"[{get_now_time()}] 读取店铺配置：{shop_name}",
        flush=True,
    )
    config = load_shop_login_config(shop_name)
    window_id = config["window_id"]
    lease = create_window_lease(
        window_id,
        owner=f"mercado_login_cli:{shop_name}",
        shop_name=shop_name,
        task_type="mercado_login_test",
    )
    if not lease.acquire(timeout=0):
        result = {
            "ok": False,
            "status": "窗口正在被其他任务占用",
            "shop": shop_name,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 2

    try:
        print(
            f"[{get_now_time()}] 已获取窗口任务锁，正在打开比特浏览器...",
            flush=True,
        )
        response = openBrowser(window_id)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict) or not data.get("driver") or not data.get("http"):
            raise RuntimeError(f"打开比特浏览器失败：{response}")

        debugger_address = str(data["http"]).strip()
        debugger_url = (
            debugger_address
            if debugger_address.startswith(("http://", "https://"))
            else f"http://{debugger_address}"
        )
        print(
            f"[{get_now_time()}] BitBrowser 已返回调试地址，等待浏览器调试端口就绪...",
            flush=True,
        )
        debugger_error = ""
        debugger_ready = False
        for attempt in range(1, 7):
            try:
                debugger_response = requests.get(
                    f"{debugger_url.rstrip('/')}/json/version",
                    timeout=3,
                )
                debugger_response.raise_for_status()
                debugger_ready = True
                break
            except Exception as exc:
                debugger_error = str(exc)
                print(
                    f"[{get_now_time()}] 调试端口尚未就绪，"
                    f"第 {attempt}/6 次：{debugger_error[:160]}",
                    flush=True,
                )
                if attempt < 6:
                    time.sleep(2)
        if not debugger_ready:
            raise RuntimeError(
                f"BitBrowser 调试端口未就绪：{debugger_error or debugger_address}"
            )

        options = webdriver.ChromeOptions()
        options.add_experimental_option("debuggerAddress", debugger_address)
        driver = webdriver.Chrome(
            service=Service(data["driver"]),
            options=options,
        )
        driver.implicitly_wait(10)
        print(
            f"[{get_now_time()}] 已连接比特浏览器 Selenium 会话",
            flush=True,
        )
        try:
            driver.set_page_load_timeout(max(1, int(args.page_load_timeout)))
        except Exception:
            pass

        navigate_error = ""
        if not args.no_navigate:
            print(
                f"[{get_now_time()}] 正在打开美客多首页检测登录状态，页面超时 "
                f"{max(1, int(args.page_load_timeout))} 秒...",
                flush=True,
            )
            result = ensure_mercado_login_from_home(
                driver,
                shop_name,
                window_id=window_id,
                wait_seconds=max(0, int(args.wait_seconds)),
                navigation_wait_seconds=5,
            )
            navigate_error = str(result.get("navigation_error") or "")[:240]
            login_detected = bool(result.get("login_detected_before"))
        else:
            login_detected = is_mercado_login_page(driver)
            print(
                f"[{get_now_time()}] 当前页面检测完成："
                f"{'检测到登录页' if login_detected else '当前不是登录页'}",
                flush=True,
            )
            result = ensure_mercado_login(
                driver,
                shop_name,
                window_id=window_id,
                wait_seconds=max(0, int(args.wait_seconds)),
            )
        try:
            page_host = urlparse(driver.current_url or "").netloc
        except Exception:
            page_host = ""
        safe_result = {
            "ok": bool(result.get("ok")),
            "status": result.get("status", ""),
            "message": result.get("message", ""),
            "shop": shop_name,
            "login_detected_before": login_detected,
            "page_host": page_host,
            "navigate_error": navigate_error,
            "browser_kept_open": True,
        }
        print(json.dumps(safe_result, ensure_ascii=False), flush=True)
        return 0 if safe_result["ok"] else 3
    finally:
        lease.release()
        print(
            f"[{get_now_time()}] 已释放窗口任务锁；比特浏览器保持打开",
            flush=True,
        )


def main(argv=None):
    parser = build_command_line_parser()
    args = parser.parse_args(argv)
    try:
        return run_login_test_from_command_line(args)
    except Exception as exc:
        import json
        import traceback

        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "测试异常",
                    "shop": str(getattr(args, "shop", "") or ""),
                    "message": str(exc),
                    "browser_kept_open": True,
                },
                ensure_ascii=False,
            )
        )
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
