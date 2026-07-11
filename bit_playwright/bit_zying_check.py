import time

from bit_playwright.listing_review import (
    DEFAULT_REVIEW_WINDOW_ID,
    ask_ai_for_suspected_infringements,
    collect_paginated_products,
    get_all_ids,
)


def check_yuanyou_title(number, window_id=DEFAULT_REVIEW_WINDOW_ID):
    start = int(time.time())
    rows = collect_paginated_products(
        window_id,
        "https://meli.zying.net/#/product",
        pages=int(number),
        title_selector=".f12.product-title, .product-title",
        id_selector=".id-link, .product-id, [class*=id]",
    )
    response = ask_ai_for_suspected_infringements(rows)
    print(response)
    print("检查采集列表总数量为:", len(rows))
    print("花费时间为:", int(time.time()) - start)
    return response


if __name__ == "__main__":
    check_yuanyou_title(1)
