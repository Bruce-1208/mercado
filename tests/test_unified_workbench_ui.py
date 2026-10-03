"""Offline UI regressions: layout, last-filter-wins, and duplicate submissions."""
import json
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader
from test_console_ui_browser import ROOT, console_browser, console_page


@pytest.mark.parametrize('width', [1440, 1280, 390])
def test_filters_precede_batch_actions_and_empty_state_fits(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.wait_for_function("document.querySelector('#order-bulk-bar').previousElementSibling.matches('.order-filters')")
    filters = page.locator('#tab-orders .order-filters').bounding_box()
    bulk = page.locator('#order-bulk-bar').bounding_box()
    assert bulk['y'] >= filters['y'] + filters['height'] - 1
    page.locator('#tab-orders .zs-table-state').scroll_into_view_if_needed()
    state = page.locator('#tab-orders .zs-table-state').bounding_box()
    assert state['x'] >= 0 and state['x'] + state['width'] <= width + 1
    assert page.evaluate('document.documentElement.scrollWidth') == width
    assert page.locator('#order-select-all').get_attribute('aria-label') == '选中本页订单'


def test_permission_error_is_distinct_from_retryable_failure(console_page):
    page = console_page
    page.route('**/api/orders?*', lambda r: r.fulfill(status=403, json={'status':'error','message':'forbidden'}))
    page.evaluate('loadOrders(1)')
    assert page.locator('#order-table-body').get_attribute('data-ui-state') == 'denied'
    assert '管理员' in page.locator('#order-table-body').inner_text()
    assert page.locator('#order-table-body button').count() == 0
    page.route('**/api/orders?*', lambda r: r.fulfill(status=503, json={'status':'error','message':'暂时不可用'}))
    page.evaluate('loadOrders(1)')
    assert page.locator('#order-table-body').get_attribute('data-ui-state') == 'error'
    assert page.locator('#order-table-body button').inner_text() == '重新加载'


def test_store_link_scope_keeps_pages_but_clears_changed_filters(console_page):
    page = console_page
    page.route('**/api/store-links?*', lambda r: r.fulfill(json={'status':'success','data':{'rows':[], 'page':1, 'pages':3}}))
    page.evaluate('loadStoreLinks(1)')
    page.evaluate('selectedStoreLinkIds.add(123); updateStoreLinkSelection()')
    page.evaluate('loadStoreLinks(2)')
    assert page.evaluate('selectedStoreLinkIds.has(123)')
    page.evaluate("storeLinkSearch.value = 'new filter'; loadStoreLinks(1)")
    assert page.evaluate('selectedStoreLinkIds.size') == 0


def test_bulk_status_locks_all_mutations_until_response(console_page):
    page = console_page
    page.evaluate("""() => {
        window.writeCount = 0;
        const original = window.fetch;
        window.fetch = (url, options) => {
            if (url === '/api/store-links/bulk-update' && options?.method === 'POST') {
                window.writeCount++;
                return new Promise(resolve => { window.finishWrite = () => resolve(new Response(JSON.stringify({status:'success',data:{running:false}}), {headers:{'Content-Type':'application/json'}})); });
            }
            return original(url, options);
        };
        selectedStoreLinkIds.add(123);
        window.firstWrite = submitStoreLinkStatus([123], 'paused', true);
        submitStoreLinkStatus([123], 'paused', true);
        bulkUpdateStoreLinks();
    }""")
    assert page.evaluate('window.writeCount') == 1
    assert page.locator('#store-link-delete-button').is_disabled()
    assert page.locator('#store-link-activate-button').is_disabled()
    page.evaluate('finishWrite(); firstWrite')
    assert page.evaluate('storeLinkSubmitting') is False


def test_typing_filter_invalidates_selection_before_search(console_page):
    page = console_page
    page.evaluate('selectedOrderIds.add("123"); updateOrderSelectionState()')
    page.locator('#order-search-input').fill('changed')
    assert page.evaluate('selectedOrderIds.size') == 0


def test_product_add_locks_other_batch_mutations(console_page):
    page = console_page
    page.evaluate("""() => {
        window.productWrites = 0;
        const original = window.fetch;
        window.fetch = (url, options) => {
            if (url === '/api/mercado-products/add') {
                window.productWrites++;
                return new Promise(resolve => { window.finishProductWrite = () => resolve(new Response(JSON.stringify({status:'success',data:{count:1}}), {headers:{'Content-Type':'application/json'}})); });
            }
            return original(url, options);
        };
        mercadoListMode = 'collection'; selectedMercadoCollectionIds.add(123);
        window.productWrite = addSelectedMercadoProducts();
        addSelectedMercadoProducts(); deleteSelectedMercadoItems();
    }""")
    assert page.evaluate('productWrites') == 1
    assert page.locator('#mercado-delete-selected').is_disabled()
    page.evaluate('finishProductWrite(); productWrite')
    assert not page.evaluate('mercadoSelectionMutationBusy')


def test_dashboard_last_filter_wins(console_browser):
    env = Environment(loader=FileSystemLoader(ROOT / 'bit/templates'))
    html = env.get_template('infraction_dashboard.html').render(current_user={}, embedded=True, url_for=lambda endpoint, filename:'/static/'+filename)
    page = console_browser.new_page()
    def route(r):
        path = urlsplit(r.request.url).path
        if path == '/': r.fulfill(content_type='text/html', body=html)
        elif path.startswith('/static/'): r.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        else: r.fulfill(json={'status':'success','data':{}})
    page.route('**/*', route)
    page.goto('http://dashboard.test/')
    page.wait_for_function('!state.loading')
    result = page.evaluate("""async () => {
        const pending=[];
        window.fetch=(url,options)=>new Promise(resolve=>pending.push({url,options,resolve}));
        elements.search.value='first'; const first=loadData();
        elements.search.value='second'; const second=loadData();
        const response=total=>({ok:true,json:async()=>({status:'success',data:{total,rows:[],summary:{},filters:{}}})});
        pending[1].resolve(response(2)); await second;
        pending[0].resolve(response(99)); await first;
        return {urls:pending.map(r=>r.url),aborted:pending[0].options.signal.aborted,text:elements.detailCount.textContent,loading:state.loading};
    }""")
    assert len(result['urls']) == 2 and 'second' in result['urls'][1]
    assert result['aborted'] and not result['loading']
    assert '99' not in result['text']
    page.close()
