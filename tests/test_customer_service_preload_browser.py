"""Exercise startup with real scripts and read/replied messages, without live APIs."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader
import pytest
from test_console_ui_browser import console_browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('performance_available', [True, False])
def test_preload_runs_without_performance_script_and_keeps_all_reply_states(console_browser, performance_available):
    env = Environment(loader=FileSystemLoader(ROOT / 'bit/templates'))
    html = env.get_template('index.html').render(
        current_user={'id': 7, 'username': 'preview', 'is_admin': True},
        can_change_task_workers=False, runtime_role='server',
        url_for=lambda endpoint, filename: '/static/' + filename,
    )
    page = console_browser.new_page()
    calls, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    now = datetime.now(timezone.utc).isoformat()
    def route(r):
        path = urlsplit(r.request.url).path
        data = {}
        if path == '/':
            return r.fulfill(content_type='text/html', body=html)
        if path.startswith('/static/'):
            if not performance_available and path.endswith(('console-performance.js', 'console-preload.js')):
                return r.fulfill(content_type='text/javascript', body='')
            return r.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        if path == '/api/mercado-tokens':
            data = {'rows': [{'id': 7, 'enabled': True, 'meli_user_id': '77', 'display_name': '测试店铺'}]}
        if path.endswith('/customer-service-aggregate'):
            payload = r.request.post_data_json
            calls.append(payload)
            if payload['mode'] == 'post-sale':
                conversations = [{'pack_id': str(index), 'unread_count': 1 if index == 1 else 0,
                    'bundle': {'paging': {'total': 1}, 'messages': [{
                        'text': label, 'from': {'user_id': '77' if index == 3 else '88'},
                        'message_date': {'created': now, 'read': None if index == 1 else now},
                    }]}} for index, label in [(1, '未读售后'), (2, '已读售后'), (3, '已回复售后')]]
                data = {'stores': [{'token_id': 7, 'conversations': conversations, 'updated_at': now}], 'pending_token_ids': []}
            else:
                data = {'stores': [{'token_id': 7, 'rows': [{'id': '123', 'token_id': 7,
                         'type': 'mediations', 'status': 'closed', 'date_created': now}],
                         'total': 1, 'status_totals': {'closed': 1}, 'updated_at': now}], 'pending_token_ids': []}
        r.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': data}))
    page.clock.install()
    page.route('**/*', route)
    page.goto('http://after-sales.test/')
    page.wait_for_function('customerServiceHasSnapshot && !customerServiceLoading')
    assert len([row for row in calls if row['mode'] == 'post-sale']) == 1
    assert any(row['mode'] == 'claims' for row in calls)
    for payload in calls:
        assert payload['filters']['date_from']
        assert payload['filters']['date_to']
    page.clock.fast_forward(3601000)
    page.wait_for_function('!customerServiceLoading && customerServiceHasSnapshot')
    assert len([row for row in calls if row['mode'] == 'post-sale']) == 2
    page.evaluate("switchTab('customer-service')")
    assert page.evaluate('customerServiceMode') == 'claims'
    assert page.locator('.customer-service-mode.active').get_attribute('data-mode') == 'claims'
    assert page.locator('#customer-service-search').get_attribute('placeholder') == '输入订单号或索赔号'
    assert page.locator('#customer-service-list tbody tr').count() == 1
    assert '123' in page.locator('#customer-service-list').inner_text()
    page.locator('.customer-service-mode[data-mode="post-sale"]').click()
    page.wait_for_function('customerServiceMode === "post-sale" && !customerServiceLoading')
    assert page.locator('#customer-service-list tbody tr').count() == 3
    assert '已读售后' in page.locator('#customer-service-list').inner_text()
    assert '已回复售后' in page.locator('#customer-service-list').inner_text()
    assert not errors
    page.close()
