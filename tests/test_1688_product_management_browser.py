"""Exercise the actual 1688 product section offline, without importing the app."""

import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _product_section_html():
    template = (ROOT / "bit/templates/index.html").read_text(encoding="utf-8")
    start = template.index('<div class="tab-page products-1688-page"')
    end = template.index('<div class="tab-page ai-original-page"', start)
    section = template[start:end].replace(
        'class="tab-page products-1688-page"',
        'class="tab-page active products-1688-page"', 1,
    )
    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", template, re.S))
    links = "\n".join(
        f'<link rel="stylesheet" href="/static/{name}">'
        for name in ("console-ui.css", "ai-original-products.css",
                     "1688-products.css", "zeshun-theme.css", "console-workflow.css")
    )
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<style>{styles}</style>{links}'
            '<style>body{padding:24px}.tab-page.active{display:block}</style>'
            '</head><body>' + section +
            '<script src="/static/1688-products.js"></script></body></html>')


@pytest.fixture(scope="module")
def products_browser():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as pw:
        candidates = [os.environ.get("CONSOLE_TEST_BROWSER", ""), pw.chromium.executable_path]
        executable = next((value for value in candidates if value and Path(value).is_file()), None)
        if not executable:
            pytest.skip("Chromium is required")
        browser = pw.chromium.launch(executable_path=executable, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def products_page(products_browser):
    page = products_browser.new_page(viewport={"width": 1920, "height": 1080})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    html = _product_section_html()

    def route(request):
        path = urlsplit(request.request.url).path
        if path == "/":
            request.fulfill(body=html, content_type="text/html")
        elif path.startswith("/static/"):
            request.fulfill(path=str(ROOT / "bit" / path.lstrip("/")))
        else:
            request.fulfill(status=404, body="Offline test does not access remote services")

    page.route("**/*", route)
    page.add_init_script(r"""
      window.aiOriginalDeleteRunning = false;
      window.mock = {
        requests: [], failPatch: false, failCategory: false, failList: false,
        categories: [{id:7,name:'家居用品',listing_ratio_percent:100},
                     {id:9,name:'汽车配件',listing_ratio_percent:100}],
        collectors: ['采集甲', '采集乙', '其他页业务员'],
        rows: [
          {id:41,title:'不锈钢真空保温杯 500ml',source_item_id:'701168800041',
           management_category_id:7,management_category_name:'家居用品',
           collector_salesperson:'采集甲',ai_status:'pending',review_status:'unreviewed',
           source_purchase_price_cny:12.5,source_weight_g:250,
           source_package_length_cm:12,source_package_width_cm:8,source_package_height_cm:20,
           net_proceeds_usd:10.8,source_1688_overrides:{},
           original_1688:{title:'不锈钢真空保温杯 500ml',category_name:'1688来源类目',
             source_url:'https://detail.1688.com/offer/701168800041.html',price:12.5,
             weight_g:250,package_length_cm:12,package_width_cm:8,package_height_cm:20,
             collected_at:'2026-10-02 09:30:00',properties:[{name:'材质',value:'不锈钢'}]}},
          {id:42,title:'多用途汽车手机支架',source_item_id:'701168800042',
           management_category_id:null,management_category_name:null,
           collector_salesperson:'采集乙',ai_status:'completed',review_status:'approved',
           source_purchase_price_cny:18,source_weight_g:160,
           source_package_length_cm:10,source_package_width_cm:6,source_package_height_cm:8,
           net_proceeds_usd:14.9,source_1688_overrides:{},
           original_1688:{title:'多用途汽车手机支架',category_name:'原始汽车用品',
             source_url:'https://detail.1688.com/offer/701168800042.html',price:18,
             weight_g:160,package_length_cm:10,package_width_cm:6,package_height_cm:8,
             collected_at:'2026-10-01 16:15:00'}}
        ]
      };
      const reply = (data, status=200) => new Response(JSON.stringify(data), {
        status, headers:{'Content-Type':'application/json'}
      });
      window.fetch = async (input, options={}) => {
        const url = new URL(typeof input === 'string' ? input : input.url, location.origin);
        const method = options.method || 'GET';
        const body = options.body ? JSON.parse(options.body) : null;
        mock.requests.push({path:url.pathname,query:Object.fromEntries(url.searchParams),method,body});
        if (url.pathname === '/api/mercado-management-categories' && method === 'GET') {
          return reply({status:'success',data:{rows:structuredClone(mock.categories)}});
        }
        if (url.pathname === '/api/1688-products/options') {
          return reply({status:'success',data:{collector_salespeople:mock.collectors}});
        }
        if (url.pathname === '/api/mercado-management-categories/assign') {
          if (mock.failCategory) return reply({status:'error',message:'分类保存失败'},500);
          const category = mock.categories.find(row => row.id === body.category_id);
          mock.rows.filter(row => body.item_ids.includes(row.id)).forEach(row => {
            row.management_category_id = category?.id || null;
            row.management_category_name = category?.name || null;
          });
          return reply({status:'success',data:{changed:body.item_ids.length}});
        }
        if (url.pathname === '/api/1688-products' && method === 'GET') {
          if (mock.failList) return reply({status:'error',message:'模拟刷新失败'},500);
          const rows = mock.rows.filter(row =>
            (!url.searchParams.get('collector_salesperson') ||
              row.collector_salesperson === url.searchParams.get('collector_salesperson')) &&
            (!url.searchParams.get('management_category_id') ||
              String(row.management_category_id) === url.searchParams.get('management_category_id')));
          return reply({status:'success',data:{rows:structuredClone(rows),total:rows.length}});
        }
        if (/^\/api\/1688-products\/\d+$/.test(url.pathname) && method === 'PATCH') {
          if (mock.failPatch) return reply({status:'error',message:'模拟保存失败'},500);
          const row = mock.rows.find(row => row.id === Number(url.pathname.split('/').at(-1)));
          Object.entries(body).forEach(([field,value]) => {
            row.source_1688_overrides[field] = value;
            row[field === 'price' ? 'source_purchase_price_cny' : `source_${field}`] = value;
            if (field === 'net_proceeds_usd') row.net_proceeds_usd = value;
          });
          return reply({status:'success',data:structuredClone(row)});
        }
        return reply({status:'error',message:`Unexpected offline request: ${method} ${url.pathname}`},404);
      };
    """)
    page.goto("http://products.test/")
    page.evaluate("load1688Products()")
    page.wait_for_function("document.querySelectorAll('#products-1688-body tr').length === 2")
    yield page
    page.close()
    assert not errors, "Unexpected JavaScript errors: " + "\n".join(errors)


def _patches(page):
    return page.evaluate("mock.requests.filter(request => request.method === 'PATCH')")


def test_collector_options_include_other_pages_and_query_keeps_selection(products_page):
    page = products_page
    collector = page.locator("#products-1688-salesperson")
    assert "其他页业务员" in collector.locator("option").all_text_contents()
    collector.select_option(label="采集乙")
    page.wait_for_function("document.querySelectorAll('#products-1688-body tr').length === 1")
    query = page.evaluate("mock.requests.filter(r => r.path === '/api/1688-products').at(-1).query")
    assert query["collector_salesperson"] == "采集乙"
    assert collector.input_value() == "采集乙"
    assert "其他页业务员" in collector.locator("option").all_text_contents()
    assert "多用途汽车手机支架" in page.locator("#products-1688-body").inner_text()


def test_bulk_category_uses_shared_product_categories_and_can_clear(products_page):
    page = products_page
    category = page.locator("#products-1688-batch-category")
    assert "家居用品" in category.locator("option").all_text_contents()
    assert "汽车配件" in category.locator("option").all_text_contents()
    assert page.locator("#products-1688-category-submit").is_disabled()
    page.locator("#products-1688-select-all").check()
    category.select_option("9")
    page.locator("#products-1688-category-submit").click()
    page.wait_for_function("[...document.querySelectorAll('#products-1688-body tr td:nth-child(3)')].every(cell => cell.textContent === '汽车配件') && !document.querySelector('#products-1688-category-submit').disabled")
    assignment = page.evaluate("mock.requests.find(r => r.path.endsWith('/assign')).body")
    assert assignment == {"item_type": "products", "item_ids": [41, 42], "category_id": 9}
    assert page.locator("#products-1688-body tr td:nth-child(3)").all_text_contents() == ["汽车配件", "汽车配件"]
    page.locator("#products-1688-body tr").first.get_by_role("button", name="查看详情").click()
    assert "汽车配件" in page.locator("#products-1688-detail-content").inner_text()
    page.locator("#products-1688-detail-dialog").get_by_role("button", name="关闭", exact=True).click()
    category.select_option("uncategorized")
    page.locator("#products-1688-category-submit").click()
    page.wait_for_function("[...document.querySelectorAll('#products-1688-body tr td:nth-child(3)')].every(cell => cell.textContent === '未分类') && !document.querySelector('#products-1688-category-submit').disabled")
    assert page.evaluate("mock.requests.filter(r => r.path.endsWith('/assign')).at(-1).body.category_id") is None


def test_single_edit_submits_changed_values_in_g_cm_usd_and_cny(products_page):
    page = products_page
    page.locator("#products-1688-body tr").first.get_by_role("button", name="修改资料").click()
    dialog = page.locator("#products-1688-edit-dialog")
    assert dialog.is_visible()
    assert page.locator("#products-1688-edit-weight_g").input_value() == "250"
    values = {"weight_g": "375.5", "package_length_cm": "13.5", "package_width_cm": "9",
              "package_height_cm": "21", "net_proceeds_usd": "17.25", "price": "25.75"}
    for field, value in values.items():
        page.locator(f"#products-1688-edit-{field}").fill(value)
    page.locator("#products-1688-edit-save").click()
    page.wait_for_function("!document.querySelector('#products-1688-edit-dialog').open")
    assert _patches(page)[-1]["body"] == {field: float(value) for field, value in values.items()}
    row = page.locator("#products-1688-body tr").first
    assert "375.5 g" in row.inner_text()
    assert "长：13.5 cm · 宽：9 cm · 高：21 cm" in row.inner_text()
    assert "￥25.75" in row.inner_text()
    assert "$17.25" in row.inner_text()
    row.get_by_role("button", name="查看详情").click()
    detail = page.locator("#products-1688-detail-content").inner_text()
    assert all(value in detail for value in ["375.5 g", "长：13.5 cm", "￥25.75", "$17.25"])


def test_untouched_fields_are_not_patched_and_explicit_clear_beats_source_snapshot(products_page):
    page = products_page
    page.locator("#products-1688-body tr").first.get_by_role("button", name="修改资料").click()
    page.locator("#products-1688-edit-weight_g").fill("")
    page.locator("#products-1688-edit-save").click()
    page.wait_for_function("!document.querySelector('#products-1688-edit-dialog').open")
    assert _patches(page)[-1]["body"] == {"weight_g": None}
    assert page.locator("#products-1688-body tr").first.locator("td").nth(5).inner_text() == "—"
    assert page.evaluate("mock.rows[0].original_1688.weight_g") == 250
    page.locator("#products-1688-body tr").first.get_by_role("button", name="修改资料").click()
    assert page.locator("#products-1688-edit-weight_g").input_value() == ""
    page.locator("#products-1688-edit-save").click()
    assert len(_patches(page)) == 1


def test_failed_edit_stays_open_and_can_retry(products_page):
    page = products_page
    page.evaluate("mock.failPatch = true")
    page.locator("#products-1688-body tr").first.get_by_role("button", name="修改资料").click()
    page.locator("#products-1688-edit-price").fill("29.99")
    page.locator("#products-1688-edit-save").click()
    page.wait_for_function("document.querySelector('#products-1688-edit-message').textContent.includes('模拟保存失败')")
    assert page.locator("#products-1688-edit-dialog").is_visible()
    assert page.locator("#products-1688-edit-price").input_value() == "29.99"
    assert page.locator("#products-1688-edit-save").is_enabled()
    assert "￥12.50" in page.locator("#products-1688-body tr").first.inner_text()
    page.evaluate("mock.failPatch = false")
    page.locator("#products-1688-edit-save").click()
    page.wait_for_function("!document.querySelector('#products-1688-edit-dialog').open")
    assert _patches(page)[-1]["body"] == {"price": 29.99}
    assert "￥29.99" in page.locator("#products-1688-body tr").first.inner_text()


def test_saved_edit_survives_followup_list_failure(products_page):
    page = products_page
    page.locator("#products-1688-body tr").first.get_by_role("button", name="修改资料").click()
    page.locator("#products-1688-edit-net_proceeds_usd").fill("19.5")
    page.evaluate("mock.failList = true")
    page.locator("#products-1688-edit-save").click()
    page.wait_for_function("!document.querySelector('#products-1688-edit-dialog').open")
    assert _patches(page)[-1]["body"] == {"net_proceeds_usd": 19.5}
    assert "$19.50" in page.locator("#products-1688-body tr").first.inner_text()
    message = page.locator("#products-1688-status").inner_text()
    assert "资料已保存" in message and "模拟刷新失败" in message
    page.locator("#products-1688-body tr").first.get_by_role("button", name="查看详情").click()
    assert "$19.50" in page.locator("#products-1688-detail-content").inner_text()


def test_failed_bulk_category_preserves_existing_rows_and_selection(products_page):
    page = products_page
    page.evaluate("mock.failCategory = true")
    page.locator("#products-1688-select-all").check()
    page.locator("#products-1688-batch-category").select_option("9")
    page.locator("#products-1688-category-submit").click()
    page.wait_for_function("document.querySelector('#products-1688-status').textContent.includes('分类保存失败')")
    assert page.locator("#products-1688-body tr td:nth-child(3)").all_text_contents() == ["家居用品", "未分类"]
    assert page.locator("#products-1688-selection").inner_text() == "本页已选择 2 件"
    assert page.locator("#products-1688-category-submit").is_enabled()


def test_desktop_list_and_edit_dialog_layout(products_page, tmp_path):
    page = products_page
    destination = Path(os.environ.get("PRODUCTS_1688_SCREENSHOT_DIR", str(tmp_path)))
    destination.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(destination / "list.png"), full_page=True)
    page.locator("#products-1688-body tr").first.get_by_role("button", name="修改资料").click()
    dialog = page.locator("#products-1688-edit-dialog")
    assert dialog.is_visible()
    bounds = dialog.bounding_box()
    assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 1920
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 1080
    page.screenshot(path=str(destination / "edit-dialog.png"), full_page=True)
