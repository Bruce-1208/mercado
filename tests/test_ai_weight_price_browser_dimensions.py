"""Offline 1688 package-size extraction and SKU ownership regressions."""
import os
from pathlib import Path

import pytest

from erp.ai_weight_price.browser import CURRENT_1688_DETAIL
from erp.ai_weight_price.models import supplier_dimensions_evidence


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        executable = os.environ.get("AWP_TEST_BROWSER_EXECUTABLE") or pw.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Install a Playwright browser or set AWP_TEST_BROWSER_EXECUTABLE")
        instance = pw.chromium.launch(executable_path=executable, headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    context.route("**/*", lambda route: route.abort())
    tab = context.new_page()
    tab.set_content('''<meta charset="utf-8">
      <div id="productTitle" data-module="od_title"><div class="title-content"><h1>测试收纳盒</h1></div></div>
      <div id="shopNavigation"><a class="shop-company-name" href="https://shop123.1688.com">测试工厂</a></div>
      <div id="gallery"><img class="preview-img" src="https://img.example/main.jpg"></div>
      <div id="skuSelection" data-module="od_sku_selection"></div>
      <div id="productPackInfo" data-module="od_product_pack_info"></div>
      <aside>推荐商品 包装尺寸 99x99x99cm</aside>''')
    tab.evaluate('''() => {
      document.querySelector('#skuSelection').__reactFiber$fixture={memoizedProps:{skuItems:[
        {skuId:11,specAttrs:'红色&gt;小号',price:12.50},
        {skuId:22,specAttrs:'蓝色&gt;大号',price:18}
      ]}};
    }''')
    try:
        yield tab
    finally:
        context.close()


def extract(page, *, rows=None, html=""):
    page.evaluate('''({rows,html}) => {
      const pack=document.querySelector('#productPackInfo');pack.innerHTML=html;
      if(rows)pack.__reactFiber$fixture={memoizedProps:{packInfoData:{skuInfo:rows}}};
    }''', {"rows": rows, "html": html})
    result = page.evaluate(CURRENT_1688_DETAIL, "")
    assert result["ready"] is True
    return result["offer"]


def test_structured_dimensions_preserve_each_axis_unit_and_sku_id(page):
    offer = extract(page, rows=[
        {"skuId": 22, "length": 300, "width": 200, "height": 100, "sizeUnit": "mm"},
        {"skuId": 11, "lengthMm": 100, "widthCm": 8, "height": "0.04m"},
    ])
    small, large = offer["skus"]
    assert supplier_dimensions_evidence(small, offer) == "10x8x4"
    assert supplier_dimensions_evidence(large, offer) == "30x20x10"
    assert small["raw_dimensions"] == "长度 100 mm；宽度 8 cm；高度 0.04m"
    assert large["raw_dimensions"] == "长度 300 mm；宽度 200 mm；高度 100 mm"
    assert "package_length_cm" not in offer


def test_common_structured_package_stays_on_offer(page):
    offer = extract(page, rows=[{"length": 30, "width": 20, "height": 10}])
    assert offer["package_length_cm"] == 30
    assert all(sku["raw_dimensions"] == "" for sku in offer["skus"])
    assert all("package_length_cm" not in sku for sku in offer["skus"])
    assert supplier_dimensions_evidence(offer["skus"][0], offer) == "30x20x10"


def test_standalone_common_package_object_stays_on_offer(page):
    page.evaluate('''() => {
      document.querySelector('#productPackInfo').__reactFiber$fixture={memoizedProps:{
        packInfoData:{lengthCm:30,widthCm:20,heightCm:10}
      }};
    }''')
    offer = extract(page)
    assert offer["package_length_cm"] == 30
    assert all(supplier_dimensions_evidence(sku, offer) == "30x20x10" for sku in offer["skus"])


def test_standalone_sku_package_object_keeps_sku_identity(page):
    page.evaluate('''() => {
      document.querySelector('#productPackInfo').__reactFiber$fixture={memoizedProps:{
        packInfoData:{skuId:22,lengthCm:30,widthCm:20,heightCm:10}
      }};
    }''')
    offer = extract(page)
    assert "raw_dimensions" not in offer
    assert supplier_dimensions_evidence(offer["skus"][0], offer) is None
    assert supplier_dimensions_evidence(offer["skus"][1], offer) == "30x20x10"


def test_common_package_object_does_not_hide_nested_sku_array(page):
    page.evaluate('''() => {
      document.querySelector('#productPackInfo').__reactFiber$fixture={memoizedProps:{
        packInfoData:{lengthCm:30,widthCm:20,heightCm:10,
          skuInfo:[{skuId:11,lengthCm:10,widthCm:8,heightCm:4}]},
        style:{width:99,height:99}
      },return:{memoizedProps:{recommendation:{lengthCm:99,widthCm:99,heightCm:99}}}};
    }''')
    offer = extract(page)
    assert supplier_dimensions_evidence(offer["skus"][0], offer) == "10x8x4"
    assert supplier_dimensions_evidence(offer["skus"][1], offer) == "30x20x10"


def test_rendered_package_table_matches_skus_by_id(page):
    offer = extract(page, html='''<table><thead><tr><th>规格</th><th>长度(mm)</th><th>宽度(mm)</th><th>高度(mm)</th><th>重量(g)</th></tr></thead>
      <tbody><tr data-sku-id="22"><td>大号</td><td>300</td><td>200</td><td>100</td><td>600</td></tr>
      <tr data-sku-id="11"><td>小号</td><td>100</td><td>80</td><td>40</td><td>250</td></tr></tbody></table>''')
    assert [supplier_dimensions_evidence(sku, offer) for sku in offer["skus"]] == ["10x8x4", "30x20x10"]
    assert "raw_dimensions" not in offer


def test_rendered_package_table_requires_exact_unique_variant_label(page):
    offer = extract(page, html='''<table><tr><th>颜色</th><th>规格</th><th>长(cm)</th><th>宽(cm)</th><th>高(cm)</th></tr>
      <tr><td>蓝色</td><td>大号</td><td>30</td><td>20</td><td>10</td></tr>
      <tr><td>红色</td><td>小号</td><td>10</td><td>8</td><td>4</td></tr>
      <tr><td>不相关</td><td>其他</td><td>99</td><td>99</td><td>99</td></tr></table>''')
    assert [supplier_dimensions_evidence(sku, offer) for sku in offer["skus"]] == ["10x8x4", "30x20x10"]
    assert "raw_dimensions" not in offer


def test_single_common_rendered_size_inherits_header_unit(page):
    offer = extract(page, html='''<table><tr><th>包装尺寸（毫米）</th><th>重量(g)</th></tr>
      <tr><td>300 × 200 × 100</td><td>600</td></tr></table>''')
    assert offer["raw_dimensions"] == "包装尺寸（毫米）：300 × 200 × 100"
    assert supplier_dimensions_evidence(offer["skus"][0], offer) == "30x20x10"


def test_unknown_packaging_row_cannot_become_common_size(page):
    offer = extract(page, html='''<table><tr><th>规格</th><th>长(cm)</th><th>宽(cm)</th><th>高(cm)</th></tr>
      <tr data-sku-id="999"><td>推荐商品</td><td>99</td><td>99</td><td>99</td></tr></table>''')
    assert "raw_dimensions" not in offer
    assert all(supplier_dimensions_evidence(sku, offer) is None for sku in offer["skus"])


def test_no_table_sku_text_cannot_leak_size_to_other_variants(page):
    offer = extract(page, rows=[{"skuId": 11, "length": 20, "width": 10, "height": 5}],
                    html="规格 小号 包装尺寸20x10x5cm")
    assert supplier_dimensions_evidence(offer["skus"][0], offer) == "20x10x5"
    assert "raw_dimensions" not in offer
    assert supplier_dimensions_evidence(offer["skus"][1], offer) is None


def test_partial_sku_package_blocks_complete_common_fallback(page):
    page.evaluate('''() => {
      document.querySelector('#productPackInfo').__reactFiber$fixture={memoizedProps:{
        skuInfo:[{skuId:11,length:10,width:0,height:0}],commonPackage:[{length:30,width:20,height:10}]
      }};
    }''')
    offer = extract(page)
    assert offer["skus"][0]["dimension_evidence_present"] is True
    assert supplier_dimensions_evidence(offer["skus"][0], offer) is None
    assert supplier_dimensions_evidence(offer["skus"][1], offer) == "30x20x10"


def test_conflicting_rendered_and_structured_size_is_rejected(page):
    offer = extract(page, rows=[{"skuId": 11, "length": 11, "width": 8, "height": 4}], html='''
      <table><tr><th>长(cm)</th><th>宽(cm)</th><th>高(cm)</th></tr>
      <tr data-sku-id="11"><td>10</td><td>8</td><td>4</td></tr></table>''')
    assert offer["skus"][0]["raw_dimensions"] == "包装尺寸信息冲突"
    assert supplier_dimensions_evidence(offer["skus"][0], offer) is None


def test_conflicting_table_axes_and_size_cell_is_rejected(page):
    offer = extract(page, html='''<table><tr><th>长(cm)</th><th>宽(cm)</th><th>高(cm)</th><th>包装尺寸</th></tr>
      <tr data-sku-id="11"><td>30</td><td>20</td><td>10</td><td>40x20x10cm</td></tr></table>''')
    assert offer["skus"][0]["raw_dimensions"] == "包装尺寸信息冲突"
    assert supplier_dimensions_evidence(offer["skus"][0], offer) is None


def test_conflicting_common_rendered_and_structured_size_is_rejected(page):
    offer = extract(page, rows=[{"length": 30, "width": 20, "height": 10}],
                    html="包装尺寸 40x20x10cm")
    assert offer["raw_dimensions"] == "包装尺寸信息冲突"
    assert supplier_dimensions_evidence(offer["skus"][0], offer) is None


def test_unknown_table_unit_blocks_common_fallback(page):
    offer = extract(page, rows=[{"package_length_cm": 30, "package_width_cm": 20, "package_height_cm": 10}],
                    html='''<table><tr><th>length(in)</th><th>width(in)</th><th>height(in)</th></tr>
                    <tr data-sku-id="11"><td>12</td><td>8</td><td>4</td></tr></table>''')
    assert "package_length_cm" not in offer["skus"][0]
    assert offer["skus"][0]["raw_dimensions"] == "长度 12 in；宽度 8 in；高度 4 in"
    assert supplier_dimensions_evidence(offer["skus"][0], offer) is None
    assert supplier_dimensions_evidence(offer["skus"][1], offer) == "30x20x10"


def test_empty_placeholders_allow_common_package_but_two_sides_do_not(page):
    page.evaluate('''() => {
      document.querySelector('#skuSelection').__reactFiber$fixture.memoizedProps.skuItems[1].specAttrs='蓝色 84*56cm';
      document.querySelector('#productPackInfo').__reactFiber$fixture={memoizedProps:{
        skuInfo:[{skuId:11,length:0,width:'0.0',height:'0cm'}],common:[{length:30,width:20,height:10}]
      }};
    }''')
    offer = extract(page)
    assert supplier_dimensions_evidence(offer["skus"][0], offer) == "30x20x10"
    assert supplier_dimensions_evidence(offer["skus"][1], offer) is None
    assert offer["skus"][1]["raw_dimensions"] == "蓝色 84*56cm"


def test_package_reader_ignores_ancestor_recommendation_dimensions(page):
    page.evaluate('''() => {
      document.querySelector('#productPackInfo').__reactFiber$fixture={memoizedProps:{skuInfo:[
        {skuId:11,length:10,width:8,height:4}
      ]},return:{memoizedProps:{recommendations:[{skuId:11,length:99,width:99,height:99}]}}};
    }''')
    offer = extract(page)
    assert supplier_dimensions_evidence(offer["skus"][0], offer) == "10x8x4"
