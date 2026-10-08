"""1688 dimensions are read from scoped source data and remain attached to a SKU."""
import pytest

from test_zeshun_popup_browser import browser, supplier_page, supplier_extract

DIMENSION_KEYS = ("package_length_cm", "package_width_cm", "package_height_cm")


def dimensions(item):
    return [item.get(key) for key in DIMENSION_KEYS]


def set_pack(page, data):
    page.evaluate("""data => {
      document.querySelector('#productPackInfo').__reactFiber$dimensions = {
        memoizedProps: {packInfoData: data}
      };
    }""", data)


@pytest.mark.parametrize("metrics,expected", [
    ({"lengthCm": 30, "widthCm": 20, "heightCm": 10}, [30, 20, 10]),
    ({"lengthMm": 300, "widthMm": 200, "heightMm": 100}, [30, 20, 10]),
    ({"lengthCm": 30, "widthMm": 200, "heightCm": 10, "sizeUnit": "mm"}, [30, 20, 10]),
    ({"packageDimensions": "包装尺寸：300毫米 × 20厘米 × 0.1米"}, [30, 20, 10]),
    ({"packageSize": "长：30厘米 宽：20厘米 高：10厘米"}, [30, 20, 10]),
    ({"raw_dimensions": "30cmx20cmx10cm"}, [30, 20, 10]),
])
def test_dimension_only_bridge_rows_and_units(supplier_page, metrics, expected):
    set_pack(supplier_page, {"skuInfo": [{"skuId": "11", **metrics}]})
    detail = supplier_page.evaluate("""() => new Promise(resolve =>
      readSupplier({type:'AI_WEIGHT_PRICE_READ_DETAIL'},null,resolve))""")
    assert detail["ok"] is True
    small, large = detail["detail"]["skus"]
    assert dimensions(small) == expected
    assert small["raw_dimensions"]
    assert small["dimension_evidence_present"] is True
    assert dimensions(large) == [None, None, None]
    assert dimensions(detail["detail"]) == [None, None, None]


def test_common_package_object_applies_as_a_complete_tuple(supplier_page):
    set_pack(supplier_page, {"packageDimensions": "30*20*10厘米"})
    product = supplier_extract(supplier_page)["product"]
    assert dimensions(product) == [30, 20, 10]
    assert product["raw_dimensions"] == "30 × 20 × 10 cm"
    assert [dimensions(row) for row in product["variations"]] == [[30, 20, 10], [30, 20, 10]]


@pytest.mark.parametrize("specific", [
    {"lengthCm": 30},
    {"lengthCm": 30, "widthCm": "错误", "heightCm": 10},
    {"raw_dimensions": "尺寸：30 × 20 × 0厘米"},
    {"raw_dimensions": "尺寸：30 × 20厘米"},
    {"raw_dimensions": "30 × 20 × 10 cm；40 × 20 × 10 cm"},
])
def test_specific_partial_invalid_or_conflicting_size_blocks_common_fallback(supplier_page, specific):
    set_pack(supplier_page, {"skuInfo": [{"skuId": "11", **specific}],
                             "common": {"lengthCm": 40, "widthCm": 30, "heightCm": 20}})
    product = supplier_extract(supplier_page)["product"]
    small, large = product["variations"]
    assert dimensions(small) == [None, None, None]
    assert small["dimension_evidence_present"] is True
    assert small["raw_dimensions"]
    assert dimensions(large) == [40, 30, 20]
    assert dimensions(product) == [None, None, None]


def test_conflicting_arrays_do_not_silently_use_common_dimensions(supplier_page):
    set_pack(supplier_page, {
        "skuInfo": [{"skuId": "11", "lengthCm": 30, "widthCm": 20, "heightCm": 10}],
        "another": [{"skuId": "11", "lengthCm": 35, "widthCm": 20, "heightCm": 10}],
        "common": {"lengthCm": 40, "widthCm": 30, "heightCm": 20},
    })
    product = supplier_extract(supplier_page)["product"]
    assert dimensions(product["variations"][0]) == [None, None, None]
    assert product["variations"][0]["dimension_evidence_present"] is True


def test_packaging_table_binds_rows_to_sku_labels_without_borrowing(supplier_page):
    supplier_page.evaluate("""() => {
      document.querySelector('#productPackInfo').innerHTML = `<table><thead><tr>
        <th>颜色</th><th>尺寸</th><th>长(mm)</th><th>宽(cm)</th><th>高(厘米)</th>
        </tr></thead><tbody>
        <tr><td>蓝色</td><td>大号</td><td>300</td><td>20</td><td>10</td></tr>
        <tr><td>红色</td><td>小号</td><td>100</td><td>8</td><td>4</td></tr>
        <tr><td>其他</td><td>未售规格</td><td>990</td><td>99</td><td>99</td></tr>
        </tbody></table>`;
    }""")
    product = supplier_extract(supplier_page)["product"]
    assert [dimensions(row) for row in product["variations"]] == [[10, 8, 4], [30, 20, 10]]
    assert dimensions(product) == [None, None, None]


def test_dimensions_inline_in_sku_data_are_retained(supplier_page):
    supplier_page.evaluate("""() => {
      const sku = document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems[0];
      Object.assign(sku, {packageSize: '10 × 8 × 4 cm'});
    }""")
    product = supplier_extract(supplier_page)["product"]
    assert dimensions(product["variations"][0]) == [10, 8, 4]
    assert dimensions(product["variations"][1]) == [None, None, None]


def test_scoped_dimension_property_ignores_recommendation_page_text(supplier_page):
    supplier_page.evaluate("""() => {
      const attributes = document.createElement('div');
      attributes.id = 'productAttributes';
      attributes.innerHTML = '<ul><li><span>包装尺寸</span><span>长30cm 宽20cm 高10cm</span></li></ul>';
      document.body.append(attributes);
      document.body.insertAdjacentHTML('beforeend', '<p>推荐商品包装尺寸 99x99x99cm</p>');
    }""")
    product = supplier_extract(supplier_page)["product"]
    assert dimensions(product) == [30, 20, 10]


@pytest.mark.parametrize("raw,expected", [
    ("包装尺寸(mm)：300x200x100", [30, 20, 10]),
    ("包装尺寸：30x20x10-15cm", [None, None, None]),
    ("包装尺寸：30 - 40x20x10cm", [None, None, None]),
    ("包装尺寸：30x20x10cm+5cm", [None, None, None]),
    ("包装尺寸：10x30x20x10cm", [None, None, None]),
    ("包装尺寸：30x20x10英寸", [None, None, None]),
    ("包装尺寸(in)：30x20x10", [None, None, None]),
])
def test_dimension_prefix_units_and_ambiguous_tuples(supplier_page, raw, expected):
    set_pack(supplier_page, {"skuInfo": [{"skuId": "11", "raw_dimensions": raw}]})
    product = supplier_extract(supplier_page)["product"]
    row = product["variations"][0]
    assert dimensions(row) == expected
    assert row["dimension_evidence_present"] is True
    assert row["raw_dimensions"] == raw


def test_unsupported_dom_axis_units_are_not_assumed_to_be_cm(supplier_page):
    supplier_page.evaluate("""() => {
      document.querySelector('#productPackInfo').innerHTML = `<table><thead><tr>
        <th>SKU编号</th><th>长度(in)</th><th>宽度(in)</th><th>高度(in)</th>
        </tr></thead><tbody><tr><td>11</td><td>30</td><td>20</td><td>10</td></tr></tbody></table>`;
    }""")
    set_pack(supplier_page, {"common": {"lengthCm": 40, "widthCm": 30, "heightCm": 20}})
    product = supplier_extract(supplier_page)["product"]
    assert dimensions(product["variations"][0]) == [None, None, None]
    assert product["variations"][0]["dimension_evidence_present"] is True
    assert dimensions(product["variations"][1]) == [40, 30, 20]


def test_flattened_pack_module_does_not_promote_one_sku_to_common(supplier_page):
    set_pack(supplier_page, {"skuInfo": [{"skuId": "11", "lengthCm": 10, "widthCm": 8, "heightCm": 4}]})
    supplier_page.evaluate("""() => {
      document.querySelector('#productPackInfo').innerHTML = '<div>红色 小号 包装尺寸10x8x4cm</div>';
    }""")
    product = supplier_extract(supplier_page)["product"]
    assert dimensions(product["variations"][0]) == [10, 8, 4]
    assert dimensions(product["variations"][1]) == [None, None, None]


def test_all_zero_pack_placeholders_allow_verified_common_tuple(supplier_page):
    set_pack(supplier_page, {"skuInfo": [{"skuId": "11", "length": 0, "width": 0, "height": 0}],
                             "common": {"lengthCm": 40, "widthCm": 30, "heightCm": 20}})
    product = supplier_extract(supplier_page)["product"]
    assert [dimensions(row) for row in product["variations"]] == [[40, 30, 20], [40, 30, 20]]


def test_common_parent_object_does_not_hide_child_sku_dimensions(supplier_page):
    set_pack(supplier_page, {"lengthCm": 40, "widthCm": 30, "heightCm": 20,
                             "skuInfo": [{"skuId": "11", "lengthCm": 10, "widthCm": 8, "heightCm": 4}]})
    product = supplier_extract(supplier_page)["product"]
    assert [dimensions(row) for row in product["variations"]] == [[10, 8, 4], [40, 30, 20]]


def test_conflicting_inline_size_arrays_keep_prices_and_flag_evidence(supplier_page):
    supplier_page.evaluate("""() => {
      const props = document.querySelector('#skuSelection').__reactFiber$test.memoizedProps;
      props.skuItems[0].packageSize = '10x8x4 cm';
      props.otherSkuItems = structuredClone(props.skuItems);
      props.otherSkuItems[0].packageSize = '11x8x4 cm';
    }""")
    result = supplier_extract(supplier_page)
    assert result["ok"] is True
    assert result["product"]["variations"][0]["price"] == 12.5
    assert dimensions(result["product"]["variations"][0]) == [None, None, None]
    assert result["product"]["variations"][0]["dimension_evidence_present"] is True


def test_sku_dimension_label_followed_by_another_attribute_is_read(supplier_page):
    supplier_page.evaluate("""() => {
      document.querySelector('#skuSelection').__reactFiber$test.memoizedProps.skuItems[0].specAttrs =
        '尺寸:10x8x4厘米&gt;颜色:红色';
    }""")
    product = supplier_extract(supplier_page)["product"]
    assert dimensions(product["variations"][0]) == [10, 8, 4]
