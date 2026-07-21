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


def test_detail_identity_accepts_matching_title_when_image_changed():
    assert "if (!titleMatches && !imageMatches) return null;" in (
        bit_zying_caiji.DETAIL_FORM_SCRIPT
    )


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
    outcomes = iter([details, TimeoutException(), TimeoutException()])

    class _SequencedWait:
        def __init__(self, *args, **kwargs):
            pass

        def until(self, condition):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(bit_zying_caiji, "WebDriverWait", _SequencedWait)
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
