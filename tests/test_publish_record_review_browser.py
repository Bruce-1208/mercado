"""Offline bulk/single review buttons and AI publication problem notices."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from test_console_ui_browser import ROOT, console_browser, console_page  # noqa: F401

TITLE = 'Máscara de Halloween Terrorífica con Peluca, Accesorio de'


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_bulk_single_review_and_failure_feedback(console_page, width):
    page = console_page
    page.set_viewport_size({'width':width,'height':1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    rows = [{'id':i,'product_item_id':i,'title':TITLE,'status':status,
             'source_item_id':'168812345','failure_reason':'SKU 2 白底主图未完成' if status != 'published' else ''}
            for i,status in [(1,'pending_review'),(2,'failed'),(3,'published'),(4,'pending')]]
    requests = []
    def retry(route):
        requests.append(route.request.post_data_json)
        route.fulfill(status=400, content_type='application/json', body=json.dumps({
            'status':'error','message':'复核未通过', 'data':{'review_issues':[{
                'title':TITLE,'product_item_id':1,'issues':['SKU 2 白底主图未完成']}]}}))
    page.route('**/api/mercado-publish-records/retry',retry)
    page.on('dialog', lambda dialog: dialog.accept())
    page.evaluate('''rows => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-publish-records').classList.add('active');
        renderMercadoPublishRecords({rows,total:4,counts:{all:4},publish_running:false});
    }''', rows)
    body = page.locator('#publish-record-body')
    assert body.locator('input[value="3"]').is_disabled()
    for number in [1,2]:
        checkbox = body.locator(f'input[value="{number}"]')
        checkbox.focus(); page.keyboard.press('Space')
    page.locator('#publish-record-retry').click()
    page.wait_for_function("!document.getElementById('publish-record-review-issues').hidden")
    assert requests[0] == {'record_ids':[1,2], 'review_before_publish':True}
    assert 'SKU 2 白底主图未完成' in page.locator('#publish-record-review-issues').inner_text()
    assert page.locator('#publish-record-retry').is_enabled()
    page.locator('#publish-record-review-all').click()
    page.wait_for_function("document.getElementById('publish-record-retry').textContent === '复核上架所选'")
    assert requests[1]['record_ids'] == [4,2,1]
    body.locator('tr').first.get_by_role('button',name='一键复核上架').click()
    page.wait_for_function("document.getElementById('publish-record-retry').textContent === '复核上架所选'")
    assert requests[2]['record_ids'] == [4]
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/publish-review-20261014'
    output.mkdir(parents=True,exist_ok=True)
    page.locator('#publish-record-review-issues').scroll_into_view_if_needed()
    page.screenshot(path=str(output/f'records-{width}.png'))
    assert not errors


@pytest.mark.parametrize('width', [1440,390])
def test_original_card_shows_publish_problem_and_opens_filtered_records(console_page, width):
    page = console_page
    page.set_viewport_size({'width':width,'height':1000})
    page.route('**/api/mercado-publish-records?*', lambda route:route.fulfill(content_type='application/json',body='{"status":"success","data":{"rows":[]}}'))
    page.evaluate('''title => {
        currentWorkbenchUser.permissions=['*'];
        mercadoPublishStoresLoaded=true;
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        aiOriginalRows=[{id:71,source_item_id:'168812345',title_es:title,
            original_1688:{title:'模拟万圣节面具'},ai_original:{status:'completed'},
            last_publish_status:'failed',last_publish_store_name:'模拟店铺',
            last_publish_error:'SKU 2 白底主图未完成 <img>'}];
        renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''',TITLE)
    warning=page.locator('#ai-original-grid .ai-original-publish-warning')
    warning.scroll_into_view_if_needed()
    assert warning.is_visible()
    page.wait_for_function("document.querySelector('#ai-original-grid .ai-original-publish-warning').innerText.length > 0")
    assert '上架失败 · 模拟店铺' in warning.inner_text()
    assert 'SKU 2 白底主图未完成' in warning.inner_text()
    assert warning.locator('img').count()==0
    warning.get_by_role('button',name='查看上架记录并复核').click()
    assert page.locator('#tab-mercado-publish-records').is_visible()
    assert page.locator('#publish-record-search').input_value()=='168812345'


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_review_batch_panel_uses_task_results_and_opens_batch_records(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    batch_id = 'retry-20261014112233-a1b2c3d4'
    row = {'id': 1, 'product_item_id': 1, 'title': TITLE, 'status': 'failed',
           'failure_reason': '旧失败记录，不属于本次结果'}
    running = {
        'selection_mode': 'retry', 'review_before_publish': True,
        'batch_id': batch_id, 'running': True, 'status': 'running',
        'message': '正在复核并上架 3 件产品', 'requested_count': 3,
        'processed_count': 0, 'published_count': 0, 'failed_count': 0,
        'pending_review_count': 0, 'started_at': '2026-10-14 11:22:33',
        'logs': ['[11:22:33] 开始复核并上架 3 件产品'], 'results': [],
    }
    completed = {
        **running, 'running': False, 'status': 'partial',
        'message': '批量上架完成：成功 1 件；失败 1 件；待审核 1 件',
        'processed_count': 3, 'published_count': 1, 'failed_count': 1,
        'pending_review_count': 1, 'finished_at': '2026-10-14 11:22:36',
        'logs': [
            '[11:22:33] 开始复核并上架 3 件产品',
            '[11:22:34] 成功 · SKU-1 · 模拟店铺 · 巴西 · 已发布',
            '[11:22:35] 失败 · SKU-2 · 模拟店铺 · 巴西 · <img src=x> 图片不合规',
            '[11:22:36] 待审核 · SKU-3 · 模拟店铺 · 巴西 · 等待平台审核',
        ],
        'results': [
            {'source_item_id': 'SKU-1', 'status': 'published', 'message': '已发布'},
            {'source_item_id': 'SKU-2', 'status': 'failed', 'message': '图片不合规'},
            {'source_item_id': 'SKU-3', 'status': 'pending_review', 'message': '等待平台审核'},
        ],
    }
    queries = []
    page.route('**/api/mercado-publish-records/retry', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': running})))

    def records(route):
        queries.append(parse_qs(urlsplit(route.request.url).query, keep_blank_values=True))
        result_rows = [
            {'id': 10, 'title': '模拟成功商品', 'source_item_id': 'SKU-1',
             'status': 'published', 'published_item_id': 'MLB900', 'batch_id': batch_id + '-1'},
            {'id': 11, 'title': '模拟失败商品', 'source_item_id': 'SKU-2',
             'status': 'failed', 'failure_reason': '图片不合规', 'batch_id': batch_id + '-1'},
        ] if queries[-1]['search'] == [batch_id] else [row]
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'rows': result_rows, 'publish_running': False, 'publish_state': completed,
            'total': 1, 'counts': {'all': 4, 'failed': 1, 'pending_review': 1, 'pending': 2},
        }}))

    page.route('**/api/mercado-publish-records?*', records)
    page.on('dialog', lambda dialog: dialog.accept())
    page.evaluate('''row => {
      document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
      document.getElementById('tab-mercado-publish-records').classList.add('active');
      document.getElementById('publish-record-status').value = 'failed';
      document.getElementById('publish-record-end-date').value = '2026-10-01T12:00';
      document.getElementById('publish-record-site').value = 'MLM';
      document.getElementById('publish-record-store').innerHTML = '<option value="99" selected>旧店铺</option>';
      renderMercadoPublishRecords({rows:[row],total:1,counts:{all:4,failed:4},publish_running:false});
    }''', row)
    page.locator('#publish-record-review-all').click()
    page.wait_for_function("!document.getElementById('publish-record-run-panel').hidden")
    assert batch_id in page.locator('#publish-record-run-summary').inner_text()

    page.wait_for_function("document.getElementById('publish-record-run-progress').textContent === '3 / 3'")
    assert page.locator('#publish-record-run-published').inner_text() == '1'
    assert page.locator('#publish-record-run-failed').inner_text() == '1'
    assert page.locator('#publish-record-run-pending-review').inner_text() == '1'
    assert page.locator('#publish-record-count-pending').inner_text() == '3'
    assert '待审核 / 等待 / 暂停' in page.locator('#publish-record-count-pending').locator('xpath=..').inner_text()
    assert '成功 1，失败 1，待审核 1' in page.locator('#publish-record-message').inner_text()
    assert '旧失败记录，不属于本次结果' not in page.locator('#publish-record-body').inner_text()
    assert 'MLB900' in page.locator('#publish-record-body').inner_text()
    assert '图片不合规' in page.locator('#publish-record-body').inner_text()
    log = page.locator('#publish-record-run-log')
    assert '成功 · SKU-1' in log.inner_text()
    assert '失败 · SKU-2' in log.inner_text()
    assert '待审核 · SKU-3' in log.inner_text()
    assert log.locator('img').count() == 0
    assert page.locator('#publish-record-status').input_value() == ''
    batch_query = next(query for query in reversed(queries) if query.get('search') == [batch_id])
    assert batch_query['end_date'] == ['']
    assert batch_query['site_id'] == ['']
    assert 'token_id' not in batch_query
    assert page.locator('#publish-record-review-all').is_enabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    with page.expect_response(lambda response: '/api/mercado-publish-records?' in response.url and batch_id in response.url) as batch_response:
        page.locator('#publish-record-run-view').click()
    page.wait_for_function("document.getElementById('publish-record-search').value.startsWith('retry-')")
    assert page.locator('#publish-record-status').input_value() == ''
    assert page.locator('#publish-record-search').input_value() == batch_id
    page.wait_for_function("document.getElementById('publish-record-message').textContent.includes('成功 1')")
    batch_query = parse_qs(urlsplit(batch_response.value.url).query, keep_blank_values=True)
    assert batch_query['search'] == [batch_id]
    assert batch_query['status'] == ['']
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = ROOT / 'artifacts/publish-result-visibility-20261014'
    output.mkdir(parents=True, exist_ok=True)
    page.locator('#publish-record-run-panel').scroll_into_view_if_needed()
    page.screenshot(path=str(output / f'results-{width}.png'))


def test_review_manual_refresh_recovers_after_transient_failure(console_page):
    page = console_page
    calls = []

    def records(route):
        calls.append(route.request.url)
        if len(calls) == 1:
            route.fulfill(status=503, content_type='application/json',
                          body=json.dumps({'status': 'error', 'message': '暂时繁忙'}))
        else:
            route.fulfill(content_type='application/json', body=json.dumps({
                'status': 'success', 'data': {'rows': [], 'publish_state': {
                    'selection_mode': 'retry', 'batch_id': 'retry-recovery',
                    'running': False, 'status': 'completed',
                    'requested_count': 1, 'processed_count': 1, 'published_count': 1,
                    'logs': ['成功 · SKU-1 · 产品编号 MLB900'],
                }}}))

    page.route('**/api/mercado-publish-records?*', records)
    page.evaluate('''() => {
        document.getElementById('tab-mercado-publish-records').classList.add('active');
        mercadoPublishRunning = true;
        loadMercadoPublishRecords();
    }''')
    page.wait_for_function("document.getElementById('publish-record-message').textContent.includes('已停止自动刷新')")
    assert len(calls) == 1
    page.evaluate('loadMercadoPublishRecords()')
    page.wait_for_function("document.getElementById('publish-record-run-published').textContent === '1'")
    assert len(calls) == 2
    assert 'MLB900' in page.locator('#publish-record-run-log').inner_text()


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_execution_states_visible_before_platform_submission(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    rows = [
        {'id': 1, 'title': '模拟等待商品', 'status': 'pending', 'result_json': {'stage': 'queued'}},
        {'id': 2, 'title': '模拟定时商品', 'status': 'pending', 'result_json': {'stage': 'queued', 'scheduled_at': '2027-01-01T10:00:00Z'}},
        {'id': 3, 'title': '模拟检查商品', 'status': 'publishing', 'result_json': {'stage': 'preparing', 'platform_submitted': False}},
        {'id': 4, 'title': '模拟未执行商品', 'status': 'failed', 'failure_reason': '未执行：线程资源不足', 'result_json': {'stage': 'not_executed', 'platform_submitted': False}},
    ]
    page.evaluate('''rows => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-mercado-publish-records').classList.add('active');
        renderMercadoPublishRecords({rows, total: 4, publish_running: true});
    }''', rows)
    body = page.locator('#publish-record-body')
    for label in ['等待执行', '定时等待', '执行中 · 检查资料', '未执行：线程资源不足']:
        assert label in body.inner_text()
    assert body.locator('input[value="2"]').is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    body.scroll_into_view_if_needed()
    page.locator('#tab-mercado-publish-records .market-table-wrap').evaluate('(el) => { const cell = el.querySelector("thead th:nth-child(7)"); el.scrollLeft += cell.getBoundingClientRect().left - el.getBoundingClientRect().left; }')
    output = ROOT / 'artifacts/publish-queued-20261014'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'states-{width}.png'))
