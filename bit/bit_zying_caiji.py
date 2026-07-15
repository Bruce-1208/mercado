import argparse
import os
import re
import time
from datetime import datetime

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from bit.bit_api import openBrowser
from bit.bit_db_api import insert_zying_product_info


DEFAULT_ZYING_WINDOW_ID = os.environ.get(
    "BIT_ZYING_WINDOW_ID",
    "9812f185f7ab49d98f3988994d9e8ebf",
)
ZYING_PRODUCT_URL = os.environ.get(
    "BIT_ZYING_PRODUCT_URL",
    "https://meli.zying.net/#/product",
)

TITLE_SELECTOR = ".f12.product-title, .product-title"
IMAGE_SELECTOR = "img.product-pic, img[class*='product-pic'], img[class*='product-image']"

FIELD_DEFINITIONS = {
    "sale_price": {
        "labels": ("售价", "销售价", "销售价格", "Price", "Precio"),
        "selectors": (
            ".sale-price",
            ".selling-price",
            ".product-price",
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
    titles = driver.find_elements(By.CSS_SELECTOR, TITLE_SELECTOR)
    return "|".join(_clean_text(item.text) for item in titles[:3])


def _go_to_next_page(driver, wait):
    selectors = (
        "li[title='下一页']:not(.ant-pagination-disabled) button",
        "li.ant-pagination-next:not(.ant-pagination-disabled) button",
        "button[aria-label='Next']:not([disabled])",
        "button[aria-label='下一页']:not([disabled])",
    )
    old_signature = _page_signature(driver)
    old_page_elements = driver.find_elements(By.CSS_SELECTOR, ".ant-pagination-item-active")
    old_page = old_page_elements[0].get_attribute("title") if old_page_elements else ""

    next_button = None
    for selector in selectors:
        candidates = driver.find_elements(By.CSS_SELECTOR, selector)
        if candidates:
            next_button = candidates[0]
            break
    if next_button is None:
        return False

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", next_button)
    next_button.click()

    def page_changed(current_driver):
        active = current_driver.find_elements(By.CSS_SELECTOR, ".ant-pagination-item-active")
        current_page = active[0].get_attribute("title") if active else ""
        return (old_page and current_page and current_page != old_page) or (
            _page_signature(current_driver) != old_signature
        )

    try:
        wait.until(page_changed)
    except TimeoutException:
        # 部分页码组件不会更新 title；只要卡片仍能加载，下一轮继续采集。
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, TITLE_SELECTOR)))
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


def collect_zying_products(number=1, window_id=DEFAULT_ZYING_WINDOW_ID):
    """采集指定页数的智赢产品卡片，并批量写入 zying_product 表。"""
    page_count = max(1, int(number))
    started_at = time.time()
    browser_info = openBrowser(window_id)
    if not browser_info or not browser_info.get("data"):
        raise RuntimeError(f"打开 BitBrowser 窗口失败：{browser_info}")

    driver_path = browser_info["data"]["driver"]
    debugger_address = browser_info["data"]["http"]
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_experimental_option("debuggerAddress", debugger_address)
    chrome_service = Service(driver_path)
    driver = webdriver.Chrome(
        service=chrome_service,
        options=chrome_options,
    )
    wait = WebDriverWait(driver, 15)
    records = []
    seen = set()

    try:
        driver.get(ZYING_PRODUCT_URL)
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, TITLE_SELECTOR)))

        for page_number in range(1, page_count + 1):
            title_elements = wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, TITLE_SELECTOR))
            )
            page_records = []
            for title_element in title_elements:
                record = extract_product_record(driver, title_element, page_number)
                key = _record_key(record)
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
                page_records.append(record)

            print(f"智赢产品第 {page_number} 页采集 {len(page_records)} 条")
            if page_number >= page_count or not _go_to_next_page(driver, wait):
                break
    finally:
        # 只停止本次 ChromeDriver 连接，不关闭用户的 BitBrowser 窗口。
        chrome_service.stop()

    inserted_count = insert_zying_product_info(records)
    print(
        f"智赢产品采集完成，共 {len(records)} 条，入库 {inserted_count} 条，"
        f"耗时 {int(time.time() - started_at)} 秒"
    )
    return records


def check_yuanyou_title(number, window_id=DEFAULT_ZYING_WINDOW_ID):
    """保留旧函数名，兼容已有的手工调用方式。"""
    return collect_zying_products(number=number, window_id=window_id)


def get_all_ids(text):
    product_ids = re.findall(r"\b\d{9}\b", str(text or ""))
    return sorted(set(product_ids))


def main():
    parser = argparse.ArgumentParser(description="采集智赢产品数据并写入数据库")
    parser.add_argument("pages", nargs="?", type=int, default=1, help="采集页数")
    parser.add_argument("--window-id", default=DEFAULT_ZYING_WINDOW_ID, help="BitBrowser 窗口 ID")
    args = parser.parse_args()
    collect_zying_products(args.pages, args.window_id)


if __name__ == "__main__":
    main()
