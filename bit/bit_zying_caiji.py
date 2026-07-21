import argparse
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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

from bit.bit_api import openBrowser, releaseBrowserLease
from bit.bit_mysql import insert_zying_product_info


DEFAULT_ZYING_WINDOW_ID = os.environ.get(
    "BIT_ZYING_WINDOW_ID",
    "9812f185f7ab49d98f3988994d9e8ebf",
)
ZYING_PRODUCT_URL = os.environ.get(
    "BIT_ZYING_PRODUCT_URL",
    "https://meli.zying.net/#/product",
)
ZYING_API_ORIGIN = os.environ.get(
    "BIT_ZYING_API_ORIGIN",
    "https://seller.zying.net",
).rstrip("/")
DEFAULT_ZYING_PAGE_COUNT = max(1, int(os.environ.get("BIT_ZYING_PAGES", "1")))
DEFAULT_ZYING_START_PAGE = max(
    1,
    int(os.environ.get("BIT_ZYING_START_PAGE", "1")),
)
ZYING_DETAIL_WORKERS = max(1, int(os.environ.get("BIT_ZYING_DETAIL_WORKERS", "6")))
ZYING_DETAIL_CLICK_TIMEOUT = max(
    15,
    int(os.environ.get("BIT_ZYING_DETAIL_CLICK_TIMEOUT", "45")),
)

TITLE_SELECTOR = ".f12.product-title, .product-title"
IMAGE_SELECTOR = "img.product-pic, img[class*='product-pic'], img[class*='product-image']"
LOGIN_SELECTOR = "input[type='password'], #password"

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
const titleMatches = Boolean(expectedTitle) && detailTitles.includes(expectedTitle);
const imageMatches = Boolean(expectedImage) && detailImages.includes(expectedImage);
if (!titleMatches && !imageMatches) return null;

const value = id => (root.querySelector(`#${id}`)?.value || '').trim();
const checkedStatus = root.querySelector("input[name='stat']:checked");
const status = (
  checkedStatus?.closest('label')?.querySelector('.ant-radio-label')?.textContent || ''
).trim();
const details = {
  product_id: productId,
  sale_price: value('cost'),
  net_income: value('netproceed'),
  package_gross_weight: value('weight'),
  size_length: value('sizeLength'),
  size_width: value('sizeWidth'),
  size_height: value('sizeHeight'),
  review_status: status,
};
return Object.values(details).every(Boolean) ? details : null;
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


def _browser_auth_token(driver):
    stored_token = driver.execute_script("return localStorage.getItem('token');")
    if not stored_token:
        raise RuntimeError("未能从智赢页面读取登录凭证，请重新登录后再运行采集脚本。")
    try:
        token = json.loads(stored_token)
    except (TypeError, ValueError):
        token = stored_token
    token = str(token or "").strip()
    if not token:
        raise RuntimeError("智赢登录凭证为空，请重新登录后再运行采集脚本。")
    return token


def _signed_api_headers(token, path, body, timestamp=None):
    timestamp = str(timestamp or int(time.time() + 0.5))
    message = f"{body}POST{path}{timestamp}{token}v1"
    signature = hmac.new(
        ZYING_API_ORIGIN.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "appclient": "2",
        "signature": signature,
        "timestamp": timestamp,
        "token": token,
        "version": "v1",
    }


def _zying_api_post(session, token, command, payload):
    path = f"/api/CmdHandler?cmd={command}"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    last_error = None
    for attempt in range(3):
        try:
            response = session.post(
                f"{ZYING_API_ORIGIN}{path}",
                data=body.encode("utf-8"),
                headers=_signed_api_headers(token, path, body),
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") == 200:
                return result.get("data") or {}
            message = result.get("message") or f"业务状态码 {result.get('code')}"
            if result.get("code") == 401:
                raise RuntimeError(f"智赢登录状态已失效：{message}")
            last_error = RuntimeError(f"智赢接口 {command} 请求失败：{message}")
        except (requests.RequestException, ValueError) as exc:
            last_error = RuntimeError(f"智赢接口 {command} 请求失败：{exc}")
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    raise last_error


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


def _detail_category_reference(detail):
    try:
        attributes = json.loads(detail.get("sale_attrs") or "{}")
    except (TypeError, ValueError):
        attributes = {}
    site_attributes = attributes.get(str(detail.get("sale_siteid") or "")) or {}
    if not site_attributes:
        site_attributes = next(
            (value for value in attributes.values() if isinstance(value, dict)),
            {},
        )
    site = _clean_text(site_attributes.get("site") or detail.get("sale_area"))
    category_id = _format_number(site_attributes.get("kindid"))
    return site, category_id


def _merge_detail_record(record, search_row, detail):
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


def _click_product_card(driver, card):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", card)
    try:
        card.click()
    except Exception:
        driver.execute_script(
            "arguments[0].dispatchEvent(new MouseEvent('click', "
            "{bubbles: true, cancelable: true, view: window}));",
            card,
        )


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
    for index, record in enumerate(records):
        expected_title = _clean_text(record.get("title"))
        expected_image = _clean_text(record.get("main_image_url"))
        if not expected_title or not expected_image:
            raise RuntimeError(f"点击详情前缺少标题或主图：{record!r}")

        # 每条产品都实际点击，不能复用上一个详情；主图负责确认点击后的详情。
        details = None
        last_error = None
        for attempt in range(2):
            if details:
                break
            print(
                f"智赢{page_label}，正在点击详情 {index + 1}/{len(records)}，"
                f"第 {attempt + 1}/2 次，标题 {expected_title!r}",
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
            _click_product_card(driver, card)
            try:
                details = WebDriverWait(
                    driver,
                    ZYING_DETAIL_CLICK_TIMEOUT,
                    poll_frequency=0.25,
                ).until(
                    lambda current_driver: current_driver.execute_script(
                        DETAIL_FORM_SCRIPT,
                        expected_title,
                        expected_image,
                    )
                )
            except TimeoutException as exc:
                last_error = exc
                print(
                    f"智赢{page_label}详情 {index + 1}/{len(records)} 等待 "
                    f"{ZYING_DETAIL_CLICK_TIMEOUT} 秒未完成，准备刷新重试",
                    flush=True,
                )
                refresh_buttons = driver.find_elements(
                    By.CSS_SELECTOR,
                    ".curd-detail-wrap .crud-detail-refresh",
                )
                if refresh_buttons:
                    driver.execute_script("arguments[0].click();", refresh_buttons[0])

        if not details:
            current_ids = driver.find_elements(
                By.CSS_SELECTOR,
                ".curd-detail-wrap .crud-detail-header .h1",
            )
            current_id = (
                _clean_text(current_ids[0].get_attribute("textContent"))
                if current_ids
                else ""
            )
            current_titles = driver.find_elements(
                By.CSS_SELECTOR,
                ".curd-detail-wrap textarea[placeholder='请输入内容']",
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
    return completed_records


def _enrich_product_record(record, token):
    with requests.Session() as session:
        session.trust_env = False
        product_id = _clean_text(record.get("product_id"))
        if product_id:
            search_row = {"id": product_id}
        else:
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


def _enrich_product_records(driver, records):
    if not records:
        return records
    token = _browser_auth_token(driver)
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


def collect_zying_products(
    number=None,
    window_id=DEFAULT_ZYING_WINDOW_ID,
    start_page=DEFAULT_ZYING_START_PAGE,
):
    """采集指定页数的智赢产品卡片，并批量写入 zying_product 表。"""
    requested_pages = DEFAULT_ZYING_PAGE_COUNT if number is None else number
    page_count = max(1, int(requested_pages))
    start_page = max(1, int(start_page))
    if start_page > page_count:
        raise ValueError(
            f"起始页 {start_page} 不能大于结束页 {page_count}。"
        )
    started_at = time.time()
    browser_info = openBrowser(window_id)
    if not browser_info or not browser_info.get("data"):
        raise RuntimeError(f"打开 BitBrowser 窗口失败：{browser_info}")

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
        releaseBrowserLease(window_id)
        raise
    wait = WebDriverWait(driver, 30)
    records = []
    seen = set()
    inserted_count = 0
    skipped_count = 0

    try:
        driver.get(ZYING_PRODUCT_URL)
        _wait_for_product_titles(driver, wait)
        _go_to_first_page(driver, wait)
        for next_page in range(2, start_page + 1):
            if not _go_to_next_page(driver, wait):
                raise RuntimeError(
                    f"无法跳转到续跑起始页 {start_page}，在第 {next_page} 页前停止。"
                )
        print(
            f"智赢自动翻页采集开始，计划采集第 {start_page}-{page_count} 页，"
            f"共 {page_count - start_page + 1} 页",
            flush=True,
        )

        for page_number in range(start_page, page_count + 1):
            title_elements = _wait_for_product_titles(driver, wait)
            extracted_records = [
                extract_product_record(driver, title_element, page_number)
                for title_element in title_elements
            ]
            listed_count = len(extracted_records)
            extracted_records = _collect_clicked_product_details(
                driver,
                extracted_records,
                page_number=page_number,
                page_count=page_count,
            )
            page_skipped_count = listed_count - len(extracted_records)
            skipped_count += page_skipped_count
            _enrich_product_records(driver, extracted_records)

            page_records = []
            for record in extracted_records:
                key = _record_key(record)
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
                page_records.append(record)

            page_inserted_count = insert_zying_product_info(page_records)
            inserted_count += page_inserted_count
            print(
                f"智赢产品第 {page_number}/{page_count} 页采集 "
                f"{len(page_records)} 条，跳过 {page_skipped_count} 条，"
                f"入库 {page_inserted_count} 条",
                flush=True,
            )
            if page_number >= page_count or not _go_to_next_page(driver, wait):
                break
    finally:
        # 只停止本次 ChromeDriver 连接，不关闭用户的 BitBrowser 窗口。
        chrome_service.stop()
        releaseBrowserLease(window_id)

    print(
        f"智赢产品采集完成，共 {len(records)} 条，跳过 {skipped_count} 条，"
        f"入库 {inserted_count} 条，"
        f"耗时 {int(time.time() - started_at)} 秒",
        flush=True,
    )
    return records


def check_yuanyou_title(number=None, window_id=DEFAULT_ZYING_WINDOW_ID):
    """保留旧函数名，兼容已有的手工调用方式。"""
    return collect_zying_products(number=number, window_id=window_id)


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
    parser.add_argument("--window-id", default=DEFAULT_ZYING_WINDOW_ID, help="BitBrowser 窗口 ID")
    args = parser.parse_args()
    page_count = (
        args.pages_option
        if args.pages_option is not None
        else args.pages
        if args.pages is not None
        else DEFAULT_ZYING_PAGE_COUNT
    )
    try:
        collect_zying_products(page_count, args.window_id, args.start_page)
    except (RuntimeError, ValueError) as exc:
        parser.exit(status=1, message=f"采集失败：{exc}\n")


if __name__ == "__main__":
    main()
