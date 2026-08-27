import json

import pytest
from selenium.common.exceptions import TimeoutException

from bit import bit_mysql, bit_zying_caiji


class _FakeDriver:
    def __init__(self, *, url, title="", titles=None, login_elements=None):
        self.current_url = url
        self.title = title
        self._titles = titles or []
        self._login_elements = login_elements or []

    def find_elements(self, by, selector):
        if selector == bit_zying_caiji.TITLE_SELECTOR:
            return self._titles
        if selector == bit_zying_caiji.LOGIN_SELECTOR:
            return self._login_elements
        return []


class _OneShotWait:
    def __init__(self, driver):
        self.driver = driver

    def until(self, condition):
        return condition(self.driver)


class _TimeoutWait:
    def until(self, condition):
        raise TimeoutException()


class _AttributeElement:
    def __init__(self, value):
        self.value = value
        self.text = value

    def get_attribute(self, name):
        return self.value


class _DetailDriver:
    def find_elements(self, by, selector):
        if selector == bit_zying_caiji.TITLE_SELECTOR:
            return [object(), object()]
        if selector == ".curd-detail-wrap .crud-detail-header .h1":
            return [_AttributeElement("789264416")]
        if selector == ".curd-detail-wrap textarea[placeholder='请输入内容']":
            return [_AttributeElement("第二条")]
        return []


class _PageElement:
    def __init__(self, *, title="", text=""):
        self._title = title
        self.text = text

    def get_attribute(self, name):
        return self._title if name == "title" else ""


class _PaginationDriver:
    def __init__(self, active_element):
        self.active_element = active_element

    def find_elements(self, by, selector):
        if selector == ".ant-pagination-item-active":
            return [self.active_element]
        return []


def test_collector_uses_current_direct_mysql_writer():
    assert bit_zying_caiji.insert_zying_product_info is (
        bit_mysql.insert_zying_product_info
    )
    assert bit_zying_caiji.get_existing_zying_product_ids is (
        bit_mysql.get_existing_zying_product_ids
    )


def test_existing_product_ids_are_skipped_before_detail_collection():
    calls = []
    known_existing_ids = set()
    checked_product_ids = set()
    records = [
        {"product_id": "801623245", "title": "已入库"},
        {"product_id": "801623017", "title": "新商品"},
        {"product_id": "", "title": "待解析编号"},
    ]

    def read_existing(product_ids):
        calls.append(product_ids)
        return {"801623245"}

    filtered, skipped = bit_zying_caiji._skip_existing_zying_records(
        records,
        read_existing,
        known_existing_ids,
        checked_product_ids,
    )
    filtered_again, skipped_again = bit_zying_caiji._skip_existing_zying_records(
        filtered,
        read_existing,
        known_existing_ids,
        checked_product_ids,
    )

    assert skipped == 1
    assert skipped_again == 0
    assert [row["title"] for row in filtered_again] == ["新商品", "待解析编号"]
    assert calls == [["801623017", "801623245"]]


def test_product_id_deduplication_keeps_only_first_new_record():
    records = [
        {"product_id": "801623245", "title": "第一条"},
        {"product_id": "801623245", "title": "重复条"},
        {"product_id": "801623017", "title": "上页已采集"},
        {"product_id": "", "title": "编号未解析"},
    ]

    filtered, duplicate_count = bit_zying_caiji._deduplicate_zying_records(
        records,
        previously_seen={("id", "801623017")},
    )

    assert duplicate_count == 2
    assert [row["title"] for row in filtered] == ["第一条", "编号未解析"]


def test_persist_zying_page_writes_one_page_immediately(monkeypatch):
    calls = []
    page_records = [{"product_id": "801623245"}, {"product_id": "801623017"}]

    def insert_page(records):
        calls.append(list(records))
        return len(records)

    monkeypatch.setattr(bit_zying_caiji, "insert_zying_product_info", insert_page)

    inserted = bit_zying_caiji._persist_zying_page(page_records, 1, 60)

    assert inserted == 2
    assert calls == [page_records]


def test_persist_zying_page_mirrors_product_list_before_snapshot(monkeypatch):
    calls = []
    page_records = [{"product_id": "801623245"}]

    inserted = bit_zying_caiji._persist_zying_page(
        page_records,
        1,
        1,
        product_writer=lambda rows: calls.append("snapshot") or len(rows),
        product_mirror_writer=lambda rows: calls.append("product-list") or {"count": len(rows)},
    )

    assert inserted == 1
    assert calls == ["product-list", "snapshot"]


def test_wait_for_product_titles_returns_found_elements():
    titles = [object(), object()]
    driver = _FakeDriver(url="https://meli.zying.net/#/product", titles=titles)

    assert bit_zying_caiji._wait_for_product_titles(
        driver, _OneShotWait(driver)
    ) == titles


def test_active_page_number_reads_title_then_visible_text():
    assert bit_zying_caiji._active_page_number(
        _PaginationDriver(_PageElement(title="12", text="ignored"))
    ) == 12
    assert bit_zying_caiji._active_page_number(
        _PaginationDriver(_PageElement(text="3"))
    ) == 3


def test_wait_for_product_titles_reports_expired_login():
    driver = _FakeDriver(
        url="https://meli.zying.net/#/login",
        login_elements=[object()],
    )

    with pytest.raises(RuntimeError, match="登录状态已失效"):
        bit_zying_caiji._wait_for_product_titles(driver, _OneShotWait(driver))


def test_wait_for_product_titles_wraps_timeout_with_page_context():
    driver = _FakeDriver(
        url="https://meli.zying.net/#/product",
        title="智赢选品-美客多",
    )

    with pytest.raises(RuntimeError, match="产品列表加载超时") as error:
        bit_zying_caiji._wait_for_product_titles(driver, _TimeoutWait())

    assert isinstance(error.value.__cause__, TimeoutException)
    assert driver.current_url in str(error.value)


def test_select_search_result_matches_highlighted_title_and_image():
    record = {
        "title": "Mochila Azul",
        "main_image_url": "https://example.test/blue.jpg",
    }
    rows = [
        {
            "id": 10,
            "title": "<b>Mochila Azul</b>",
            "thumb": "https://example.test/other.jpg",
        },
        {
            "id": 11,
            "title": "<b>Mochila Azul</b>",
            "thumb": "https://example.test/blue.jpg",
        },
    ]

    assert bit_zying_caiji._select_search_result(record, rows)["id"] == 11


def test_merge_detail_record_populates_all_database_fields():
    record = {
        "title": "Mochila Azul",
        "main_image_url": "old-image",
        "sale_price": "",
    }
    detail = {
        "sale_id": 781267313,
        "sale_cur": "USD",
        "sale_cost": 214.66,
        "sale_netproceed": 152.26,
        "sale_weight": 1650,
        "sale_size": [66, 33, 11],
        "sale_stat": 1111,
        "sale_pic": ["https://example.test/new-image.jpg"],
        "sale_siteid": 8,
        "sale_attrs": '{"8":{"site":"CBT","kindid":"430974"}}',
    }

    result = bit_zying_caiji._merge_detail_record(record, {}, detail)

    assert result["product_id"] == "781267313"
    assert result["sale_price"] == "USD 214.66"
    assert result["net_income"] == "USD 152.26"
    assert result["package_gross_weight"] == "1650 克"
    assert result["package_dimensions"] == "66 X 33 X 11 厘米"
    assert result["review_status"] == "通过"
    assert result["main_image_url"] == "https://example.test/new-image.jpg"
    assert result["_category_site"] == "CBT"
    assert result["_category_id"] == "430974"


def test_zying_detail_builds_publish_ready_snapshot_with_all_common_fields():
    record = {
        "product_id": "795184904",
        "title": "List title",
        "main_image_url": "https://example.test/list.jpg",
        "sale_price": "USD 36.55",
        "net_income": "USD 22",
        "collected_at": "2026-08-27 12:00:00",
        "zying_category_id": "202170568",
        "zying_category": "圆佑同步/家电类",
    }
    detail = {
        "sale_id": 795184904,
        "sale_title": '{"en":"Detailed English title"}',
        "sale_description": '{"en":"Detailed description"}',
        "sale_cur": "USD",
        "sale_cost": 36.55,
        "sale_netproceed": 22,
        "sale_weight": 1000,
        "sale_size": [23, 22, 13],
        "sale_pic": ["https://example.test/one.jpg", "https://example.test/two.jpg"],
        "sale_siteid": 8,
        "sale_attrs": json.dumps(
            {
                "8": {
                    "site": "CBT",
                    "kindid": "430974",
                    "attributes": [
                        {"id": "BRAND", "value_name": "Generic"},
                        {"id": "MODEL", "value_name": "M-9"},
                    ],
                }
            }
        ),
        "sale_variations": [
            {"attribute_combinations": [{"id": "COLOR", "value_name": "Blue"}]}
        ],
        "sale_terms": [{"id": "WARRANTY_TYPE", "value_name": "Seller warranty"}],
    }

    bit_zying_caiji._merge_detail_record(record, {}, detail)
    bit_zying_caiji._merge_category_record(
        record,
        {"cate_cateid": "CBT430974", "cate_fullname": "Home / Test"},
    )
    result = bit_zying_caiji._finalize_zying_listing_snapshot(record)
    snapshot = result["listing_snapshot"]

    assert snapshot["source"]["id"] == "CBT795184904"
    assert snapshot["source"]["title"] == "Detailed English title"
    assert snapshot["source"]["category_id"] == "CBT430974"
    assert snapshot["description"]["plain_text"] == "Detailed description"
    assert [row["id"] for row in snapshot["source"]["attributes"]] == [
        "BRAND",
        "MODEL",
    ]
    assert len(snapshot["source"]["pictures"]) == 2
    assert snapshot["source"]["variations"] == detail["sale_variations"]
    assert snapshot["source"]["sale_terms"] == detail["sale_terms"]
    assert snapshot["page_snapshot"]["zying_detail"]["sale_id"] == 795184904


def test_merge_detail_record_keeps_values_read_from_clicked_product():
    record = {
        "product_id": "794001290",
        "sale_price": "$ 25.31",
        "net_income": "$ 14.94",
        "package_gross_weight": "240 克",
        "package_dimensions": "9 X 5 X 11 厘米",
        "review_status": "通过",
    }
    detail = {
        "sale_id": 794000011,
        "sale_cur": "$",
        "sale_cost": 1,
        "sale_netproceed": 2,
        "sale_weight": 3,
        "sale_size": [4, 5, 6],
        "sale_stat": 3000,
        "sale_siteid": 8,
        "sale_attrs": '{"8":{"site":"CBT","kindid":"105405"}}',
    }

    result = bit_zying_caiji._merge_detail_record(record, {}, detail)

    assert result["product_id"] == "794001290"
    assert result["sale_price"] == "$ 25.31"
    assert result["net_income"] == "$ 14.94"
    assert result["package_gross_weight"] == "240 克"
    assert result["package_dimensions"] == "9 X 5 X 11 厘米"
    assert result["review_status"] == "通过"
    assert result["_category_id"] == "105405"


def test_merge_category_record_stores_id_and_bilingual_path():
    record = {
        "product_id": "781267313",
        "raw_text": "卡片内容",
        "_category_site": "CBT",
        "_category_id": "430974",
    }
    category = {
        "cate_cateid": "CBT430974",
        "cate_fullname": "Souvenirs / Party Novelties / Crowns",
        "cate_fullzh": "纪念品 / 派对新奇物品 / 皇冠",
    }

    result = bit_zying_caiji._merge_category_record(record, category)

    assert result["product_category_id"] == "CBT430974"
    assert result["product_category"] == (
        "Souvenirs / Party Novelties / Crowns | 纪念品 / 派对新奇物品 / 皇冠"
    )
    assert "分类编号: CBT430974" in result["raw_text"]
    assert "产品分类:" in result["raw_text"]
    assert "_category_site" not in result
    assert "_category_id" not in result


def _zying_category_options():
    return [
        {
            "value": 202170501,
            "label": "圆佑同步",
            "children": [
                {"value": 202170531, "label": "游戏类", "children": []},
                {"value": 202170568, "label": "家电类", "children": []},
                {
                    "value": 202170507,
                    "label": "玩具/玩偶/娃娃",
                    "children": [],
                },
            ],
        },
        {
            "value": 202170575,
            "label": "武汉泽顺跟卖",
            "children": [
                {"value": 202170576, "label": "墨西哥", "children": []},
            ],
        },
        {
            "value": 202170583,
            "label": "刘德智采集",
            "children": [
                {"value": 202170584, "label": "墨西哥", "children": []},
            ],
        },
    ]


def test_zying_category_resolver_accepts_id_unique_name_and_full_path():
    options = _zying_category_options()

    by_id = bit_zying_caiji._resolve_zying_category(options, "202170568")
    by_name = bit_zying_caiji._resolve_zying_category(options, "家电类")
    by_path = bit_zying_caiji._resolve_zying_category(
        options,
        "圆佑同步/家电类",
    )

    assert by_id == by_name == by_path
    assert by_id["category_id"] == "202170568"
    assert by_id["category_path"] == "圆佑同步/家电类"
    assert by_id["path_values"] == [202170501, 202170568]
    assert bit_zying_caiji._resolve_zying_category(
        options,
        "玩具/玩偶/娃娃",
    )["category_id"] == "202170507"


def test_zying_category_resolver_rejects_ambiguous_leaf_name():
    with pytest.raises(RuntimeError, match="不唯一") as error:
        bit_zying_caiji._resolve_zying_category(
            _zying_category_options(),
            "墨西哥",
        )

    assert "武汉泽顺跟卖/墨西哥" in str(error.value)
    assert "刘德智采集/墨西哥" in str(error.value)


def test_attach_zying_category_keeps_it_separate_from_mercado_category():
    record = {
        "product_category_id": "CBT430974",
        "product_category": "Patio Heaters | 露台加热器",
        "raw_text": "卡片内容",
    }
    selection = bit_zying_caiji._resolve_zying_category(
        _zying_category_options(),
        "圆佑同步/家电类",
    )

    result = bit_zying_caiji._attach_zying_category(record, selection)

    assert result["zying_category_id"] == "202170568"
    assert result["zying_category"] == "圆佑同步/家电类"
    assert result["product_category_id"] == "CBT430974"
    assert result["product_category"] == "Patio Heaters | 露台加热器"
    assert "智赢产品分类: 圆佑同步/家电类" in result["raw_text"]


def test_apply_zying_category_sets_cascader_and_clicks_search(monkeypatch):
    events = []

    class Button:
        text = "搜索"

        def get_attribute(self, name):
            return "搜索" if name == "textContent" else ""

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def click(self):
            events.append("search")

    class Driver:
        def execute_script(self, script, *args):
            if script == bit_zying_caiji.ZYING_CATEGORY_OPTIONS_SCRIPT:
                return _zying_category_options()
            if script == bit_zying_caiji.ZYING_SET_CATEGORY_SCRIPT:
                events.append(("category", args[0]))
                return True
            if "ant-select-selection-item" in script:
                return "圆佑同步 / 家电类"
            return None

        def find_elements(self, by, selector):
            if selector == "button":
                return [Button()]
            if selector == bit_zying_caiji.TITLE_SELECTOR:
                return [object()]
            return []

    class Wait:
        def until(self, condition):
            return condition(driver)

    driver = Driver()
    signatures = iter(["before", "after"])
    monkeypatch.setattr(
        bit_zying_caiji,
        "_page_signature",
        lambda value: next(signatures),
    )

    selection = bit_zying_caiji._apply_zying_category_filter(
        driver,
        Wait(),
        "圆佑同步/家电类",
    )

    assert selection["category_id"] == "202170568"
    assert events == [("category", [202170501, 202170568]), "search"]


def test_mysql_writer_stores_zying_and_mercado_categories_separately(monkeypatch):
    captured = {}

    class Cursor:
        lastrowid = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            return None

        def executemany(self, sql, rows):
            captured["sql"] = sql
            captured["rows"] = rows

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            captured["committed"] = True

        def rollback(self):
            captured["rolled_back"] = True

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, "_ensure_zying_product_table", lambda cursor: None)

    count = bit_mysql.insert_zying_product_info(
        [
            {
                "product_id": "795184904",
                "zying_category_id": "202170568",
                "zying_category": "圆佑同步/家电类",
                "product_category_id": "CBT430974",
                "product_category": "Appliances | 家用电器",
                "title": "测试商品",
            }
        ]
    )

    row = captured["rows"][0]
    assert count == 1
    assert len(row) == 17
    assert row[:5] == (
        "795184904",
        "202170568",
        "圆佑同步/家电类",
        "CBT430974",
        "Appliances | 家用电器",
    )
    assert "`智赢分类编号`" in captured["sql"]
    assert "`智赢产品分类`" in captured["sql"]
    assert "`上架快照`" in captured["sql"]


def test_open_collection_browser_can_attach_local_edge_without_bitbrowser(monkeypatch):
    stopped = []

    class Service:
        def stop(self):
            stopped.append(True)

    class Driver:
        service = Service()

    monkeypatch.setattr(bit_zying_caiji.webdriver, "Edge", lambda **kwargs: Driver())
    monkeypatch.setattr(
        bit_zying_caiji,
        "openBrowser",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("BitBrowser should not open")),
    )

    driver, service, lease_id = bit_zying_caiji._open_zying_collection_browser(
        "edge",
        "unused",
        edge_debugger_address="http://127.0.0.1:9222/",
    )

    assert isinstance(driver, Driver)
    assert service is driver.service
    assert lease_id == ""


def test_mysql_existing_product_reader_returns_only_found_ids(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return [{"产品编号": "801623245"}]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(bit_mysql.pymysql, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(bit_mysql, "_ensure_zying_product_table", lambda cursor: None)

    result = bit_mysql.get_existing_zying_product_ids(
        ["801623245", "801623017", "801623245", ""],
    )

    assert result == {"801623245"}
    assert "SELECT DISTINCT `产品编号`" in captured["sql"]
    assert captured["params"] == ("801623245", "801623017")
    assert captured["closed"] is True


def test_merge_ui_detail_record_overwrites_api_values_with_clicked_form():
    record = {
        "product_id": "old-id",
        "sale_price": "USD 1",
        "net_income": "USD 2",
        "package_gross_weight": "3 克",
        "package_dimensions": "4 X 5 X 6 厘米",
        "review_status": "待审核",
        "raw_text": "卡片内容",
    }
    details = {
        "product_id": "795184904",
        "sale_price": "36.55",
        "net_income": "22",
        "package_gross_weight": "1000",
        "size_length": "23",
        "size_width": "23",
        "size_height": "13",
        "review_status": "通过",
    }

    result = bit_zying_caiji._merge_ui_detail_record(record, details)

    assert result["product_id"] == "795184904"
    assert result["sale_price"] == "USD 36.55"
    assert result["net_income"] == "USD 22"
    assert result["package_gross_weight"] == "1000 克"
    assert result["package_dimensions"] == "23 X 23 X 13 厘米"
    assert result["review_status"] == "通过"
    assert "详情产品编号: 795184904" in result["raw_text"]


def test_detail_identity_accepts_matching_id_when_title_is_translated():
    assert (
        "if (!productIdMatches && !titleMatches && !imageMatches) return null;"
        in bit_zying_caiji.DETAIL_FORM_SCRIPT
    )
    assert "const expectedProductId = normalize(arguments[2]);" in (
        bit_zying_caiji.DETAIL_FORM_SCRIPT
    )


def test_wait_for_clicked_detail_passes_expected_product_id(monkeypatch):
    calls = []
    details = {
        "product_id": "801623245",
        "sale_price": "10",
        "net_income": "8",
        "package_gross_weight": "100",
        "size_length": "1",
        "size_width": "2",
        "size_height": "3",
        "review_status": "通过",
    }

    class Driver:
        def execute_script(self, script, *args):
            calls.append(args)
            return {"ready": True, "details": details}

    class ImmediateWait:
        def __init__(self, driver, timeout, poll_frequency):
            self.driver = driver

        def until(self, condition):
            return condition(self.driver)

    monkeypatch.setattr(bit_zying_caiji, "WebDriverWait", ImmediateWait)

    result = bit_zying_caiji._wait_for_clicked_detail(
        Driver(),
        "English listing title",
        "https://example.test/old.jpg",
        "801623245",
    )

    assert result == details
    assert calls == [
        (
            "English listing title",
            "https://example.test/old.jpg",
            "801623245",
        )
    ]


def test_clicked_details_skips_one_timeout_and_returns_successes(monkeypatch):
    details = {
        "product_id": "111111111",
        "sale_price": "10",
        "net_income": "8",
        "package_gross_weight": "100",
        "size_length": "1",
        "size_width": "2",
        "size_height": "3",
        "review_status": "通过",
    }
    outcomes = iter(
        [
            details,
            TimeoutException(),
            TimeoutException(),
            TimeoutException(),
        ]
    )

    def sequenced_detail_wait(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        bit_zying_caiji,
        "_wait_for_clicked_detail",
        sequenced_detail_wait,
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_find_record_title_element",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_find_product_card",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_click_product_card",
        lambda *args, **kwargs: None,
    )
    records = [
        {
            "title": "第一条",
            "main_image_url": "https://example.test/1.jpg",
            "sale_price": "USD 1",
            "raw_text": "",
        },
        {
            "title": "第二条",
            "main_image_url": "https://example.test/2.jpg",
            "sale_price": "USD 2",
            "raw_text": "",
        },
    ]

    completed = bit_zying_caiji._collect_clicked_product_details(
        _DetailDriver(),
        records,
        page_number=1,
        page_count=1,
    )

    assert completed == [records[0]]
    assert completed[0]["product_id"] == "111111111"
    assert "product_id" not in records[1]


def test_clicked_details_switches_click_strategy_after_detail_does_not_open(
    monkeypatch,
):
    details = {
        "product_id": "222222222",
        "sale_price": "20",
        "net_income": "16",
        "package_gross_weight": "200",
        "size_length": "2",
        "size_width": "3",
        "size_height": "4",
        "review_status": "通过",
    }
    outcomes = iter([TimeoutException(), details])
    click_attempts = []

    def sequenced_detail_wait(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        bit_zying_caiji,
        "_wait_for_clicked_detail",
        sequenced_detail_wait,
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_find_record_title_element",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_find_product_card",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_click_product_card",
        lambda driver, card, attempt=0: click_attempts.append(attempt)
        or ("卡片", "标题/链接", "指针事件")[attempt],
    )
    records = [
        {
            "title": "备用点击商品",
            "main_image_url": "https://example.test/retry.jpg",
            "sale_price": "USD 2",
            "raw_text": "",
        }
    ]

    completed = bit_zying_caiji._collect_clicked_product_details(
        _DetailDriver(),
        records,
        page_number=1,
        page_count=1,
    )

    assert click_attempts == [0, 1]
    assert completed == records
    assert completed[0]["product_id"] == "222222222"


def test_clicked_details_uses_api_fallback_without_retrying_loaded_detail(
    monkeypatch,
):
    click_attempts = []
    partial_details = {
        "product_id": "801623017",
        "sale_price": "",
        "net_income": "",
        "package_gross_weight": "",
        "size_length": "",
        "size_width": "",
        "size_height": "",
        "review_status": "",
    }
    monkeypatch.setattr(
        bit_zying_caiji,
        "_wait_for_clicked_detail",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            bit_zying_caiji._DetailFieldsTimeout(partial_details)
        ),
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_find_record_title_element",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_find_product_card",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        bit_zying_caiji,
        "_click_product_card",
        lambda driver, card, attempt=0: click_attempts.append(attempt) or "卡片",
    )
    records = [
        {
            "product_id": "801623017",
            "title": "English listing title",
            "main_image_url": "https://example.test/item.jpg",
            "sale_price": "USD 2",
            "raw_text": "",
        }
    ]

    completed = bit_zying_caiji._collect_clicked_product_details(
        _DetailDriver(),
        records,
        page_number=1,
        page_count=1,
    )

    assert click_attempts == [0]
    assert completed == records
