import json
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
import pytest
from test_console_ui_browser import console_browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('role', ['member', 'operator', 'enterprise_admin', 'super_admin'])
def test_agent_controls_and_after_sales_launch(console_browser, role):
    admin = role in {'enterprise_admin', 'super_admin'}
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('index.html').render(
        current_user={'id': 12, 'username': 'preview', 'role_key': role, 'permissions': ['*'], 'access_version': 1},
        can_change_task_workers=False, runtime_role='server', url_for=lambda endpoint, filename: '/static/' + filename)
    page = console_browser.new_page(viewport={'width': 1440, 'height': 1000})
    calls = []
    status_calls = []
    def route(r):
        path = urlsplit(r.request.url).path
        if path == '/':
            return r.fulfill(content_type='text/html', body=html)
        if path.startswith('/static/'):
            return r.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        data = {}
        if path == '/api/mercado-tokens': data = {'rows': []}
        if path == '/api/execution-agents':
            calls.append(('list', None))
            data = {'agents': [{'agent_id': 'office', 'name': '办公室电脑', 'online': True, 'capabilities': ['daily_task', 'appeal']}]}
        if path == '/api/mercado-claims/9/open-browser':
            calls.append(('open', r.request.post_data_json))
            data = {'task_id': 'open-job', 'agent_name': '办公室电脑'}
            return r.fulfill(status=202, content_type='application/json', body=json.dumps({'status': 'success', 'data': data, 'message': '已提交到办公室电脑'}))
        if path == '/api/mercado-claims/open-store/open-job':
            status_calls.append(path)
            data = ({'running': True, 'status': 'queued', 'message': '等待 Agent 接收'} if len(status_calls) == 1
                    else {'running': False, 'status': 'success', 'message': '已打开授权店铺'})
        r.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': data}))
    page.route('**/*', route)
    page.goto('http://agent-access.test/')
    page.wait_for_function('typeof renderCustomerServiceTable === "function"')
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-customer-service').classList.add('active');
        customerServiceMode = 'claims';
        customerServiceRows = [{id: 'claim-123', token_id: 9, type: 'mediations', order_context: {shop_name: '授权店铺'}}];
        renderCustomerServiceTable();
    }''')
    launch = page.get_by_role('button', name='▶ 启动浏览器')
    assert launch.count() == int(admin)
    assert page.locator('#topbar-agent-download').is_visible() == admin
    for selector in ['#order-zying-sync-open', '#wdr-zying-execute', '#appeal-agent-field', '#daily-task-agent-field']:
        if not admin: assert not page.locator(selector).is_visible()
    if admin:
        page.evaluate('loadCustomerServiceAgents()')
        page.wait_for_function('document.getElementById("customer-service-agent").value === "office"')
        launch.click()
        page.wait_for_function('document.querySelector(".claim-actions button:last-of-type").textContent === "已排队"')
        page.wait_for_function('document.querySelector(".claim-actions button:last-of-type").textContent === "✓ 已启动"')
        assert next(body for kind, body in calls if kind == 'open') == {
            'claim_id': 'claim-123', 'shop_name': '授权店铺', 'execution_target': 'agent', 'agent_id': 'office'}
        assert len(status_calls) == 2
        assert '已打开授权店铺' in page.locator('.claim-browser-status').inner_text()
    else:
        page.evaluate('loadExecutionAgents(); loadDailyTaskAgents(); loadCustomerServiceAgents();')
        assert not calls
        assert page.locator('#customer-service-agent').count() == 0
        assert page.locator('#appeal-execution-target option[value="agent"]').count() == 0
    page.close()
