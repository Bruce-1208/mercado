import time

from playwright.listing_review import (
    DEFAULT_REVIEW_WINDOW_ID,
    ask_ai_for_suspected_infringements,
    collect_paginated_products,
)


def check_yuanyou_title(pages=10, window_id=DEFAULT_REVIEW_WINDOW_ID):
    start = int(time.time())
    rows = collect_paginated_products(
        window_id,
        "https://www.erpyuanyou.com/#/collects/list",
        pages=pages,
        title_selector=".product-title",
        id_selector=".id-link",
    )
    response = ask_ai_for_suspected_infringements(rows)
    print(response)
    print("检查采集列表总数量为:", len(rows))
    print("花费时间为:", int(time.time()) - start)
    return response


if __name__ == "__main__":
    check_yuanyou_title()
