import pymysql
import re
from bs4 import BeautifulSoup

from bit.bit_mysql import config
from bit.bit_source_main_images import build_direct_session


connection = pymysql.connect(**config)
try:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT product_id, source_url, main_image_fetch_status,
                   main_image_fetch_method, main_image_final_url,
                   LEFT(main_image_error, 300)
            FROM zying_desktop_products
            WHERE export_category=%s AND source_batch=%s
              AND main_image_fetch_status IS NOT NULL
            ORDER BY product_id
            LIMIT 30
            """,
            ("全部分类", "zying_boutique_17074_20260812_run3"),
        )
        for row in cursor.fetchall():
            print(row)
        cursor.execute(
            """
            SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(source_url, '/', 3), '/', -1),
                   COUNT(*)
            FROM zying_desktop_products
            WHERE export_category=%s AND source_batch=%s
            GROUP BY 1 ORDER BY 2 DESC
            """,
            ("全部分类", "zying_boutique_17074_20260812_run3"),
        )
        print("DOMAINS")
        for row in cursor.fetchall():
            print(row)
finally:
    connection.close()

session = build_direct_session()
samples = [
    "https://detail.1688.com/offer/1020342307575.html",
    "https://articulo.mercadolibre.com.mx/MLM-3098000885",
]
for url in samples:
    response = session.get(url, timeout=30, allow_redirects=True)
    soup = BeautifulSoup(response.text, "html.parser")
    print("SAMPLE", url)
    print("HTTP", response.status_code, response.url, len(response.content))
    print("HEADERS", dict(response.headers))
    print("BODY", response.text[:3000])
    print("TITLE", soup.title.get_text(" ", strip=True) if soup.title else None)
    for pattern in (
        r'https?:\\?/\\?/[^"\'<> ]+?\.(?:jpg|jpeg|png|webp)[^"\'<> ]*',
        r'(?i)(?:mainImage|imageUrl|picUrl|offerImg)[^\n]{0,300}',
    ):
        matches = re.findall(pattern, response.text)
        print("MATCHES", pattern[:30], matches[:10])
