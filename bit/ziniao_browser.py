"""Local ZiNiao WebDriver bridge. Credentials never enter task payloads."""
import os
from pathlib import Path
import uuid

import requests

PREFIX = "ziniao:"


def normalize_provider(value):
    provider = str(value or "bitbrowser").strip().lower()
    if provider not in {"bitbrowser", "ziniao"}:
        raise ValueError("请选择比特浏览器或紫鸟浏览器")
    return provider


def is_ziniao(window_id):
    return str(window_id).startswith(PREFIX)


def request(action, *, timeout=120, **fields):
    credentials = {key: os.getenv("ZINIAO_" + key.upper(), "").strip()
                   for key in ("company", "username", "password")}
    if not all(credentials.values()):
        raise RuntimeError("请在执行电脑配置 ZINIAO_COMPANY、ZINIAO_USERNAME、ZINIAO_PASSWORD 后重启 Agent")
    try:
        port = int(os.getenv("ZINIAO_PORT", "16851"))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise RuntimeError("ZINIAO_PORT 必须是有效的本机端口") from None
    try:
        # Never use an HTTP proxy for the local credential-bearing request.
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(f"http://127.0.0.1:{port}", json={
                **credentials, **fields, "action": action, "requestId": str(uuid.uuid4()),
            }, timeout=timeout)
            response.raise_for_status()
            result = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("紫鸟本机接口不可用，请以 WebDriver HTTP 模式启动紫鸟并核对端口") from None
    if not isinstance(result, dict) or str(result.get("statusCode")) != "0":
        # Do not echo an API response: it may contain account credentials.
        raise RuntimeError("紫鸟接口执行失败，请检查企业登录信息、店铺权限及客户端版本")
    return result


def list_browsers():
    rows = request("getBrowserList").get("browserList")
    if not isinstance(rows, list):
        raise RuntimeError("紫鸟店铺列表格式异常")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("紫鸟店铺信息格式异常")
        # Use the encrypted ID for both startBrowser and stopBrowser.
        identity = str(row.get("browserOauth") or "").strip()
        name = str(row.get("browserName") or "").strip()
        if not identity or not name:
            raise RuntimeError("紫鸟店铺缺少名称或 browserOauth，无法安全匹配")
        result.append({"id": PREFIX + identity, "name": name})
    return result


def _identity(window_id):
    if not is_ziniao(window_id) or not str(window_id)[len(PREFIX):]:
        raise ValueError("无效的紫鸟店铺 ID")
    return str(window_id)[len(PREFIX):]


def open_browser(window_id, timeout=120):
    result = request("startBrowser", timeout=timeout, browserOauth=_identity(window_id),
                     isWaitPluginUpdate=0, isHeadless=0, isWebDriverReadOnlyMode=0,
                     cookieTypeLoad=0, cookieTypeSave=0, runMode="1",
                     privacyMode=0, isLoadUserPlugin=True, pluginIdType=1)
    try:
        if result.get("core_type") not in (None, 0, "Chromium"):
            raise RuntimeError("任务模块需要紫鸟 Chrome 内核，请调整该店铺的内核")
        port = int(result.get("debuggingPort") or 0)
        if not 1 <= port <= 65535:
            raise RuntimeError("紫鸟未返回有效的调试端口")
        browser_path = Path(str(result.get("browserPath") or ""))
        if browser_path.name.lower() in {"superbrowser", "superbrowser.exe"}:
            browser_path = browser_path.parent
        driver_name = "webdriver.exe" if os.name == "nt" else "webdriver"
        candidates = [browser_path / driver_name] if result.get("browserPath") else []
        override = os.getenv("ZINIAO_DRIVER_PATH", "").strip()
        if override:
            candidates.insert(0, Path(override).expanduser())
        driver = next((path for path in candidates if path.is_file()), None)
        if driver is None:
            raise RuntimeError("未找到紫鸟内核对应的 WebDriver，请配置 ZINIAO_DRIVER_PATH")
        return {"success": True, "data": {"id": window_id,
                "http": f"127.0.0.1:{port}", "driver": str(driver),
                "ipDetectionPage": str(result.get("ipDetectionPage") or "")}}
    except Exception:
        # A partially opened task must not leave an unattended shop window.
        try:
            close_browser(window_id, timeout=30)
        except Exception:
            pass
        raise


def close_browser(window_id, timeout=30):
    request("stopBrowser", timeout=timeout, browserOauth=_identity(window_id), duplicate=0)
    return {"success": True}


def resolve_aliases(aliases, browsers):
    """Require one unique shop across all authorization aliases."""
    def compact(value):
        return "".join(c for c in str(value).casefold() if c.isalnum())
    wanted = {compact(alias) for alias in aliases if compact(alias)}
    matches = [row for row in browsers if compact(row.get("name", "")) in wanted]
    if not matches:
        raise RuntimeError("未找到与授权显示名或昵称对应的紫鸟店铺")
    if len(matches) != 1:
        raise RuntimeError("授权名称匹配到多个紫鸟店铺，请将店铺名称设为唯一后重试")
    return matches[0]["id"]


def check_ip(driver, url):
    """Honor ZiNiao's IP check before navigating to a business page."""
    if not url:
        raise RuntimeError("紫鸟未返回 IP 检测页，请升级客户端后重试")
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    driver.get(url)
    try:
        WebDriverWait(driver, 30).until(EC.presence_of_element_located(
            (By.XPATH, '//button[contains(@class, "styles_btn--success")]')))
    except Exception:
        raise RuntimeError("紫鸟店铺 IP 检测未通过，已停止任务") from None
