import re
import time

from AI_Agent.deepseek import get_ai_response
from bit_playwright.common import BitPlaywrightSession


DEFAULT_REVIEW_WINDOW_ID = "1495e31cb630406bb690ba187f264fe7"


def get_all_ids(text):
    return re.findall(r"\d+", text or "")


def collect_product_rows(page, title_selector=".product-title", id_selector=".id-link"):
    titles = [item.strip() for item in page.locator(title_selector).all_inner_texts() if item.strip()]
    ids = [item.strip() for item in page.locator(id_selector).all_inner_texts() if item.strip()]
    return list(zip(titles, ids))


def click_next_page(page, timeout=5000):
    next_buttons = [
        'li[title="下一页"]:not(.ant-pagination-disabled) button',
        'li[title="下一页"]:not(.ant-pagination-disabled)',
        ".ant-pagination-next:not(.ant-pagination-disabled) button",
        ".andes-pagination__button--next:not([disabled])",
    ]
    for selector in next_buttons:
        locator = page.locator(selector).first
        try:
            locator.click(timeout=timeout)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            continue
    return False


def collect_paginated_products(
    window_id,
    url,
    pages=10,
    title_selector=".product-title",
    id_selector=".id-link",
):
    rows = []
    with BitPlaywrightSession(window_id) as session:
        page = session.page
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        for _ in range(int(pages)):
            rows.extend(collect_product_rows(page, title_selector, id_selector))
            if not click_next_page(page):
                break
            time.sleep(2)
    return rows


def ask_ai_for_suspected_infringements(rows):
    line = "\n".join(str(row) for row in rows)
    prompt = (
        line
        + "\n这组数据每一行是产品标题和产品编号，韩国品牌IP、日本动漫IP一般不为侵权，"
        + "帮我找出所有疑似侵权的产品，只返回编号。"
    )
    return get_ai_response(prompt)
