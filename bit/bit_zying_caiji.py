import argparse
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps
from pathlib import Path


if __package__ in (None, ""):
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

try:
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
        line_buffering=True,
    )
    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
        line_buffering=True,
    )
except (AttributeError, ValueError):
    pass

import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_api import getBrowserIdByName, openBrowser, releaseBrowserLease
from bit.bit_mysql import (
    get_existing_zying_product_ids,
    insert_zying_product_info,
)


DEFAULT_ZYING_WINDOW_ID = os.environ.get(
    "BIT_ZYING_WINDOW_ID",
    "9812f185f7ab49d98f3988994d9e8ebf",
)
DEFAULT_ZYING_BROWSER_TYPE = os.environ.get(
    "BIT_ZYING_BROWSER_TYPE",
    "bitbrowser",
)
DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS = os.environ.get(
    "BIT_ZYING_EDGE_DEBUGGER_ADDRESS",
    "127.0.0.1:9222",
)
ZYING_PRODUCT_URL = os.environ.get(
    "BIT_ZYING_PRODUCT_URL",
    "https://meli.zying.net/#/product",
)
ZYING_API_ORIGIN = os.environ.get(
    "BIT_ZYING_API_ORIGIN",
    "https://meli.zying.net",
).rstrip("/")
DEFAULT_ZYING_PAGE_COUNT = max(1, int(os.environ.get("BIT_ZYING_PAGES", "1")))
DEFAULT_ZYING_START_PAGE = max(
    1,
    int(os.environ.get("BIT_ZYING_START_PAGE", "1")),
)
DEFAULT_ZYING_CATEGORY = os.environ.get("BIT_ZYING_CATEGORY", "")
ZYING_AUTH_FILE = Path(
    os.environ.get(
        "BIT_ZYING_AUTH_FILE",
        Path(__file__).resolve().parent / "runtime_locks" / "zying_auth.json",
    )
)
ZYING_EDGE_PROFILE_DIR = Path(
    os.environ.get(
        "BIT_ZYING_EDGE_PROFILE_DIR",
        Path(__file__).resolve().parent / "runtime_locks" / "zying_edge_profile",
    )
)
ZYING_API_PAGE_SIZE = max(1, min(int(os.environ.get("BIT_ZYING_PAGE_SIZE", "60")), 500))
ZYING_MELI_PLATFORM_ID = 8
ZYING_COOKIE_CREDENTIAL_PREFIX = "cookie://"
ZYING_DETAIL_WORKERS = max(1, int(os.environ.get("BIT_ZYING_DETAIL_WORKERS", "6")))
ZYING_DETAIL_CLICK_TIMEOUT = max(
    5,
    int(os.environ.get("BIT_ZYING_DETAIL_CLICK_TIMEOUT", "12")),
)
ZYING_DETAIL_OPEN_TIMEOUT = max(
    3,
    int(os.environ.get("BIT_ZYING_DETAIL_OPEN_TIMEOUT", "5")),
)
ZYING_DETAIL_CLICK_ATTEMPTS = max(
    1,
    int(os.environ.get("BIT_ZYING_DETAIL_CLICK_ATTEMPTS", "2")),
)

TITLE_SELECTOR = ".f12.product-title, .product-title"
IMAGE_SELECTOR = "img.product-pic, img[class*='product-pic'], img[class*='product-image']"
LOGIN_SELECTOR = "input[type='password'], #password"


class ZyingAuthenticationError(RuntimeError):
    """智赢登录凭证缺失或已失效，需要用户在可视窗口中重新登录。"""


class ZyingCollectionStopped(RuntimeError):
    """智赢采集收到用户结束指令。"""


_ZYING_STOP_STATE_LOCK = threading.RLock()
_ZYING_ACTIVE_STOP_EVENT = None


def _raise_if_zying_collection_stopped(stop_event=None):
    with _ZYING_STOP_STATE_LOCK:
        event = stop_event or _ZYING_ACTIVE_STOP_EVENT
    if event is not None and event.is_set():
        raise ZyingCollectionStopped("智赢产品采集已由用户结束")


@contextmanager
def _zying_collection_stop_scope(stop_event):
    global _ZYING_ACTIVE_STOP_EVENT
    with _ZYING_STOP_STATE_LOCK:
        previous_event = _ZYING_ACTIVE_STOP_EVENT
        _ZYING_ACTIVE_STOP_EVENT = stop_event
    try:
        yield
    finally:
        with _ZYING_STOP_STATE_LOCK:
            if _ZYING_ACTIVE_STOP_EVENT is stop_event:
                _ZYING_ACTIVE_STOP_EVENT = previous_event


class _ZyingFrontendSigner:
    """Use Zying's current browser-side WASM signer from a headless Edge page."""

    SIGN_SCRIPT = r"""
const requestConfig = arguments[0];
const token = arguments[1];
const done = arguments[arguments.length - 1];
(async () => {
  try {
    const moduleScript = document.querySelector('script[type="module"][src]');
    if (!moduleScript?.src) throw new Error('未找到智赢前端模块');
    const frontend = await import(moduleScript.src);
    if (typeof frontend.i !== 'function' || typeof frontend.a !== 'function') {
      throw new Error('智赢前端签名模块已更新');
    }
    await frontend.i();
    const signed = frontend.a(requestConfig, token);
    if (
      !Array.isArray(signed) || signed.length < 2 ||
      !/^\d{10,13}$/.test(String(signed[0] || '')) ||
      !/^[a-f\d]{64}$/i.test(String(signed[1] || ''))
    ) {
      throw new Error('智赢前端签名结果格式异常');
    }
    done({ok: true, timestamp: String(signed[0]), signature: String(signed[1])});
  } catch (error) {
    done({ok: false, error: error?.message || String(error)});
  }
})();
"""

    API_CALL_SCRIPT = r"""
const prefix = arguments[0];
const action = arguments[1];
const payload = arguments[2];
const done = arguments[arguments.length - 1];
(async () => {
  try {
    const findApi = async () => {
      if (
        window.__mercadoZyingProductApi &&
        typeof window.__mercadoZyingProductApi.handleProduct === 'function'
      ) return window.__mercadoZyingProductApi;

      const urls = new Set();
      for (const entry of performance.getEntriesByType('resource')) {
        if (entry.name && entry.name.includes('.js')) urls.add(entry.name);
      }
      for (const script of document.scripts) {
        if (script.src && script.src.includes('.js')) urls.add(script.src);
      }
      for (const url of urls) {
        let parsed;
        try { parsed = new URL(url, location.href); } catch (_) { continue; }
        if (parsed.origin !== location.origin) continue;
        let source = '';
        try {
          source = await fetch(parsed.href, {credentials: 'same-origin'}).then(
            response => response.ok ? response.text() : ''
          );
        } catch (_) { continue; }
        if (!source.includes('handleProduct:')) continue;
        try {
          const module = await import(parsed.href);
          const api = Object.values(module).find(
            value => value && typeof value.handleProduct === 'function'
          );
          if (api) {
            window.__mercadoZyingProductApi = api;
            return api;
          }
        } catch (_) { continue; }
      }
      throw new Error('没有找到智赢前端产品接口模块');
    };
    const api = await findApi();
    const response = await api.handleProduct(prefix, action, payload);
    done({ok: true, response});
  } catch (error) {
    const responseData = error?.response?.data || error?.data || null;
    done({
      ok: false,
      error: responseData?.message || responseData?.msg || error?.message || String(error),
      status: error?.response?.status || error?.status || null,
      responseData,
    });
  }
})();
"""

    def __init__(self):
        self.driver = None
        self._lock = threading.RLock()
        self._credential = ""

    def start(self):
        options = webdriver.EdgeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--no-first-run")
        options.add_argument("--window-size=1280,800")
        edge_executable = _find_edge_executable()
        if edge_executable:
            options.binary_location = edge_executable
        try:
            self.driver = webdriver.Edge(options=options)
            self.driver.set_page_load_timeout(45)
            self.driver.set_script_timeout(60)
            self.driver.get(f"{ZYING_API_ORIGIN}/")
        except Exception as exc:
            self.close()
            raise RuntimeError(
                "无法启动智赢后台签名组件，请确认本机 Edge 可以正常启动"
            ) from exc
        return self

    def sign(self, token, path, payload):
        if self.driver is None:
            raise RuntimeError("智赢后台签名组件尚未启动")
        config = {
            "data": payload,
            "method": "POST",
            "url": f"{ZYING_API_ORIGIN}{path}",
        }
        with self._lock:
            try:
                result = self.driver.execute_async_script(
                    self.SIGN_SCRIPT,
                    config,
                    token,
                )
            except Exception as exc:
                raise RuntimeError(
                    "智赢后台签名组件调用失败，请稍后重试"
                ) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            message = result.get("error") if isinstance(result, dict) else "未知错误"
            raise RuntimeError(f"智赢后台签名失败：{message}")
        return result["timestamp"], result["signature"]

    def _configure_credential_locked(self, credential):
        credential = _clean_text(credential)
        if self._credential == credential:
            return
        auth_mode, auth_value = _split_zying_auth_credential(credential)
        if not auth_value:
            raise ZyingAuthenticationError("智赢登录凭证为空，请重新登录")
        try:
            self.driver.get(f"{ZYING_API_ORIGIN}/")
            if auth_mode == "cookie":
                self.driver.execute_script("localStorage.removeItem('token');")
                self.driver.add_cookie(
                    {
                        "name": "token",
                        "value": auth_value,
                        "domain": ".zying.net",
                        "path": "/",
                    }
                )
            else:
                self.driver.execute_script(
                    "localStorage.setItem('token', JSON.stringify(arguments[0]));",
                    auth_value,
                )
            self.driver.get(ZYING_PRODUCT_URL)
            WebDriverWait(self.driver, 45).until(
                lambda driver: bool(
                    driver.execute_script(
                        """
                        return performance.getEntriesByType('resource')
                          .filter(entry => entry.name.includes('.js')).length > 5
                          && String(document.body?.innerText || '').length > 0;
                        """
                    )
                )
            )
        except ZyingAuthenticationError:
            raise
        except Exception as exc:
            raise RuntimeError("智赢后台页面初始化失败，请稍后重试") from exc
        if "#/login" in str(self.driver.current_url or "").casefold():
            raise ZyingAuthenticationError(
                "智赢登录状态已失效，请重新打开登录窗口并保存登录状态"
            )
        self._credential = credential

    def call_api(self, credential, command, payload):
        if "." not in str(command or ""):
            raise ValueError(f"智赢接口命令格式错误：{command!r}")
        prefix, action = command.rsplit(".", 1)
        _raise_if_zying_collection_stopped()
        with self._lock:
            _raise_if_zying_collection_stopped()
            self._configure_credential_locked(credential)
            try:
                result = self.driver.execute_async_script(
                    self.API_CALL_SCRIPT,
                    prefix,
                    action,
                    payload,
                )
            except Exception as exc:
                raise RuntimeError(f"智赢接口 {command} 后台调用失败") from exc
        _raise_if_zying_collection_stopped()
        if not isinstance(result, dict):
            raise RuntimeError(f"智赢接口 {command} 没有返回可识别结果")
        if not result.get("ok"):
            status = result.get("status")
            message = result.get("error") or "未知错误"
            if status in (401, 403) or "登录" in message:
                raise ZyingAuthenticationError(f"智赢登录状态已失效：{message}")
            raise RuntimeError(f"智赢接口 {command} 请求失败：{message}")
        response = result.get("response")
        if not isinstance(response, dict):
            raise RuntimeError(f"智赢接口 {command} 响应格式错误")
        return response

    def close(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


_ZYING_SIGNER_STATE_LOCK = threading.RLock()
_ZYING_ACTIVE_SIGNER = None
_ZYING_ACTIVE_SIGNER_USERS = 0
_ZYING_ACTIVE_SIGNER_HOLDS = 0


@contextmanager
def _zying_frontend_signer_session():
    """Share one headless signer across a collection and its detail workers."""
    global _ZYING_ACTIVE_SIGNER, _ZYING_ACTIVE_SIGNER_USERS
    with _ZYING_SIGNER_STATE_LOCK:
        if _ZYING_ACTIVE_SIGNER is None:
            _ZYING_ACTIVE_SIGNER = _ZyingFrontendSigner().start()
        signer = _ZYING_ACTIVE_SIGNER
        _ZYING_ACTIVE_SIGNER_USERS += 1
    try:
        yield signer
    finally:
        signer_to_close = None
        with _ZYING_SIGNER_STATE_LOCK:
            _ZYING_ACTIVE_SIGNER_USERS -= 1
            if (
                _ZYING_ACTIVE_SIGNER_USERS == 0
                and _ZYING_ACTIVE_SIGNER_HOLDS == 0
            ):
                signer_to_close = _ZYING_ACTIVE_SIGNER
                _ZYING_ACTIVE_SIGNER = None
        if signer_to_close is not None:
            signer_to_close.close()


@contextmanager
def _hold_zying_frontend_signer():
    """Keep a lazily-created signer alive for one complete collection task."""
    global _ZYING_ACTIVE_SIGNER, _ZYING_ACTIVE_SIGNER_HOLDS
    with _ZYING_SIGNER_STATE_LOCK:
        _ZYING_ACTIVE_SIGNER_HOLDS += 1
    try:
        yield
    finally:
        signer_to_close = None
        with _ZYING_SIGNER_STATE_LOCK:
            _ZYING_ACTIVE_SIGNER_HOLDS -= 1
            if (
                _ZYING_ACTIVE_SIGNER_HOLDS == 0
                and _ZYING_ACTIVE_SIGNER_USERS == 0
            ):
                signer_to_close = _ZYING_ACTIVE_SIGNER
                _ZYING_ACTIVE_SIGNER = None
        if signer_to_close is not None:
            signer_to_close.close()


def _reuse_zying_frontend_signer(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _zying_collection_stop_scope(kwargs.get("stop_event")):
            with _hold_zying_frontend_signer():
                return func(*args, **kwargs)

    return wrapped


DETAIL_ROOT_SELECTOR = ".curd-detail-wrap"
DETAIL_CLICK_TARGET_SELECTOR = (
    ".f12.product-title, .product-title, a[href], button, "
    "img.product-pic, img[class*='product-pic'], img[class*='product-image']"
)

ZYING_CATEGORY_OPTIONS_SCRIPT = r"""
const cascader = document.querySelector('.ant-cascader');
if (!cascader) return [];
const fiberKey = Object.keys(cascader).find(key => key.startsWith('__reactFiber$'));
let fiber = fiberKey ? cascader[fiberKey] : null;
while (fiber) {
  const props = fiber.memoizedProps || {};
  if (Array.isArray(props.options)) {
    const copyOptions = options => options.map(option => ({
      value: option.value,
      label: String(option.label || '').trim(),
      children: copyOptions(Array.isArray(option.children) ? option.children : []),
    }));
    return copyOptions(props.options);
  }
  fiber = fiber.return;
}
return [];
"""

ZYING_SET_CATEGORY_SCRIPT = r"""
const wantedValues = arguments[0].map(value => String(value));
const cascader = document.querySelector('.ant-cascader');
if (!cascader) return false;
const fiberKey = Object.keys(cascader).find(key => key.startsWith('__reactFiber$'));
let fiber = fiberKey ? cascader[fiberKey] : null;
while (fiber) {
  const props = fiber.memoizedProps || {};
  if (Array.isArray(props.options) && typeof props.onChange === 'function') {
    const selectedOptions = [];
    let options = props.options;
    for (const wanted of wantedValues) {
      const option = options.find(item => String(item.value) === wanted);
      if (!option) return false;
      selectedOptions.push(option);
      options = Array.isArray(option.children) ? option.children : [];
    }
    props.onChange(selectedOptions.map(option => option.value), selectedOptions);
    return true;
  }
  fiber = fiber.return;
}
return false;
"""

REVIEW_STATUS_NAMES = {
    1000: "通过",
    3000: "待审核",
    4000: "价格异常",
    5000: "疑似",
    7000: "侵权",
    8000: "屏蔽",
    9000: "风险",
}

DETAIL_FORM_SCRIPT = r"""
const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
const expectedTitle = normalize(arguments[0]);
const expectedImage = String(arguments[1] || '').trim();
const expectedProductId = normalize(arguments[2]);
const root = document.querySelector('.curd-detail-wrap');
if (!root) return null;
const header = root.querySelector('.crud-detail-header .h1');
const productId = (header?.textContent || '').trim();
const detailTitles = Array.from(
  root.querySelectorAll("textarea[placeholder='请输入内容']")
).map(input => normalize(input.value)).filter(Boolean);
const detailImages = Array.from(root.querySelectorAll('img.ant-image-img'))
  .map(image => String(image.currentSrc || image.src || '').trim())
  .filter(Boolean);
if (!productId) return null;
// 智赢的多变体产品会在价格、重量等字段加载完成前卸载标题编辑器。
// 列表缩略图可能还是旧图，而详情已经换成新图；标题或主图任一匹配即可确认身份。
const productIdMatches = Boolean(expectedProductId) && productId === expectedProductId;
const titleMatches = Boolean(expectedTitle) && detailTitles.includes(expectedTitle);
const imageMatches = Boolean(expectedImage) && detailImages.includes(expectedImage);
if (!productIdMatches && !titleMatches && !imageMatches) return null;

const value = id => (root.querySelector(`#${id}`)?.value || '').trim();
const checkedStatus = root.querySelector("input[name='stat']:checked");
const status = (
  checkedStatus?.closest('label')?.querySelector('.ant-radio-label')?.textContent || ''
).trim();
const formFields = {};
for (const control of root.querySelectorAll('input, textarea, select')) {
  if ((control.type === 'radio' || control.type === 'checkbox') && !control.checked) continue;
  const label = control.id
    ? root.querySelector(`label[for="${CSS.escape(control.id)}"]`)
    : null;
  const key = normalize(
    control.id || control.name || label?.textContent ||
    control.getAttribute('placeholder') || control.getAttribute('aria-label')
  );
  if (!key) continue;
  const fieldValue = control.type === 'checkbox'
    ? Boolean(control.checked)
    : normalize(control.value);
  if (fieldValue !== '') formFields[key] = fieldValue;
}
const details = {
  product_id: productId,
  sale_price: value('cost'),
  net_income: value('netproceed'),
  package_gross_weight: value('weight'),
  size_length: value('sizeLength'),
  size_width: value('sizeWidth'),
  size_height: value('sizeHeight'),
  review_status: status,
  title_values: detailTitles,
  images: detailImages,
  form_fields: formFields,
  detail_text: String(root.innerText || '').trim(),
};
return {
  details,
  ready: [
    details.product_id, details.sale_price, details.net_income,
    details.package_gross_weight, details.size_length, details.size_width,
    details.size_height, details.review_status,
  ].every(Boolean),
};
"""

FIELD_DEFINITIONS = {
    "sale_price": {
        "labels": ("售价", "销售价", "销售价格", "Price", "Precio"),
        "selectors": (
            ".sale-price",
            ".selling-price",
            ".product-price",
            ".product-info .color-0000b3",
            "[class*='sale-price']",
            "[class*='selling-price']",
            "[class~='price']",
            "[class*='product-price']",
        ),
    },
    "net_income": {
        "labels": (
            "净收益",
            "净利润",
            "预计净收益",
            "Net income",
            "Net profit",
            "Ganancia neta",
        ),
        "selectors": (
            ".net-income",
            ".net-profit",
            "[class*='net-income']",
            "[class*='net-profit']",
            "[class*='profit']",
        ),
    },
    "package_gross_weight": {
        "labels": (
            "包装毛重",
            "包裹毛重",
            "包装重量",
            "毛重",
            "Package gross weight",
            "Package weight",
            "Peso bruto",
            "Peso del paquete",
        ),
        "selectors": (
            ".package-gross-weight",
            ".package-weight",
            "[class*='package-weight']",
            "[class*='gross-weight']",
            "[class~='weight']",
        ),
    },
    "package_dimensions": {
        "labels": (
            "包装尺寸",
            "包裹尺寸",
            "长宽高",
            "Package dimensions",
            "Dimensiones del paquete",
        ),
        "selectors": (
            ".package-dimensions",
            ".package-size",
            "[class*='package-dimension']",
            "[class*='package-size']",
            "[class*='dimension']",
        ),
    },
    "review_status": {
        "labels": (
            "审核状态",
            "审核",
            "Review status",
            "Estado de revisión",
        ),
        "selectors": (
            ".review-status",
            ".audit-status",
            "[class*='review-status']",
            "[class*='audit-status']",
            "[class~='status']",
        ),
    },
}

PRODUCT_ID_LABELS = (
    "产品编号",
    "商品编号",
    "产品ID",
    "商品ID",
    "Product ID",
    "Item ID",
)


def _clean_text(value):
    return re.sub(r"[\t\r ]+", " ", str(value or "")).strip()


def _read_zying_auth_record(auth_file=None):
    """读取本机智赢凭证元数据；不会把 token 暴露给状态接口。"""
    configured_token = _clean_text(os.environ.get("BIT_ZYING_TOKEN"))
    if configured_token:
        return {
            "token": configured_token,
            "auth_mode": "header",
            "source": "environment",
            "saved_at": "",
            "browser_type": "",
            "window_name": "",
        }

    path = Path(auth_file or ZYING_AUTH_FILE)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not _clean_text(payload.get("token")):
        return {}
    payload["source"] = "local_file"
    return payload


def load_zying_auth_token(auth_file=None, required=True):
    record = _read_zying_auth_record(auth_file)
    token = _clean_text(record.get("token"))
    if not token and required:
        raise ZyingAuthenticationError(
            "未能读取智赢登录凭证，请在当前所选浏览器登录智赢后直接重新启动采集"
        )
    return _encode_zying_auth_credential(token, record.get("auth_mode"))


def _encode_zying_auth_credential(value, auth_mode="header"):
    value = _clean_text(value)
    if value and _clean_text(auth_mode).casefold() == "cookie":
        return f"{ZYING_COOKIE_CREDENTIAL_PREFIX}{value}"
    return value


def _split_zying_auth_credential(credential):
    credential = _clean_text(credential)
    if credential.startswith(ZYING_COOKIE_CREDENTIAL_PREFIX):
        return "cookie", credential[len(ZYING_COOKIE_CREDENTIAL_PREFIX):]
    return "header", credential


def save_zying_auth_token(
    token,
    *,
    browser_type="",
    window_name="",
    window_id="",
    auth_file=None,
):
    auth_mode, token = _split_zying_auth_credential(token)
    if not token:
        raise ZyingAuthenticationError("智赢登录凭证为空，请重新登录")
    path = Path(auth_file or ZYING_AUTH_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": token,
        "auth_mode": auth_mode,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "browser_type": normalize_zying_browser_type(browser_type),
        "window_name": _clean_text(window_name)[:256],
        "window_id": _clean_text(window_id)[:128],
    }
    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        os.chmod(temporary_path, 0o600)
    except OSError:
        pass
    os.replace(temporary_path, path)
    return get_zying_auth_status(auth_file=path)


def get_zying_auth_status(auth_file=None):
    record = _read_zying_auth_record(auth_file)
    return {
        "configured": bool(_clean_text(record.get("token"))),
        "source": _clean_text(record.get("source")),
        "saved_at": _clean_text(record.get("saved_at")),
        "browser_type": _clean_text(record.get("browser_type")),
        "window_name": _clean_text(record.get("window_name")),
        "auth_mode": _clean_text(record.get("auth_mode") or "header"),
    }


def _iter_zying_category_paths(options, parents=()):
    for option in options or []:
        current = parents + (
            {
                "value": option.get("value"),
                "label": _clean_text(option.get("label")),
            },
        )
        yield current
        yield from _iter_zying_category_paths(option.get("children"), current)


def _resolve_zying_category(options, requested_category):
    """按智赢分类 ID、唯一名称或完整路径解析 Cascader 选项。"""
    requested = _clean_text(requested_category)
    if not requested:
        return None
    paths = list(_iter_zying_category_paths(options))
    requested_path = tuple(
        _clean_text(value).casefold()
        for value in re.split(r"\s*(?:/|>|＞)\s*", requested)
        if _clean_text(value)
    )
    matches = []
    for path in paths:
        labels = tuple(item["label"].casefold() for item in path)
        values = tuple(str(item["value"]) for item in path)
        requested_folded = requested.casefold()
        joined_labels = "/".join(labels)
        if (
            requested == values[-1]
            or requested_path == labels
            or requested_folded == joined_labels
        ):
            matches.append(path)
        elif requested_folded == labels[-1]:
            matches.append(path)

    if not matches:
        raise RuntimeError(
            f"智赢产品分类中找不到 {requested!r}；"
            "请填写分类 ID、唯一分类名或完整路径（例如：圆佑同步/家电类）"
        )
    unique_paths = {
        tuple(str(item["value"]) for item in path): path for path in matches
    }
    if len(unique_paths) > 1:
        candidates = [
            "/".join(item["label"] for item in path)
            for path in list(unique_paths.values())[:8]
        ]
        raise RuntimeError(
            f"智赢产品分类名称 {requested!r} 不唯一，请改用分类 ID 或完整路径："
            + "；".join(candidates)
        )
    path = next(iter(unique_paths.values()))
    return {
        "category_id": str(path[-1]["value"]),
        "category_name": path[-1]["label"],
        "category_path": "/".join(item["label"] for item in path),
        "path_values": [item["value"] for item in path],
        "path_labels": [item["label"] for item in path],
    }


def _find_search_button(driver):
    for button in driver.find_elements(By.CSS_SELECTOR, "button"):
        try:
            text = _clean_text(button.get_attribute("textContent") or button.text)
            if text == "搜索" and button.is_displayed() and button.is_enabled():
                return button
        except Exception:
            continue
    return None


def _apply_zying_category_filter(driver, wait, requested_category):
    options = driver.execute_script(ZYING_CATEGORY_OPTIONS_SCRIPT) or []
    if not options:
        raise RuntimeError("未能读取智赢产品分类选项，页面可能尚未加载完成或已改版")
    selection = _resolve_zying_category(options, requested_category)
    old_signature = _page_signature(driver)
    changed = driver.execute_script(
        ZYING_SET_CATEGORY_SCRIPT,
        selection["path_values"],
    )
    if not changed:
        raise RuntimeError(
            f"智赢产品分类 {selection['category_path']!r} 设置失败，页面分类控件可能已改版"
        )

    try:
        wait.until(
            lambda current_driver: selection["category_name"]
            in _clean_text(
                current_driver.execute_script(
                    "const item=document.querySelector('.ant-cascader "
                    ".ant-select-selection-item');"
                    "return item ? item.textContent : '';"
                )
            )
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"智赢产品分类 {selection['category_path']!r} 已解析，但页面未显示选中状态"
        ) from exc

    search_button = _find_search_button(driver)
    if search_button is None:
        raise RuntimeError("已设置智赢产品分类，但没有找到“搜索”按钮")
    try:
        search_button.click()
    except Exception:
        driver.execute_script("arguments[0].click();", search_button)

    try:
        wait.until(
            lambda current_driver: bool(
                current_driver.find_elements(By.CSS_SELECTOR, TITLE_SELECTOR)
            )
            and _page_signature(current_driver) != old_signature
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"智赢产品分类 {selection['category_path']!r} 搜索后列表加载超时"
        ) from exc
    print(
        f"智赢产品分类已指定：{selection['category_path']} "
        f"(ID {selection['category_id']})",
        flush=True,
    )
    return selection


def _normalize_browser_auth_token(stored_token):
    if not stored_token:
        return ""
    try:
        token = json.loads(stored_token)
    except (TypeError, ValueError):
        token = stored_token
    if isinstance(token, dict):
        token = (
            token.get("token")
            or token.get("access_token")
            or token.get("accessToken")
            or ""
        )
    return str(token or "").strip()


def _browser_auth_token(driver):
    """Read both legacy localStorage and current Zying cookie credentials."""
    stored_token = driver.execute_script("return localStorage.getItem('token');")
    token = _normalize_browser_auth_token(stored_token)
    auth_mode = "header"
    if not token:
        try:
            cookie = driver.get_cookie("token") or {}
            token = _normalize_browser_auth_token(cookie.get("value"))
            if token:
                auth_mode = "cookie"
        except Exception:
            token = ""
    if not token:
        try:
            cookie_token = driver.execute_script(
                """
                const item = document.cookie.split(';').map(value => value.trim())
                  .find(value => value.startsWith('token='));
                return item ? item.slice('token='.length) : '';
                """
            )
            token = _normalize_browser_auth_token(cookie_token)
            if token:
                auth_mode = "cookie"
        except Exception:
            token = ""
    if not token:
        raise RuntimeError(
            "未能从智赢页面的本地存储或 Cookie 读取登录凭证，"
            "请重新登录后再运行采集脚本。"
        )
    return _encode_zying_auth_credential(token, auth_mode)


def _browser_has_auth_token(driver):
    try:
        return bool(_browser_auth_token(driver))
    except Exception:
        return False


def _signed_api_headers(token, path, body, timestamp=None):
    del timestamp  # Timestamp is generated inside Zying's current WASM signer.
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("智赢接口请求内容无法签名") from exc
    with _zying_frontend_signer_session() as signer:
        timestamp, signature = signer.sign(token, path, payload)
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "appclient": "2",
        "signature": signature,
        "timestamp": timestamp,
        "token": token,
        "version": "v1.1",
    }


def _zying_api_post(session, token, command, payload):
    del session  # Requests are executed by Zying's own frontend inside headless Edge.
    last_error = None
    with _zying_frontend_signer_session() as frontend:
        for attempt in range(3):
            _raise_if_zying_collection_stopped()
            try:
                result = frontend.call_api(token, command, payload)
                _raise_if_zying_collection_stopped()
                if result.get("code") == 200:
                    return result.get("data") or {}
                message = result.get("message") or f"业务状态码 {result.get('code')}"
                if result.get("code") == 401:
                    raise ZyingAuthenticationError(f"智赢登录状态已失效：{message}")
                last_error = RuntimeError(f"智赢接口 {command} 请求失败：{message}")
            except (ZyingAuthenticationError, ZyingCollectionStopped):
                raise
            except Exception as exc:
                last_error = (
                    exc
                    if isinstance(exc, RuntimeError)
                    else RuntimeError(f"智赢接口 {command} 请求失败：{exc}")
                )
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise last_error


def validate_zying_auth_token(token):
    """用一个最小列表请求确认 token 能用于后台 API 采集。"""
    token = _clean_text(token)
    if not token:
        raise ZyingAuthenticationError("智赢登录凭证为空，请重新登录")
    with requests.Session() as session:
        session.trust_env = False
        data = _zying_api_post(
            session,
            token,
            "sale.stat",
            {
                "page": 1,
                "pagesize": 1,
                "word": "",
                "from": ZYING_MELI_PLATFORM_ID,
            },
        )
    listing = data.get("list") if isinstance(data, dict) else None
    if not isinstance(listing, dict):
        raise RuntimeError("智赢登录检测成功，但产品列表接口返回格式异常")
    return True


def _plain_search_title(value):
    return _clean_text(html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))))


def _select_search_result(record, rows):
    wanted_title = _clean_text(record.get("title")).casefold()
    wanted_image = _clean_text(record.get("main_image_url"))
    exact_matches = [
        row
        for row in rows or []
        if _plain_search_title(row.get("title")).casefold() == wanted_title
    ]
    for row in exact_matches:
        if wanted_image and _clean_text(row.get("thumb")) == wanted_image:
            return row
    if exact_matches:
        return exact_matches[0]
    if len(rows or []) == 1:
        return rows[0]
    return None


def _format_number(value):
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_money(value, currency):
    number = _format_number(value)
    if not number:
        return ""
    currency = _clean_text(currency)
    return f"{currency} {number}".strip()


def _json_value(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _localized_detail_text(value):
    parsed = _json_value(value)
    if isinstance(parsed, dict):
        for language in ("en", "es", "pt", "zh", "cn"):
            text = _clean_text(parsed.get(language))
            if text:
                return text
        return next(
            (_clean_text(item) for item in parsed.values() if _clean_text(item)),
            "",
        )
    if isinstance(parsed, list):
        return " ".join(_clean_text(item) for item in parsed if _clean_text(item))
    return _clean_text(value)


def _detail_site_attributes(detail):
    attributes = _json_value(detail.get("sale_attrs"), {})
    if not isinstance(attributes, dict):
        return {}
    site_attributes = attributes.get(str(detail.get("sale_siteid") or "")) or {}
    if not isinstance(site_attributes, dict) or not site_attributes:
        site_attributes = next(
            (value for value in attributes.values() if isinstance(value, dict)),
            {},
        )
    return site_attributes


def _normalize_listing_attribute(attribute, fallback_id=""):
    if isinstance(attribute, dict):
        attribute_id = _clean_text(
            attribute.get("id")
            or attribute.get("attribute_id")
            or attribute.get("attr_id")
            or attribute.get("code")
            or fallback_id
        )
        name = _clean_text(attribute.get("name") or attribute.get("label"))
        value_name = attribute.get("value_name")
        if value_name in (None, ""):
            value_name = attribute.get("value")
        if value_name in (None, ""):
            value_name = attribute.get("text")
        normalized = {
            key: attribute.get(key)
            for key in ("value_id", "value_struct", "values")
            if attribute.get(key) not in (None, "")
        }
        if attribute_id:
            normalized["id"] = attribute_id
        if name:
            normalized["name"] = name
        if value_name not in (None, ""):
            normalized["value_name"] = _clean_text(value_name)
        return normalized if normalized.get("id") else None
    if fallback_id and attribute not in (None, ""):
        return {"id": _clean_text(fallback_id), "value_name": _clean_text(attribute)}
    return None


def _detail_listing_attributes(detail):
    """尽量从 sale.detail 的不同版本中还原 Mercado attributes 数组。"""
    site_attributes = _detail_site_attributes(detail)
    candidates = []
    for container in (detail, site_attributes):
        if not isinstance(container, dict):
            continue
        for key in (
            "attributes", "attrs", "attribute", "item_attributes",
            "sale_attributes", "specifications", "specs",
        ):
            value = _json_value(container.get(key), container.get(key))
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.extend(
                    {"id": item_id, "value": item_value}
                    if not isinstance(item_value, dict)
                    else {"id": item_id, **item_value}
                    for item_id, item_value in value.items()
                )

    common_fields = {
        "sale_brand": "BRAND",
        "brand": "BRAND",
        "sale_model": "MODEL",
        "model": "MODEL",
        "sale_gtin": "GTIN",
        "gtin": "GTIN",
        "ean": "GTIN",
        "upc": "GTIN",
        "sale_sku": "SELLER_SKU",
    }
    for field_name, attribute_id in common_fields.items():
        if detail.get(field_name) not in (None, ""):
            candidates.append({"id": attribute_id, "value": detail[field_name]})

    normalized = []
    positions = {}
    for candidate in candidates:
        attribute = _normalize_listing_attribute(candidate)
        if not attribute:
            continue
        attribute_id = attribute["id"].upper()
        attribute["id"] = attribute_id
        if attribute_id in positions:
            existing = normalized[positions[attribute_id]]
            if not existing.get("value_name") and attribute.get("value_name"):
                normalized[positions[attribute_id]] = attribute
            continue
        positions[attribute_id] = len(normalized)
        normalized.append(attribute)
    return normalized


def _detail_list(detail, *keys):
    for key in keys:
        value = _json_value(detail.get(key), detail.get(key))
        if isinstance(value, list):
            return value
    return []


def _finalize_zying_listing_snapshot(record):
    """生成与普通商品采集一致、可供产品上架直接读取的源快照。"""
    detail = dict(record.get("detail_data") or {})
    product_id = _clean_text(record.get("product_id"))
    category_id = _clean_text(record.get("product_category_id"))
    title = (
        _localized_detail_text(detail.get("sale_title"))
        or _clean_text(record.get("title"))
    )
    currency = _clean_text(detail.get("sale_cur"))
    price = detail.get("sale_cost")
    if price in (None, ""):
        price = _clean_text(record.get("sale_price")).split(" ")[-1]
    images = [
        _clean_text(value.get("source") or value.get("url") or value.get("secure_url"))
        if isinstance(value, dict)
        else _clean_text(value)
        for value in _detail_list(detail, "sale_pic", "pictures", "images")
    ]
    images = list(dict.fromkeys(value for value in images if value))
    if not images and _clean_text(record.get("main_image_url")):
        images = [_clean_text(record["main_image_url"])]
    description = ""
    for key in (
        "sale_description", "sale_desc", "description", "desc",
        "sale_content", "content",
    ):
        description = _localized_detail_text(detail.get(key))
        if description:
            break
    attributes = _detail_listing_attributes(detail)
    variations = _detail_list(
        detail, "sale_variations", "sale_variation", "variations", "variation"
    )
    sale_terms = _detail_list(
        detail, "sale_terms", "sale_saleterms", "saleterms", "terms"
    )
    source_id = product_id
    if product_id and not re.match(r"^(?:ML[A-Z]|CBT)-?\d+$", product_id, re.I):
        source_id = f"CBT{re.sub(r'\D+', '', product_id) or product_id}"
    source = {
        "id": source_id,
        "site_id": "CBT",
        "title": title,
        "category_id": category_id,
        "price": price,
        "currency_id": currency or "USD",
        "condition": _clean_text(
            detail.get("sale_condition") or detail.get("condition") or "new"
        ),
        "pictures": [{"source": value} for value in images],
        "attributes": attributes,
        "variations": variations,
        "sale_terms": sale_terms,
    }
    quantity = detail.get("sale_stock")
    if quantity in (None, ""):
        quantity = detail.get("available_quantity")
    if quantity not in (None, ""):
        source["available_quantity"] = quantity
    package_size = _detail_list(detail, "sale_size", "size", "dimensions")[:3]
    package_size.extend([None] * (3 - len(package_size)))
    snapshot = {
        "item_id": product_id,
        "source_url": ZYING_PRODUCT_URL,
        "final_url": ZYING_PRODUCT_URL,
        "main_image_url": images[0] if images else "",
        "title": title,
        "price": price,
        "currency_id": source["currency_id"],
        "category_id": category_id,
        "source": source,
        "description": {"plain_text": description} if description else {},
        "page_snapshot": {
            "zying_detail": detail,
            "detail_form_fields": record.get("detail_form_fields") or {},
            "detail_text": record.get("detail_text") or "",
        },
        "plugin_snapshot": {
            "source_type": "zying",
            "zying_category_id": record.get("zying_category_id") or "",
            "zying_category": record.get("zying_category") or "",
        },
        "weight_g": detail.get("sale_weight"),
        "package_length_cm": package_size[0],
        "package_width_cm": package_size[1],
        "package_height_cm": package_size[2],
        "scrape_status": "ok",
        "scraped_at": record.get("collected_at"),
    }
    record["listing_attributes"] = attributes
    record["listing_variations"] = variations
    record["listing_sale_terms"] = sale_terms
    record["description_text"] = description
    record["all_image_urls"] = images
    record["listing_snapshot"] = snapshot
    return record


def _detail_category_reference(detail):
    site_attributes = _detail_site_attributes(detail)
    site = _clean_text(site_attributes.get("site") or detail.get("sale_area"))
    category_id = _format_number(site_attributes.get("kindid"))
    return site, category_id


def _merge_detail_record(record, search_row, detail):
    record["detail_data"] = dict(detail or {})
    product_id = detail.get("sale_id") or search_row.get("id")
    currency = detail.get("sale_cur") or search_row.get("cur")
    sale_price = detail.get("sale_cost")
    if sale_price is None:
        sale_price = search_row.get("cost")

    record["product_id"] = _clean_text(record.get("product_id")) or _format_number(
        product_id
    )
    record["sale_price"] = _clean_text(record.get("sale_price")) or _format_money(
        sale_price,
        currency,
    )
    record["net_income"] = _clean_text(record.get("net_income")) or _format_money(
        detail.get("sale_netproceed"),
        currency,
    )

    weight = _format_number(detail.get("sale_weight"))
    record["package_gross_weight"] = _clean_text(
        record.get("package_gross_weight")
    ) or (f"{weight} 克" if weight else "")

    dimensions = detail.get("sale_size") or []
    dimension_values = [_format_number(value) for value in dimensions[:3]]
    if len(dimension_values) == 3 and all(dimension_values):
        api_dimensions = f"{' X '.join(dimension_values)} 厘米"
    else:
        api_dimensions = ""
    record["package_dimensions"] = _clean_text(
        record.get("package_dimensions")
    ) or api_dimensions

    try:
        review_code = (int(detail.get("sale_stat") or 0) // 1000) * 1000
    except (TypeError, ValueError):
        review_code = 0
    record["review_status"] = _clean_text(record.get("review_status")) or (
        REVIEW_STATUS_NAMES.get(
            review_code,
            _format_number(detail.get("sale_stat")),
        )
    )

    images = detail.get("sale_pic") or []
    if images:
        record["main_image_url"] = _clean_text(images[0])
    category_site, category_id = _detail_category_reference(detail)
    record["_category_site"] = category_site
    record["_category_id"] = category_id
    return record


def _load_product_category(token, site, category_id):
    with requests.Session() as session:
        session.trust_env = False
        category_data = _zying_api_post(
            session,
            token,
            "meli_category.detail",
            {"site": site, "id": category_id},
        )
    category_rows = category_data.get("root") or []
    if not category_rows:
        raise RuntimeError(f"分类接口未返回数据：{site}{category_id}")
    return category_rows[0]


def _merge_category_record(record, category):
    category_id = _clean_text(category.get("cate_cateid"))
    if not category_id:
        site = _clean_text(category.get("cate_site"))
        raw_id = _format_number(category.get("cate_id"))
        category_id = f"{site}{raw_id}" if site and raw_id else raw_id

    english_path = _clean_text(
        category.get("cate_fullname") or category.get("cate_name")
    )
    chinese_path = _clean_text(category.get("cate_fullzh") or category.get("cate_zh"))
    paths = []
    for value in (english_path, chinese_path):
        if value and value not in paths:
            paths.append(value)

    record["product_category_id"] = category_id
    record["product_category"] = " | ".join(paths)
    record.pop("_category_site", None)
    record.pop("_category_id", None)

    category_lines = []
    if category_id:
        category_lines.append(f"分类编号: {category_id}")
    if record["product_category"]:
        category_lines.append(f"产品分类: {record['product_category']}")
    if category_lines:
        raw_text = str(record.get("raw_text") or "").rstrip()
        record["raw_text"] = "\n".join(filter(None, (raw_text, *category_lines)))
    return record


def _enrich_product_categories(token, records):
    category_records = {}
    for record in records:
        site = _clean_text(record.get("_category_site"))
        category_id = _clean_text(record.get("_category_id"))
        if not site or not category_id:
            raise RuntimeError(
                f"产品详情缺少美客多分类编号：{record.get('product_id') or record.get('title')!r}"
            )
        category_records.setdefault((site, category_id), []).append(record)

    failures = []
    worker_count = min(ZYING_DETAIL_WORKERS, len(category_records))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_categories = {
            executor.submit(_load_product_category, token, site, category_id): (
                site,
                category_id,
            )
            for site, category_id in category_records
        }
        for future in as_completed(future_categories):
            key = future_categories[future]
            try:
                category = future.result()
                for record in category_records[key]:
                    _merge_category_record(record, category)
            except ZyingCollectionStopped:
                for pending in future_categories:
                    pending.cancel()
                raise
            except Exception as exc:
                failures.append(f"{key[0]}{key[1]}: {exc}")
    if failures:
        preview = "；".join(failures[:3])
        suffix = f"；另有 {len(failures) - 3} 个分类失败" if len(failures) > 3 else ""
        raise RuntimeError(
            f"智赢产品分类补全失败 {len(failures)}/{len(category_records)} 个，"
            f"为避免不完整数据，本次未入库。{preview}{suffix}"
        )
    return records


def _merge_ui_detail_record(record, details):
    existing_price = _clean_text(record.get("sale_price"))
    currency = existing_price.split(" ", 1)[0] if " " in existing_price else ""
    record["product_id"] = _clean_text(details.get("product_id"))
    record["sale_price"] = _format_money(details.get("sale_price"), currency)
    record["net_income"] = _format_money(details.get("net_income"), currency)
    record["package_gross_weight"] = (
        f"{_clean_text(details.get('package_gross_weight'))} 克"
    )
    record["package_dimensions"] = (
        f"{_clean_text(details.get('size_length'))} X "
        f"{_clean_text(details.get('size_width'))} X "
        f"{_clean_text(details.get('size_height'))} 厘米"
    )
    record["review_status"] = _clean_text(details.get("review_status"))
    record["detail_form_fields"] = dict(details.get("form_fields") or {})
    record["detail_text"] = str(details.get("detail_text") or "").strip()
    detail_images = [
        _clean_text(value) for value in (details.get("images") or []) if _clean_text(value)
    ]
    if detail_images:
        record["all_image_urls"] = list(dict.fromkeys(detail_images))

    detail_lines = (
        f"详情产品编号: {record['product_id']}",
        f"详情售价: {record['sale_price']}",
        f"详情净收益: {record['net_income']}",
        f"详情包装毛重: {record['package_gross_weight']}",
        f"详情包装尺寸: {record['package_dimensions']}",
        f"详情审核状态: {record['review_status']}",
    )
    raw_text = str(record.get("raw_text") or "").rstrip()
    record["raw_text"] = "\n".join(filter(None, (raw_text, *detail_lines)))
    return record


def _click_product_card(driver, card, attempt=0):
    """按重试次数切换点击策略，避免一直重复点击无响应的卡片外层。"""
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
    if attempt <= 0:
        try:
            card.click()
            return "卡片"
        except Exception:
            driver.execute_script("arguments[0].click();", card)
            return "卡片-JS"

    target = driver.execute_script(
        "return arguments[0].querySelector(arguments[1]) || arguments[0];",
        card,
        DETAIL_CLICK_TARGET_SELECTOR,
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
    if attempt == 1:
        try:
            target.click()
            return "标题/链接"
        except Exception:
            driver.execute_script("arguments[0].click();", target)
            return "标题/链接-JS"

    driver.execute_script(
        """
        const target = arguments[0];
        const options = {bubbles: true, cancelable: true, view: window};
        for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
            const EventClass = type.startsWith('pointer') && window.PointerEvent
                ? window.PointerEvent
                : window.MouseEvent;
            target.dispatchEvent(new EventClass(type, options));
        }
        """,
        target,
    )
    return "指针事件"


class _DetailFieldsTimeout(TimeoutException):
    """目标详情已打开，但表单字段没有在限定时间内加载完整。"""

    def __init__(self, details):
        super().__init__("目标详情字段加载超时")
        self.details = details


def _wait_for_clicked_detail(
    driver,
    expected_title,
    expected_image,
    expected_product_id="",
):
    """先确认打开的是目标商品，再等待表单字段；避免把旧详情误判为已打开。"""

    def read_state(current_driver):
        return current_driver.execute_script(
            DETAIL_FORM_SCRIPT,
            expected_title,
            expected_image,
            expected_product_id,
        )

    state = WebDriverWait(
        driver,
        min(ZYING_DETAIL_OPEN_TIMEOUT, ZYING_DETAIL_CLICK_TIMEOUT),
        poll_frequency=0.25,
    ).until(read_state)
    if state.get("ready"):
        return state["details"]

    def read_ready_state(current_driver):
        current_state = read_state(current_driver) or {}
        return current_state if current_state.get("ready") else False

    try:
        state = WebDriverWait(
            driver,
            ZYING_DETAIL_CLICK_TIMEOUT,
            poll_frequency=0.25,
        ).until(read_ready_state)
    except TimeoutException as exc:
        raise _DetailFieldsTimeout(state["details"]) from exc
    return state["details"]


def _find_record_title_element(
    driver,
    title_elements,
    expected_title,
    expected_image,
    preferred_index,
):
    candidate_indexes = list(range(len(title_elements)))
    if 0 <= preferred_index < len(title_elements):
        candidate_indexes.remove(preferred_index)
        candidate_indexes.insert(0, preferred_index)

    for candidate_index in candidate_indexes:
        title_element = title_elements[candidate_index]
        title = _clean_text(
            title_element.get_attribute("textContent") or title_element.text
        )
        if title != expected_title:
            continue
        card = _find_product_card(driver, title_element)
        if expected_image and _extract_image_url(card) != expected_image:
            continue
        return title_element
    return None


def _collect_clicked_product_details(
    driver,
    records,
    page_number=None,
    page_count=None,
):
    page_label = (
        f"第 {page_number}/{page_count} 页"
        if page_number is not None and page_count is not None
        else "当前页"
    )
    completed_records = []
    skipped_messages = []
    fallback_messages = []
    for index, record in enumerate(records):
        expected_title = _clean_text(record.get("title"))
        expected_image = _clean_text(record.get("main_image_url"))
        expected_product_id = _clean_text(record.get("product_id"))
        if not expected_title or not expected_image:
            raise RuntimeError(f"点击详情前缺少标题或主图：{record!r}")

        # 每条产品都实际点击，不能复用上一条详情；优先用预解析的产品编号确认身份。
        details = None
        last_error = None
        for attempt in range(ZYING_DETAIL_CLICK_ATTEMPTS):
            if details:
                break
            print(
                f"智赢{page_label}，正在点击详情 {index + 1}/{len(records)}，"
                f"第 {attempt + 1}/{ZYING_DETAIL_CLICK_ATTEMPTS} 次，"
                f"标题 {expected_title!r}",
                flush=True,
            )
            title_elements = driver.find_elements(By.CSS_SELECTOR, TITLE_SELECTOR)
            title_element = _find_record_title_element(
                driver,
                title_elements,
                expected_title,
                expected_image,
                index,
            )
            if title_element is None:
                raise RuntimeError(
                    f"产品列表在采集过程中发生变化：找不到标题 {expected_title!r}、"
                    f"主图 {expected_image!r} 对应的卡片，当前有 "
                    f"{len(title_elements)} 条。"
                )
            card = _find_product_card(driver, title_element)
            click_method = _click_product_card(driver, card, attempt=attempt)
            try:
                details = _wait_for_clicked_detail(
                    driver,
                    expected_title,
                    expected_image,
                    expected_product_id,
                )
            except _DetailFieldsTimeout as exc:
                last_error = exc
                loaded_product_id = _clean_text(exc.details.get("product_id"))
                if loaded_product_id:
                    record["product_id"] = loaded_product_id
                    expected_product_id = loaded_product_id
                print(
                    f"智赢{page_label}详情 {index + 1}/{len(records)} 已通过"
                    f"{click_method}打开并确认产品编号 {expected_product_id or '空'}，"
                    f"但字段等待 {ZYING_DETAIL_CLICK_TIMEOUT} 秒未完成；"
                    "停止页面重试，稍后使用接口详情补全",
                    flush=True,
                )
                break
            except TimeoutException as exc:
                last_error = exc
                print(
                    f"智赢{page_label}详情 {index + 1}/{len(records)} 使用"
                    f"{click_method}后 {min(ZYING_DETAIL_OPEN_TIMEOUT, ZYING_DETAIL_CLICK_TIMEOUT)} "
                    "秒内未打开目标详情，准备切换点击方式重试",
                    flush=True,
                )

        if not details:
            current_ids = driver.find_elements(
                By.CSS_SELECTOR,
                f"{DETAIL_ROOT_SELECTOR} .crud-detail-header .h1",
            )
            current_id = (
                _clean_text(current_ids[0].get_attribute("textContent"))
                if current_ids
                else ""
            )
            current_titles = driver.find_elements(
                By.CSS_SELECTOR,
                f"{DETAIL_ROOT_SELECTOR} textarea[placeholder='请输入内容']",
            )
            current_title = (
                _clean_text(current_titles[0].get_attribute("value"))
                if current_titles
                else ""
            )
            message = (
                f"点击产品后详情加载超时：期望标题 {expected_title!r}，"
                f"当前详情编号 {current_id or '空'}，"
                f"当前详情标题 {current_title or '空'!r}。"
            )
            if _clean_text(record.get("product_id")):
                fallback_messages.append(message)
                completed_records.append(record)
                print(
                    f"智赢{page_label}详情 {index + 1}/{len(records)} 页面采集未完成，"
                    f"已保留产品编号 {record['product_id']} 并继续，稍后使用接口详情补全："
                    f"{message}",
                    flush=True,
                )
                continue
            skipped_messages.append(message)
            print(
                f"智赢{page_label}详情 {index + 1}/{len(records)} 采集失败，"
                f"已跳过该商品并继续：{message}",
                flush=True,
            )
            continue

        _merge_ui_detail_record(record, details)
        completed_records.append(record)
        print(
            f"智赢{page_label}详情已读取 {index + 1}/{len(records)}："
            f"产品编号 {record['product_id']}",
            flush=True,
        )
    if records and not completed_records:
        raise RuntimeError(
            f"智赢{page_label}全部 {len(records)} 条商品详情采集失败，"
            f"本页未入库。首条错误：{skipped_messages[0]}"
        ) from last_error
    if skipped_messages:
        print(
            f"智赢{page_label}共跳过 {len(skipped_messages)} 条详情失败商品，"
            f"其余 {len(completed_records)} 条继续入库",
            flush=True,
        )
    if fallback_messages:
        print(
            f"智赢{page_label}有 {len(fallback_messages)} 条页面详情未完整加载，"
            "已改用接口详情补全，未跳过商品",
            flush=True,
        )
    return completed_records


def _find_product_search_row(session, token, record):
    product_id = _clean_text(record.get("product_id"))
    if product_id:
        return {"id": product_id}

    search_data = _zying_api_post(
        session,
        token,
        "sale.stat",
        {"page": 1, "pagesize": 60, "word": record.get("title", "")},
    )
    rows = (search_data.get("list") or {}).get("data") or []
    search_row = _select_search_result(record, rows)
    if not search_row or not search_row.get("id"):
        raise RuntimeError(f"未找到产品编号：{record.get('title')!r}")
    return search_row


def _resolve_product_id(record, token):
    """在点击前通过并发接口查询补齐编号，用编号识别翻译后标题的详情。"""
    if _clean_text(record.get("product_id")):
        return record
    with requests.Session() as session:
        session.trust_env = False
        search_row = _find_product_search_row(session, token, record)
    record["product_id"] = _format_number(search_row["id"])
    return record


def _resolve_product_ids(token, records):
    unresolved_records = [
        record for record in records if not _clean_text(record.get("product_id"))
    ]
    if not unresolved_records:
        return records

    failures = []
    worker_count = min(ZYING_DETAIL_WORKERS, len(unresolved_records))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_records = {
            executor.submit(_resolve_product_id, record, token): record
            for record in unresolved_records
        }
        for future in as_completed(future_records):
            record = future_records[future]
            try:
                future.result()
            except ZyingCollectionStopped:
                for pending in future_records:
                    pending.cancel()
                raise
            except Exception as exc:
                failures.append(f"{record.get('title')!r}: {exc}")
    if failures:
        print(
            f"智赢点击前有 {len(failures)}/{len(unresolved_records)} 条产品编号"
            "未能预解析，将继续使用标题/主图识别详情："
            + "；".join(failures[:3]),
            flush=True,
        )
    return records


def _enrich_product_record(record, token):
    with requests.Session() as session:
        session.trust_env = False
        search_row = _find_product_search_row(session, token, record)

        detail_data = _zying_api_post(
            session,
            token,
            "sale.detail",
            {"id": search_row["id"]},
        )
        detail_rows = detail_data.get("root") or []
        if not detail_rows:
            raise RuntimeError(
                f"产品 {search_row['id']} 的详情接口未返回数据：{record.get('title')!r}"
            )
        return _merge_detail_record(record, search_row, detail_rows[0])


def _enrich_product_records(driver, records, token=None):
    if not records:
        return records
    token = token or _browser_auth_token(driver)
    failures = []
    worker_count = min(ZYING_DETAIL_WORKERS, len(records))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_records = {
            executor.submit(_enrich_product_record, record, token): record
            for record in records
        }
        for future in as_completed(future_records):
            record = future_records[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{record.get('title')!r}: {exc}")
    if failures:
        preview = "；".join(failures[:3])
        suffix = f"；另有 {len(failures) - 3} 条失败" if len(failures) > 3 else ""
        raise RuntimeError(
            f"智赢产品详情补全失败 {len(failures)}/{len(records)} 条，"
            f"为避免不完整数据，本次未入库。{preview}{suffix}"
        )
    _enrich_product_categories(token, records)
    return records


def _attach_zying_category(record, selection):
    if not selection:
        record.setdefault("zying_category_id", "")
        record.setdefault("zying_category", "")
        return record
    record["zying_category_id"] = selection["category_id"]
    record["zying_category"] = selection["category_path"]
    raw_text = str(record.get("raw_text") or "").rstrip()
    record["raw_text"] = "\n".join(
        filter(
            None,
            (
                raw_text,
                f"智赢分类编号: {selection['category_id']}",
                f"智赢产品分类: {selection['category_path']}",
            ),
        )
    )
    return record


def _extract_labeled_value(text, labels):
    """从卡片文本中兼容“标签: 值”和标签/值分行两种布局。"""
    lines = [_clean_text(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    lowered_labels = {label.casefold() for label in labels}

    for index, line in enumerate(lines):
        for label in labels:
            match = re.match(
                rf"^{re.escape(label)}\s*(?:[:：]|[-—])?\s*(.*)$",
                line,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            value = _clean_text(match.group(1))
            if value and value.casefold() != label.casefold():
                return value
            if index + 1 < len(lines):
                next_line = lines[index + 1]
                if next_line.casefold() not in lowered_labels:
                    return next_line
    return ""


def _element_value(element, labels=()):
    text = _clean_text(element.get_attribute("textContent") or element.text)
    if not text:
        text = _clean_text(element.get_attribute("value") or element.get_attribute("title"))
    if not labels:
        return text

    labeled_value = _extract_labeled_value(text, labels)
    if labeled_value:
        return labeled_value

    for label in labels:
        text = re.sub(
            rf"^{re.escape(label)}\s*(?:[:：]|[-—])?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
    return text


def _find_first_value(card, selectors, labels=()):
    for selector in selectors:
        for element in card.find_elements(By.CSS_SELECTOR, selector):
            value = _element_value(element, labels)
            if value:
                return value
    return ""


def _find_product_card(driver, title_element):
    card = driver.execute_script(
        "return arguments[0].closest('.product-item, [class*=\"product-item\"]');",
        title_element,
    )
    if card is not None:
        return card

    # 兼容旧页面：标题位于 product-info 内，外层第二级父元素才是卡片。
    try:
        return title_element.find_element(By.XPATH, "./../..")
    except NoSuchElementException:
        return title_element


def _extract_image_url(card):
    for image in card.find_elements(By.CSS_SELECTOR, IMAGE_SELECTOR):
        for attribute in ("src", "data-src", "data-original"):
            value = _clean_text(image.get_attribute(attribute))
            if value and not value.startswith("data:"):
                return value
        srcset = _clean_text(image.get_attribute("srcset"))
        if srcset:
            return srcset.split(",", 1)[0].strip().split(" ", 1)[0]
    return ""


def _extract_product_id(card, raw_text):
    selectors = (
        ".product-id",
        ".item-id",
        ".id-link",
        "[class*='product-id']",
        "[class*='item-id']",
    )
    value = _find_first_value(card, selectors, PRODUCT_ID_LABELS)
    if not value:
        value = _extract_labeled_value(raw_text, PRODUCT_ID_LABELS)

    match = re.search(r"\b(?:ML[A-Z]-?\d+|\d{8,})\b", value, flags=re.IGNORECASE)
    return match.group(0) if match else value


def extract_product_record(driver, title_element, page_number):
    card = _find_product_card(driver, title_element)
    raw_text = str(card.get_attribute("innerText") or card.text or "").strip()
    title = _clean_text(title_element.get_attribute("textContent") or title_element.text)

    record = {
        "product_id": _extract_product_id(card, raw_text),
        "main_image_url": _extract_image_url(card),
        "title": title,
        "page_number": page_number,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw_text": raw_text,
    }
    for field_name, definition in FIELD_DEFINITIONS.items():
        value = _extract_labeled_value(raw_text, definition["labels"])
        if not value:
            value = _find_first_value(
                card,
                definition["selectors"],
                definition["labels"],
            )
        record[field_name] = value
    return record


def _page_signature(driver):
    return _clean_text(
        driver.execute_script(
            "return Array.from(document.querySelectorAll(arguments[0]))"
            ".slice(0, 3).map(item => (item.textContent || '').trim()).join('|');",
            TITLE_SELECTOR,
        )
    )


def _active_page_number(driver):
    if hasattr(driver, "execute_script"):
        value = _clean_text(
            driver.execute_script(
                "const item = document.querySelector('.ant-pagination-item-active');"
                "return item ? (item.getAttribute('title') || item.textContent || '') : '';"
            )
        )
    else:
        active_elements = driver.find_elements(
            By.CSS_SELECTOR,
            ".ant-pagination-item-active",
        )
        if not active_elements:
            return None
        value = _clean_text(
            active_elements[0].get_attribute("title") or active_elements[0].text
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _click_pagination_element(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def _go_to_first_page(driver, wait):
    current_page = _active_page_number(driver)
    if current_page in (None, 1):
        return

    old_signature = _page_signature(driver)
    candidates = driver.find_elements(
        By.CSS_SELECTOR,
        "li.ant-pagination-item-1, li.ant-pagination-item[title='1']",
    )
    if not candidates:
        raise RuntimeError(
            f"当前在智赢第 {current_page} 页，但未找到第 1 页按钮，无法从首页开始采集。"
        )
    _click_pagination_element(driver, candidates[0])

    try:
        wait.until(
            lambda current_driver: _active_page_number(current_driver) == 1
            and _page_signature(current_driver) != old_signature
        )
    except TimeoutException as exc:
        raise RuntimeError(
            f"智赢列表从第 {current_page} 页返回第 1 页超时。"
        ) from exc
    _wait_for_product_titles(driver, wait)


def _wait_for_product_titles(driver, wait):
    def product_page_ready(current_driver):
        titles = current_driver.find_elements(By.CSS_SELECTOR, TITLE_SELECTOR)
        if titles:
            return titles

        current_url = current_driver.current_url
        if "#/login" in current_url.casefold() or current_driver.find_elements(
            By.CSS_SELECTOR, LOGIN_SELECTOR
        ):
            raise RuntimeError(
                "智赢登录状态已失效，页面已跳转到登录页："
                f"{current_url}。请先在当前 BitBrowser 窗口登录智赢，再重新运行采集脚本。"
            )
        return False

    try:
        return wait.until(product_page_ready)
    except TimeoutException as exc:
        raise RuntimeError(
            "智赢产品列表加载超时，未找到产品标题元素 "
            f"{TITLE_SELECTOR!r}。当前页面：{driver.title!r}（{driver.current_url}）。"
            "请确认页面能正常打开且列表不为空；如果页面已改版，需要更新采集选择器。"
        ) from exc


def _go_to_next_page(driver, wait):
    selectors = (
        "li[title='下一页']:not(.ant-pagination-disabled) button",
        "li.ant-pagination-next:not(.ant-pagination-disabled) button",
        "button[aria-label='Next']:not([disabled])",
        "button[aria-label='下一页']:not([disabled])",
    )
    old_signature = _page_signature(driver)
    old_page = _active_page_number(driver)

    next_button = None
    for selector in selectors:
        candidates = driver.find_elements(By.CSS_SELECTOR, selector)
        if candidates:
            next_button = candidates[0]
            break
    if next_button is None:
        return False

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", next_button)
    try:
        next_button.click()
    except Exception:
        driver.execute_script("arguments[0].click();", next_button)

    def page_changed(current_driver):
        current_page = _active_page_number(current_driver)
        return (
            old_page is not None
            and current_page is not None
            and current_page != old_page
            and _page_signature(current_driver) != old_signature
        )

    try:
        wait.until(page_changed)
    except TimeoutException as exc:
        raise RuntimeError(
            f"智赢列表从第 {old_page or '未知'} 页自动翻到下一页超时。"
        ) from exc
    return True


def _record_key(record):
    product_id = _clean_text(record.get("product_id"))
    if product_id:
        return ("id", product_id)
    return (
        "content",
        _clean_text(record.get("title")),
        _clean_text(record.get("main_image_url")),
    )


def _skip_existing_zying_records(
    records,
    existing_product_id_reader,
    known_existing_ids=None,
    checked_product_ids=None,
):
    """按产品编号过滤数据库已存在商品，避免继续打开详情页。"""
    known_existing_ids = (
        known_existing_ids if known_existing_ids is not None else set()
    )
    checked_product_ids = (
        checked_product_ids if checked_product_ids is not None else set()
    )
    product_ids = {
        _clean_text(record.get("product_id"))
        for record in (records or ())
        if _clean_text(record.get("product_id"))
    }
    unchecked_ids = product_ids - checked_product_ids
    if unchecked_ids:
        existing_ids = existing_product_id_reader(sorted(unchecked_ids)) or ()
        checked_product_ids.update(unchecked_ids)
        known_existing_ids.update(
            _clean_text(product_id)
            for product_id in existing_ids
            if _clean_text(product_id)
        )
    filtered_records = [
        record
        for record in (records or ())
        if not _clean_text(record.get("product_id"))
        or _clean_text(record.get("product_id")) not in known_existing_ids
    ]
    return filtered_records, len(records or ()) - len(filtered_records)


def _deduplicate_zying_records(records, previously_seen=None):
    """同一批及先前页面中的产品编号只保留第一条。"""
    previously_seen = previously_seen or set()
    batch_seen = set()
    filtered_records = []
    for record in records or ():
        product_id = _clean_text(record.get("product_id"))
        if not product_id:
            filtered_records.append(record)
            continue
        key = ("id", product_id)
        if key in previously_seen or key in batch_seen:
            continue
        batch_seen.add(key)
        filtered_records.append(record)
    return filtered_records, len(records or ()) - len(filtered_records)


def _persist_zying_page(
    page_records,
    page_number,
    page_count,
    product_writer=None,
    product_mirror_writer=None,
):
    """同步提交单页数据；只有数据库事务提交成功后，采集器才会继续翻页。"""
    if not page_records:
        print(
            f"智赢第 {page_number}/{page_count} 页没有可入库商品，继续下一页",
            flush=True,
        )
        return 0

    print(
        f"智赢第 {page_number}/{page_count} 页读取完成，正在立即提交数据库 "
        f"({len(page_records)} 条)",
        flush=True,
    )
    if product_mirror_writer is not None:
        mirror_result = product_mirror_writer(page_records) or {}
        print(
            f"智赢第 {page_number}/{page_count} 页已同步产品列表 "
            f"{int(mirror_result.get('count') or 0)} 条",
            flush=True,
        )
    writer = product_writer or insert_zying_product_info
    inserted_count = writer(page_records)
    print(
        f"智赢第 {page_number}/{page_count} 页数据库提交完成："
        f"{inserted_count} 条；后续即使中断，本页数据仍已保留",
        flush=True,
    )
    return inserted_count


def normalize_zying_browser_type(value):
    """将页面和命令行中的浏览器类型统一为 edge 或 bitbrowser。"""
    normalized = str(value or DEFAULT_ZYING_BROWSER_TYPE).strip().lower()
    aliases = {
        "bit": "bitbrowser",
        "bit_browser": "bitbrowser",
        "bit-browser": "bitbrowser",
        "比特": "bitbrowser",
        "比特浏览器": "bitbrowser",
        "local_edge": "edge",
        "local-edge": "edge",
        "msedge": "edge",
        "本地edge": "edge",
        "本地 edge": "edge",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"edge", "bitbrowser"}:
        raise ValueError("浏览器类型只能选择本地 Edge 或比特浏览器")
    return normalized


def _edge_debugger_address(value):
    address = str(value or DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS).strip()
    address = re.sub(r"^https?://", "", address, flags=re.I).rstrip("/")
    if not address:
        raise ValueError("本地 Edge 调试地址不能为空")
    return address


def _edge_debugger_ready(debugger_address, timeout=0.5):
    try:
        host, port_text = debugger_address.rsplit(":", 1)
        with socket.create_connection((host, int(port_text)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _find_edge_executable():
    configured = _clean_text(os.environ.get("BIT_ZYING_EDGE_EXECUTABLE"))
    candidates = [configured] if configured else []
    candidates.extend(
        [
            str(Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe"),
            str(Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    raise RuntimeError("未找到 Microsoft Edge，请改用比特浏览器登录")


def ensure_visible_zying_edge_login_window(
    debugger_address=DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS,
):
    """启动独立的可视 Edge 登录窗口，不要求关闭用户日常使用的 Edge。"""
    debugger_address = _edge_debugger_address(debugger_address)
    if _edge_debugger_ready(debugger_address):
        return False
    host, port_text = debugger_address.rsplit(":", 1)
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"无法自动启动远程 Edge 调试地址：{debugger_address}")

    ZYING_EDGE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        _find_edge_executable(),
        f"--remote-debugging-port={int(port_text)}",
        f"--user-data-dir={ZYING_EDGE_PROFILE_DIR}",
        "--no-first-run",
        "--new-window",
        ZYING_PRODUCT_URL,
    ]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if _edge_debugger_ready(debugger_address):
            return True
        time.sleep(0.25)
    raise RuntimeError(
        f"已尝试打开 Edge，但调试端口 {debugger_address} 未就绪；"
        "请检查安全软件拦截，或改用比特浏览器登录"
    )


def _open_zying_collection_browser(
    browser_type,
    window_id,
    window_name="",
    edge_debugger_address=DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS,
):
    """连接采集浏览器，返回 driver、driver service 和需释放锁的窗口 ID。"""
    browser_type = normalize_zying_browser_type(browser_type)
    if browser_type == "edge":
        debugger_address = _edge_debugger_address(edge_debugger_address)
        edge_options = webdriver.EdgeOptions()
        edge_options.add_experimental_option("debuggerAddress", debugger_address)
        try:
            driver = webdriver.Edge(options=edge_options)
        except Exception as exc:
            raise RuntimeError(
                f"无法连接本地 Edge（{debugger_address}）。请完全退出 Edge 后，"
                "使用 --remote-debugging-port=9222 启动 Edge，并确认已登录智赢"
            ) from exc
        print(f"已连接本地 Edge：{debugger_address}", flush=True)
        return driver, getattr(driver, "service", None), ""

    requested_name = _clean_text(window_name)
    resolved_window_id = (
        getBrowserIdByName(requested_name)
        if requested_name
        else _clean_text(window_id) or DEFAULT_ZYING_WINDOW_ID
    )
    browser_info = openBrowser(resolved_window_id)
    if not browser_info or not browser_info.get("data"):
        identifier = f"窗口“{requested_name}”" if requested_name else f"窗口 ID {resolved_window_id}"
        raise RuntimeError(f"打开比特浏览器{identifier}失败：{browser_info}")

    driver_path = browser_info["data"]["driver"]
    debugger_address = browser_info["data"]["http"]
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debugger_address)
    chrome_service = Service(driver_path)
    try:
        driver = webdriver.Chrome(
            service=chrome_service,
            options=chrome_options,
        )
    except Exception:
        try:
            chrome_service.stop()
        finally:
            releaseBrowserLease(resolved_window_id)
        raise
    opened_name = _clean_text(browser_info.get("data", {}).get("name")) or requested_name
    print(
        f"已连接比特浏览器窗口：{opened_name or resolved_window_id}",
        flush=True,
    )
    return driver, chrome_service, resolved_window_id


def _release_zying_browser_connection(browser_service, leased_window_id):
    if browser_service is not None:
        try:
            browser_service.stop()
        except Exception:
            pass
    if leased_window_id:
        releaseBrowserLease(leased_window_id)


def _activate_zying_page(driver):
    """Switch the selected browser to an existing Zying tab or open a new one."""
    handles = list(driver.window_handles)
    current_handle = ""
    try:
        current_handle = driver.current_window_handle
    except Exception:
        pass
    ordered_handles = []
    if current_handle in handles:
        ordered_handles.append(current_handle)
    ordered_handles.extend(
        handle for handle in reversed(handles) if handle != current_handle
    )

    selected_handle = ""
    for handle in ordered_handles:
        try:
            driver.switch_to.window(handle)
            if "meli.zying.net" in str(driver.current_url or "").casefold():
                selected_handle = handle
                break
        except Exception:
            continue

    if not selected_handle:
        try:
            driver.switch_to.new_window("tab")
        except Exception:
            driver.execute_script("window.open('about:blank', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
        driver.get(ZYING_PRODUCT_URL)

    try:
        driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception:
        try:
            driver.execute_script("window.focus();")
        except Exception:
            pass
    return str(driver.current_url or "")


def open_zying_login_window(
    *,
    browser_type=DEFAULT_ZYING_BROWSER_TYPE,
    window_id=DEFAULT_ZYING_WINDOW_ID,
    window_name="",
    edge_debugger_address=DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS,
):
    """打开可视登录页；浏览器保持打开，WebDriver 连接立即释放。"""
    browser_type = normalize_zying_browser_type(browser_type)
    if browser_type == "edge":
        started = ensure_visible_zying_edge_login_window(edge_debugger_address)
        if started:
            return {
                "browser_type": browser_type,
                "window_name": "",
                "message": "已打开独立的 Edge 智赢登录窗口，请完成登录",
            }

    driver = browser_service = None
    leased_window_id = ""
    try:
        driver, browser_service, leased_window_id = _open_zying_collection_browser(
            browser_type,
            window_id,
            window_name=window_name,
            edge_debugger_address=edge_debugger_address,
        )
        _activate_zying_page(driver)
        target_name = _clean_text(window_name)
        target_label = (
            f"比特浏览器窗口“{target_name}”"
            if browser_type == "bitbrowser" and target_name
            else "所选浏览器"
        )
        return {
            "browser_type": browser_type,
            "window_name": target_name,
            "message": f"已切换到{target_label}的智赢页面，请完成登录",
        }
    finally:
        _release_zying_browser_connection(browser_service, leased_window_id)


def capture_zying_login_from_browser(
    *,
    browser_type=DEFAULT_ZYING_BROWSER_TYPE,
    window_id=DEFAULT_ZYING_WINDOW_ID,
    window_name="",
    edge_debugger_address=DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS,
    auth_file=None,
    validate=True,
):
    """从当前所选浏览器读取凭证；可按需跳过独立登录检测。"""
    driver = browser_service = None
    leased_window_id = ""
    try:
        driver, browser_service, leased_window_id = _open_zying_collection_browser(
            browser_type,
            window_id,
            window_name=window_name,
            edge_debugger_address=edge_debugger_address,
        )
        _activate_zying_page(driver)
        target_name = _clean_text(window_name)
        target_label = (
            f"比特浏览器窗口“{target_name}”"
            if browser_type == "bitbrowser" and target_name
            else "当前页面所选浏览器"
        )
        if validate:
            try:
                WebDriverWait(driver, 10).until(_browser_has_auth_token)
            except Exception as exc:
                raise ZyingAuthenticationError(
                    f"已切换到{target_label}的智赢页面，但尚未检测到登录状态；"
                    "请在该窗口完成登录后重试"
                ) from exc
        try:
            token = _browser_auth_token(driver)
        except Exception as exc:
            raise ZyingAuthenticationError(
                f"无法从{target_label}读取智赢登录凭证；"
                "请在该窗口登录智赢后直接重新启动采集"
            ) from exc
        if validate:
            validate_zying_auth_token(token)
        return save_zying_auth_token(
            token,
            browser_type=browser_type,
            window_name=window_name,
            window_id=leased_window_id or window_id,
            auth_file=auth_file,
        )
    finally:
        _release_zying_browser_connection(browser_service, leased_window_id)


def _zying_api_category_selection(category, category_name=""):
    category_id = _clean_text(category)
    category_path = _clean_text(category_name) or category_id
    if not category_id:
        return None
    return {
        "category_id": category_id,
        "category_path": category_path,
    }


def _zying_api_list_record(row, page_number, collected_at):
    product_id = _format_number(row.get("id"))
    if not product_id:
        raise RuntimeError(f"智赢接口列表第 {page_number} 页存在缺少产品编号的数据")
    currency = _clean_text(row.get("cur"))
    price = row.get("cost") if row.get("cost") is not None else row.get("price")
    title = _clean_text(html.unescape(re.sub(r"<[^>]+>", "", str(row.get("title") or ""))))
    record = {
        "product_id": product_id,
        "main_image_url": _clean_text(row.get("thumb")),
        "title": title,
        "page_number": page_number,
        "collected_at": collected_at,
        "raw_text": json.dumps(row, ensure_ascii=False, default=str),
        "sale_price": _format_money(price, currency),
    }
    for field_name in FIELD_DEFINITIONS:
        record.setdefault(field_name, "")
    return record


def _zying_api_record_matches_category(record, category_id):
    requested = _clean_text(category_id)
    if not requested:
        return True
    detail = record.get("detail_data") or {}
    actual = _format_number(
        detail.get("sale_localid")
        or detail.get("localid")
        or detail.get("category_local_id")
    )
    # 只有数字 ID 才能做可靠的二次核验；手工输入名称时依赖接口筛选。
    return not requested.isdigit() or actual == requested


@_reuse_zying_frontend_signer
def collect_zying_products_api(
    *,
    auth_token=None,
    number=None,
    start_page=DEFAULT_ZYING_START_PAGE,
    category=None,
    category_name="",
    product_writer=None,
    existing_product_id_reader=None,
    product_mirror_writer=None,
    return_summary=False,
    stop_event=None,
):
    """通过智赢 API 采集；无头 Edge 仅加载官方签名模块，不读取页面 DOM。"""
    page_count = max(1, int(DEFAULT_ZYING_PAGE_COUNT if number is None else number))
    start_page = max(1, int(start_page))
    if start_page > page_count:
        raise ValueError(f"起始页 {start_page} 不能大于结束页 {page_count}。")
    token = _clean_text(auth_token) or load_zying_auth_token()
    category_selection = _zying_api_category_selection(category, category_name)
    product_writer = product_writer or insert_zying_product_info
    existing_product_id_reader = (
        existing_product_id_reader or get_existing_zying_product_ids
    )
    if product_mirror_writer is None:
        from erp.mercadolibre_collection_store import upsert_zying_products_to_products

        product_mirror_writer = upsert_zying_products_to_products

    started_at = time.time()
    records = []
    seen = set()
    known_existing_ids = set()
    checked_product_ids = set()
    inserted_count = 0
    skipped_existing_count = 0
    duplicate_count = 0
    category_mismatch_count = 0
    last_committed_page = start_page - 1
    print(
        f"智赢 API 后台采集直接启动：第 {start_page}-{page_count} 页，"
        f"分类 {category_selection['category_path'] if category_selection else '全部'}",
        flush=True,
    )

    with requests.Session() as session:
        session.trust_env = False
        for page_number in range(start_page, page_count + 1):
            _raise_if_zying_collection_stopped(stop_event)
            payload = {
                "page": page_number,
                "pagesize": ZYING_API_PAGE_SIZE,
                "word": "",
                "from": ZYING_MELI_PLATFORM_ID,
            }
            if category_selection:
                # 智赢产品分类的 Cascader 末级值对应 sale_localid。
                payload["localid"] = category_selection["category_id"]
            data = _zying_api_post(session, token, "sale.stat", payload)
            _raise_if_zying_collection_stopped(stop_event)
            listing = data.get("list") if isinstance(data, dict) else None
            rows = listing.get("data") if isinstance(listing, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError(
                    f"智赢接口列表第 {page_number} 页返回格式异常，请刷新登录状态后重试"
                )
            collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            page_records = [
                _zying_api_list_record(row, page_number, collected_at)
                for row in rows
                if isinstance(row, dict)
            ]
            print(
                f"智赢 API 列表第 {page_number}/{page_count} 页返回 {len(page_records)} 条",
                flush=True,
            )
            page_records, page_duplicate_count = _deduplicate_zying_records(
                page_records,
                seen,
            )
            duplicate_count += page_duplicate_count
            page_records, skipped = _skip_existing_zying_records(
                page_records,
                existing_product_id_reader,
                known_existing_ids,
                checked_product_ids,
            )
            skipped_existing_count += skipped
            if page_records:
                _enrich_product_records(None, page_records, token=token)
            _raise_if_zying_collection_stopped(stop_event)
            if category_selection:
                matched_records = [
                    record
                    for record in page_records
                    if _zying_api_record_matches_category(
                        record,
                        category_selection["category_id"],
                    )
                ]
                mismatch_count = len(page_records) - len(matched_records)
                category_mismatch_count += mismatch_count
                page_records = matched_records
                if mismatch_count:
                    print(
                        f"智赢接口第 {page_number} 页过滤掉 {mismatch_count} 条分类不匹配产品",
                        flush=True,
                    )
            page_records, skipped_after_detail = _skip_existing_zying_records(
                page_records,
                existing_product_id_reader,
                known_existing_ids,
                checked_product_ids,
            )
            skipped_existing_count += skipped_after_detail
            for record in page_records:
                _attach_zying_category(record, category_selection)
                _finalize_zying_listing_snapshot(record)
            _raise_if_zying_collection_stopped(stop_event)
            inserted_count += _persist_zying_page(
                page_records,
                page_number,
                page_count,
                product_writer=product_writer,
                product_mirror_writer=product_mirror_writer,
            )
            records.extend(page_records)
            seen.update({_record_key(record) for record in page_records})
            last_committed_page = page_number
            if not rows:
                print("智赢接口已没有更多产品，提前结束采集", flush=True)
                break

    summary = {
        "records": records,
        "collected_count": len(records),
        "inserted_count": inserted_count,
        "skipped_count": skipped_existing_count + duplicate_count,
        "skipped_existing_count": skipped_existing_count,
        "duplicate_count": duplicate_count,
        "category_mismatch_count": category_mismatch_count,
        "detail_failed_count": 0,
        "last_committed_page": last_committed_page,
        "elapsed_seconds": round(time.time() - started_at, 2),
        "collection_mode": "api",
    }
    print(
        f"智赢 API 后台采集完成：入库 {inserted_count} 条，"
        f"已有产品跳过 {skipped_existing_count} 条，重复跳过 {duplicate_count} 条",
        flush=True,
    )
    return summary if return_summary else records


def collect_zying_products(
    number=None,
    window_id=DEFAULT_ZYING_WINDOW_ID,
    start_page=DEFAULT_ZYING_START_PAGE,
    category=None,
    product_writer=None,
    existing_product_id_reader=None,
    product_mirror_writer=None,
    return_summary=False,
    browser_type=DEFAULT_ZYING_BROWSER_TYPE,
    window_name="",
    edge_debugger_address=DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS,
    category_name="",
    auth_token=None,
    api_mode=True,
    stop_event=None,
):
    """采集智赢产品；默认使用后台 API 与无头官方签名模式。"""
    if api_mode:
        _raise_if_zying_collection_stopped(stop_event)
        resolved_auth_token = _clean_text(auth_token)
        if not resolved_auth_token:
            print("正在从当前页面所选浏览器读取智赢登录凭证", flush=True)
            capture_zying_login_from_browser(
                browser_type=browser_type,
                window_id=window_id,
                window_name=window_name,
                edge_debugger_address=edge_debugger_address,
                validate=False,
            )
            _raise_if_zying_collection_stopped(stop_event)
            resolved_auth_token = load_zying_auth_token()
            print("已读取智赢登录凭证，直接开始采集", flush=True)
        return collect_zying_products_api(
            auth_token=resolved_auth_token,
            number=number,
            start_page=start_page,
            category=DEFAULT_ZYING_CATEGORY if category is None else category,
            category_name=category_name,
            product_writer=product_writer,
            existing_product_id_reader=existing_product_id_reader,
            product_mirror_writer=product_mirror_writer,
            return_summary=return_summary,
            stop_event=stop_event,
        )
    requested_pages = DEFAULT_ZYING_PAGE_COUNT if number is None else number
    page_count = max(1, int(requested_pages))
    start_page = max(1, int(start_page))
    requested_category = _clean_text(
        DEFAULT_ZYING_CATEGORY if category is None else category
    )
    if start_page > page_count:
        raise ValueError(
            f"起始页 {start_page} 不能大于结束页 {page_count}。"
        )
    started_at = time.time()
    browser_type = normalize_zying_browser_type(browser_type)
    driver, browser_service, leased_window_id = _open_zying_collection_browser(
        browser_type,
        window_id,
        window_name=window_name,
        edge_debugger_address=edge_debugger_address,
    )
    wait = WebDriverWait(driver, 30)
    records = []
    seen = set()
    known_existing_ids = set()
    checked_product_ids = set()
    inserted_count = 0
    skipped_count = 0
    skipped_existing_count = 0
    duplicate_count = 0
    category_selection = None
    last_committed_page = start_page - 1
    product_writer = product_writer or insert_zying_product_info
    existing_product_id_reader = (
        existing_product_id_reader or get_existing_zying_product_ids
    )
    if product_mirror_writer is None:
        from erp.mercadolibre_collection_store import upsert_zying_products_to_products

        product_mirror_writer = upsert_zying_products_to_products

    try:
        driver.get(ZYING_PRODUCT_URL)
        _wait_for_product_titles(driver, wait)
        if requested_category:
            category_selection = _apply_zying_category_filter(
                driver,
                wait,
                requested_category,
            )
        _go_to_first_page(driver, wait)
        for next_page in range(2, start_page + 1):
            if not _go_to_next_page(driver, wait):
                raise RuntimeError(
                    f"无法跳转到续跑起始页 {start_page}，在第 {next_page} 页前停止。"
                )
        print(
            f"智赢自动翻页采集开始，计划采集第 {start_page}-{page_count} 页，"
            f"共 {page_count - start_page + 1} 页"
            + (
                f"，智赢产品分类：{category_selection['category_path']}"
                if category_selection
                else "，智赢产品分类：全部"
            ),
            flush=True,
        )
        token = _browser_auth_token(driver)

        for page_number in range(start_page, page_count + 1):
            title_elements = _wait_for_product_titles(driver, wait)
            extracted_records = [
                extract_product_record(driver, title_element, page_number)
                for title_element in title_elements
            ]
            extracted_records, skipped_before_resolve = _skip_existing_zying_records(
                extracted_records,
                existing_product_id_reader,
                known_existing_ids,
                checked_product_ids,
            )
            skipped_existing_count += skipped_before_resolve
            _resolve_product_ids(token, extracted_records)
            extracted_records, skipped_after_resolve = _skip_existing_zying_records(
                extracted_records,
                existing_product_id_reader,
                known_existing_ids,
                checked_product_ids,
            )
            skipped_existing_count += skipped_after_resolve
            extracted_records, page_duplicate_count = _deduplicate_zying_records(
                extracted_records,
                seen,
            )
            duplicate_count += page_duplicate_count
            page_existing_count = skipped_before_resolve + skipped_after_resolve
            if page_existing_count or page_duplicate_count:
                print(
                    f"智赢第 {page_number}/{page_count} 页在详情采集前已跳过 "
                    f"数据库已有产品 {page_existing_count} 条，"
                    f"重复产品 {page_duplicate_count} 条",
                    flush=True,
                )
            detail_candidate_count = len(extracted_records)
            extracted_records = _collect_clicked_product_details(
                driver,
                extracted_records,
                page_number=page_number,
                page_count=page_count,
            )
            page_skipped_count = detail_candidate_count - len(extracted_records)
            skipped_count += page_skipped_count
            _enrich_product_records(driver, extracted_records, token=token)
            extracted_records, skipped_after_enrich = _skip_existing_zying_records(
                extracted_records,
                existing_product_id_reader,
                known_existing_ids,
                checked_product_ids,
            )
            skipped_existing_count += skipped_after_enrich
            extracted_records, duplicates_after_enrich = _deduplicate_zying_records(
                extracted_records,
                seen,
            )
            duplicate_count += duplicates_after_enrich
            if skipped_after_enrich or duplicates_after_enrich:
                print(
                    f"智赢第 {page_number}/{page_count} 页详情补全后又跳过 "
                    f"数据库已有产品 {skipped_after_enrich} 条，"
                    f"重复产品 {duplicates_after_enrich} 条",
                    flush=True,
                )
            for record in extracted_records:
                _attach_zying_category(record, category_selection)
                _finalize_zying_listing_snapshot(record)

            page_records = []
            for record in extracted_records:
                key = _record_key(record)
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
                page_records.append(record)

            page_inserted_count = _persist_zying_page(
                page_records,
                page_number,
                page_count,
                product_writer=product_writer,
                product_mirror_writer=product_mirror_writer,
            )
            inserted_count += page_inserted_count
            last_committed_page = page_number
            print(
                f"智赢产品第 {page_number}/{page_count} 页采集 "
                f"{len(page_records)} 条，详情失败跳过 {page_skipped_count} 条，"
                f"入库 {page_inserted_count} 条",
                flush=True,
            )
            if page_number >= page_count or not _go_to_next_page(driver, wait):
                break
    except Exception:
        if last_committed_page >= start_page:
            print(
                f"智赢采集在第 {last_committed_page + 1} 页附近中断；"
                f"第 {start_page}-{last_committed_page} 页已经逐页提交数据库。"
                f"下次可使用 --start-page {last_committed_page + 1} 继续",
                flush=True,
            )
        raise
    finally:
        # 只停止本次 WebDriver 连接，不关闭用户的 Edge/BitBrowser 窗口。
        try:
            if browser_service is not None:
                browser_service.stop()
        finally:
            if leased_window_id:
                releaseBrowserLease(leased_window_id)

    print(
        f"智赢产品采集完成，共 {len(records)} 条，详情失败跳过 {skipped_count} 条，"
        f"已有产品跳过 {skipped_existing_count} 条，页面重复跳过 {duplicate_count} 条，"
        f"入库 {inserted_count} 条，"
        f"耗时 {int(time.time() - started_at)} 秒",
        flush=True,
    )
    if return_summary:
        return {
            "records": records,
            "collected_count": len(records),
            "inserted_count": inserted_count,
            "skipped_existing_count": skipped_existing_count,
            "duplicate_count": duplicate_count,
            "detail_failed_count": skipped_count,
            "start_page": start_page,
            "end_page": last_committed_page,
            "category": (
                category_selection.get("category_path", "")
                if category_selection
                else ""
            ),
        }
    return records


def check_yuanyou_title(
    number=None,
    window_id=DEFAULT_ZYING_WINDOW_ID,
    category=None,
    browser_type=DEFAULT_ZYING_BROWSER_TYPE,
    window_name="",
):
    """保留旧函数名，兼容已有的手工调用方式。"""
    return collect_zying_products(
        number=number,
        window_id=window_id,
        category=category,
        browser_type=browser_type,
        window_name=window_name,
    )


def get_all_ids(text):
    product_ids = re.findall(r"\b\d{9}\b", str(text or ""))
    return sorted(set(product_ids))


def main():
    parser = argparse.ArgumentParser(description="采集智赢产品数据并写入数据库")
    parser.add_argument("pages", nargs="?", type=int, help="采集页数（兼容位置参数）")
    parser.add_argument(
        "--pages",
        dest="pages_option",
        type=int,
        help=f"自动采集页数（默认 {DEFAULT_ZYING_PAGE_COUNT}，也可设置 BIT_ZYING_PAGES）",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=DEFAULT_ZYING_START_PAGE,
        help=(
            "从指定页续跑，--pages 表示结束页"
            f"（默认 {DEFAULT_ZYING_START_PAGE}，也可设置 BIT_ZYING_START_PAGE）"
        ),
    )
    parser.add_argument("--window-id", default=DEFAULT_ZYING_WINDOW_ID, help="比特浏览器窗口 ID（兼容旧用法）")
    parser.add_argument(
        "--browser-type",
        default=DEFAULT_ZYING_BROWSER_TYPE,
        choices=("bitbrowser", "edge"),
        help="采集浏览器：bitbrowser 或 edge",
    )
    parser.add_argument("--window-name", default="", help="比特浏览器窗口名称（优先于窗口 ID）")
    parser.add_argument(
        "--edge-debugger-address",
        default=DEFAULT_ZYING_EDGE_DEBUGGER_ADDRESS,
        help="本地 Edge 远程调试地址",
    )
    parser.add_argument(
        "--category",
        default=DEFAULT_ZYING_CATEGORY,
        help=(
            "指定智赢页面的产品分类，可填写智赢分类 ID、唯一分类名或完整路径"
            "（例如：圆佑同步/家电类）"
        ),
    )
    args = parser.parse_args()
    page_count = (
        args.pages_option
        if args.pages_option is not None
        else args.pages
        if args.pages is not None
        else DEFAULT_ZYING_PAGE_COUNT
    )
    try:
        collect_zying_products(
            page_count,
            args.window_id,
            args.start_page,
            category=args.category,
            browser_type=args.browser_type,
            window_name=args.window_name,
            edge_debugger_address=args.edge_debugger_address,
        )
    except (RuntimeError, ValueError) as exc:
        parser.exit(status=1, message=f"采集失败：{exc}\n")


if __name__ == "__main__":
    main()
