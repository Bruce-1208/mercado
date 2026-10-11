"""Verify product hydration, draft preservation and previews in the real template."""
from pathlib import Path

import pytest
from test_console_ui_browser import console_browser, console_page


def setup_questions(page, delayed=False):
    page.route('https://images.test/**', lambda route: route.fulfill(content_type='image/svg+xml', body='''
        <svg xmlns="http://www.w3.org/2000/svg" width="500" height="500" viewBox="0 0 500 500">
        <rect width="500" height="500" fill="white"/>
        <rect x="210" y="65" width="80" height="65" rx="14" fill="#334155"/>
        <rect x="170" y="115" width="160" height="330" rx="50" fill="#38bdf8"/>
        <rect x="195" y="140" width="20" height="275" rx="10" fill="#bae6fd"/>
        </svg>'''))
    page.evaluate('''delayed => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-pre-sale').classList.add('active');
        hasWorkbenchPermission = () => true;
        window.productCalls = [];
        window.productResponse = {products:{
            MLM123: {title:'Botella térmica de acero inoxidable 750 ml', image_url:'https://images.test/main.png'},
            CBT123: {title:'Global bottle', image_url:'https://images.test/global.png'},
        }};
        window.productPromise = delayed ? new Promise(resolve => window.resolveProducts = resolve) : Promise.resolve(productResponse);
        preSaleRequest = (action, options) => {
            productCalls.push({action, ...options});
            return productPromise;
        };
        preSaleRows = [1,2].map(id => ({id, _token_id:7, item_id:'CBT123', marketplace_item_id:'MLM123',
            site_id:'MLM', status:'UNANSWERED', text:'¿Está disponible?', _text_zh:'现在有货吗？',
            date_created:'2026-10-10T09:00:00Z', _store:{id:7,display_name:'墨西哥测试店铺'}}));
        renderPreSaleRows();
    }''', delayed)


@pytest.mark.parametrize('width', [1440, 390])
def test_titles_primary_images_and_hover_work_in_list_and_modal(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    setup_questions(page)
    page.wait_for_function("preSaleRows.every(row => row._product_loaded)")
    assert page.locator('#pre-sale-list .pre-sale-product-title').all_inner_texts() == [
        'Botella térmica de acero inoxidable 750 ml'] * 2
    assert len(page.evaluate('productCalls')) == 1
    assert page.evaluate('productCalls[0].data.item_ids') == 'MLM123,CBT123'
    image_link = page.locator('#pre-sale-list .pre-sale-product-image-link').first
    image_link.scroll_into_view_if_needed()
    page.wait_for_function("document.querySelector('#pre-sale-list .pre-sale-product-image-link img')?.naturalWidth > 0")
    image_link.hover()
    preview = page.locator('.order-image-preview.visible')
    assert preview.is_visible()
    assert preview.locator('img').get_attribute('src') == 'https://images.test/main.png'
    bounds = preview.bounding_box()
    assert bounds['width'] > image_link.bounding_box()['width']
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    page.mouse.move(0, 0)
    assert page.locator('.order-image-preview.visible').count() == 0
    page.evaluate('openPreSaleQuestion(0)')
    detail = page.locator('#pre-sale-detail-product')
    assert 'Botella térmica' in detail.inner_text()
    detail.locator('.pre-sale-product-image-link').hover()
    preview = page.locator('#pre-sale-detail-dialog .order-image-preview.visible')
    assert preview.is_visible()
    assert page.evaluate('''() => {
        const el = document.querySelector('#pre-sale-detail-dialog .order-image-preview');
        const r = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2))
            || document.elementFromPoint(r.x + r.width/2, r.y + r.height/2).closest('dialog')?.id === 'pre-sale-detail-dialog';
    }''')
    directory = Path(__file__).resolve().parents[1] / 'artifacts/pre-sale-products-20261010'
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / f'detail-hover-{width}.png'))
    page.evaluate('closePreSaleDetail()')
    assert page.locator('.order-image-preview.visible').count() == 0


def test_loading_products_preserves_reply_and_title_search(console_page):
    page = console_page
    setup_questions(page, delayed=True)
    page.evaluate('openPreSaleQuestion(0)')
    page.locator('#pre-sale-reply-text').fill('您好，有货的。')
    page.evaluate('resolveProducts(productResponse)')
    page.wait_for_function("preSaleRows.every(row => row._product_loaded)")
    assert page.locator('#pre-sale-reply-text').input_value() == '您好，有货的。'
    assert 'Botella térmica' in page.locator('#pre-sale-detail-product').inner_text()
    page.evaluate('closePreSaleDetail()')
    page.locator('#pre-sale-search').fill('térmica')
    assert page.locator('#pre-sale-list tbody tr').count() == 2
    page.locator('#pre-sale-search').fill('missing title')
    assert page.locator('#pre-sale-list tbody tr').count() == 0


def test_failed_site_product_falls_back_to_global_and_broken_image_has_placeholder(console_page):
    page = console_page
    setup_questions(page, delayed=True)
    page.evaluate("resolveProducts({products:{CBT123:{title:'Global bottle',image_url:'https://images.test/global.png'}}})")
    page.wait_for_function("preSaleRows.every(row => row._product_loaded)")
    assert page.locator('#pre-sale-list .pre-sale-product-title').first.inner_text() == 'Global bottle'
    page.locator('#pre-sale-list img').first.evaluate("image => image.dispatchEvent(new Event('error'))")
    assert page.locator('#pre-sale-list .pre-sale-product-placeholder').first.is_visible()
    assert page.locator('#pre-sale-list .pre-sale-product-image-link').first.is_hidden()


def test_product_failure_is_retryable_without_losing_reply(console_page):
    page = console_page
    setup_questions(page, delayed=True)
    page.evaluate('resolveProducts({products:{}})')
    page.wait_for_function("preSaleRows.every(row => row._product_loaded)")
    page.evaluate('openPreSaleQuestion(0)')
    page.locator('#pre-sale-reply-text').fill('等待您的回复')
    page.evaluate('productPromise = Promise.resolve(productResponse)')
    page.locator('#pre-sale-detail-product button').click()
    page.wait_for_function("preSaleRows[0]._product.title?.includes('Botella')")
    assert page.locator('#pre-sale-reply-text').input_value() == '等待您的回复'


def test_products_load_for_current_page_and_cache_is_scoped_to_store(console_page):
    page = console_page
    setup_questions(page)
    page.wait_for_function("preSaleRows.every(row => row._product_loaded)")
    page.evaluate('''() => {
        preSaleRows = Array.from({length:21}, (_,index) => ({id:index+10,_token_id:8,
            item_id:index === 20 ? 'CBT456' : 'CBT123',site_id:'MLM',status:'ANSWERED'}));
        productPromise = Promise.resolve({products:{CBT123:{title:'Store eight bottle'}}});
        renderPreSaleRows();
    }''')
    page.wait_for_function("preSaleRows.slice(0,20).every(row => row._product_loaded)")
    assert page.locator('#pre-sale-list .pre-sale-product-title').first.inner_text() == 'Store eight bottle'
    assert page.evaluate('productCalls.at(-1).tokenId') == 8
    assert page.evaluate('productCalls.at(-1).data.item_ids') == 'CBT123'
    assert not page.evaluate('Boolean(preSaleRows[20]._product_loaded)')
    page.evaluate('''() => {
        productPromise = Promise.resolve({products:{CBT456:{title:'Next page product'}}});
        preSalePage = 2;
        renderPreSaleRows();
    }''')
    page.wait_for_function('preSaleRows[20]._product_loaded')
    assert page.locator('#pre-sale-list .pre-sale-product-title').inner_text() == 'Next page product'
    assert page.evaluate('productCalls.at(-1).data.item_ids') == 'CBT456'
