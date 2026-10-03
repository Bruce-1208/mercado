"""Render the real integration template with deterministic provider responses."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def balance_browser():
    playwright_api = pytest.importorskip('playwright.sync_api')
    with playwright_api.sync_playwright() as playwright:
        candidates = [
            os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE', ''),
            playwright.chromium.executable_path,
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        ]
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        if not executable:
            pytest.skip('No local Chromium browser available')
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        yield browser
        browser.close()


def provider_overview():
    names = {
        'deepseek': 'DeepSeek', 'dashscope': '阿里云百炼 DashScope',
        'openai': 'OpenAI', 'seedance': '火山方舟 Seedance', 'wan': '阿里云万相 Wan',
    }
    messages = {
        'deepseek': 'DeepSeek 余额正常（20.50 元、2 美元）。',
        'dashscope': '百炼 API Key 无法直接查询账户余额，请登录该 Key 所属的阿里云账号查看余额。',
        'openai': 'OpenAI 模型 API Key 无法直接查询剩余余额，请登录所属组织的账单页面查看。',
        'seedance': '方舟 API Key 无法直接查询账户余额，请登录该 Key 所属的火山引擎账号查看余额。',
        'wan': '万相 API Key 无法直接查询账户余额，请登录所属的阿里云账号查看；与百炼属于同一账号时共用账户余额。',
    }
    return {'status': 'unsupported', 'message': '已配置 5 项模型服务。', 'providers': [
        {
            'provider': provider, 'name': name,
            'status': 'ok' if provider == 'deepseek' else 'unsupported', 'message': messages[provider],
            'balances': [{'currency': 'CNY', 'amount': '20.50'}, {'currency': 'USD', 'amount': '2'}] if provider == 'deepseek' else [],
            **({'checked_at': '2026-10-01T01:00:00Z'} if provider == 'deepseek' else {}),
            'console_url': 'javascript:alert(1)',
        }
        for provider, name in names.items()
    ]}


def integration_page(browser, width):
    environment = Environment(loader=FileSystemLoader(ROOT / 'bit/templates'), autoescape=True)
    html = environment.get_template('integration_settings.html').render(
        url_for=lambda endpoint, filename: '/static/' + filename,
        current_user=SimpleNamespace(display_name='余额测试账号', username='balance-test'),
    )
    page = browser.new_page(viewport={'width': width, 'height': 1100}, locale='zh-CN', timezone_id='Asia/Shanghai')
    state = {'overview': provider_overview(), 'failure': False, 'requests': [], 'errors': []}
    page.on('pageerror', lambda error: state['errors'].append(str(error)))

    def route_request(route):
        request = route.request
        path = urlparse(request.url).path
        state['requests'].append((path, request.method, urlparse(request.url).query))
        if path == '/settings/integrations':
            route.fulfill(content_type='text/html', body=html)
        elif path.startswith('/static/'):
            asset = ROOT / 'bit/static' / path.removeprefix('/static/')
            route.fulfill(content_type='text/css' if asset.suffix == '.css' else 'text/javascript', body=asset.read_text())
        elif path == '/api/account-integrations/balances':
            if state['failure']:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({'status': 'error'}))
            else:
                route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': state['overview']}))
        elif path == '/api/account-integrations/tokens':
            route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'message': '凭证已保存。', 'data': {}}))
        elif path == '/api/account-integrations':
            route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': {'tokens': {
                item['provider'] + '_configured': True for item in state['overview']['providers']
            }}}))
        else:
            route.abort()

    page.route('http://balance.test/**', route_request)
    page.goto('http://balance.test/settings/integrations')
    page.wait_for_selector('.model-balance-provider')
    return page, state


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_real_template_provider_cards_and_responsive_layout(balance_browser, width):
    page, state = integration_page(balance_browser, width)
    try:
        cards = page.locator('.model-balance-provider')
        assert cards.count() == 5
        assert cards.nth(0).locator('.model-balance-amount').inner_text() == '20.50 元 / 2 美元'
        assert page.locator('.model-balance-provider-checked').count() == 1
        assert page.locator('.model-balance-provider[data-status="unsupported"] .model-balance-amount').count() == 0
        assert page.locator('.model-balance-provider[data-status="unsupported"] .model-balance-status').all_text_contents() == ['需在服务商查看'] * 4
        assert page.locator('#model-balance-recharge').is_hidden()
        assert all(link.startswith('https://') for link in page.locator('.model-balance-console').evaluate_all('(links) => links.map(link => link.href)'))
        panel = page.locator('#model-balance-alert')
        assert panel.evaluate('(node) => node.scrollWidth <= node.clientWidth')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if width in (1440, 390):
            output = ROOT / 'artifacts/model-balances-ui'
            output.mkdir(parents=True, exist_ok=True)
            panel.screenshot(path=str(output / f'providers-{width}.png'))
        assert not state['errors']
    finally:
        page.close()


def test_real_save_button_refreshes_all_providers_and_failed_refresh_clears_old_amount(balance_browser):
    page, state = integration_page(balance_browser, 390)
    try:
        page.locator('#openai_api_key').fill('saved-test-key')
        page.locator('#save-tokens').click()
        page.wait_for_function("document.getElementById('token-message').textContent === '凭证已保存。'")
        page.wait_for_function("document.getElementById('model-balance-alert').getAttribute('aria-busy') === 'false'")
        assert ('/api/account-integrations/tokens', 'PUT', '') in state['requests']
        assert ('/api/account-integrations/balances', 'GET', 'refresh=1') in state['requests']
        state['failure'] = True
        page.locator('#model-balance-refresh').click()
        page.wait_for_function("document.getElementById('model-balance-alert').dataset.status === 'unknown'")
        assert page.locator('.model-balance-provider').count() == 0
        assert '当前余额状态未知' in page.locator('#model-balance-message').inner_text()
        assert '20.50' not in page.locator('#model-balance-alert').inner_text()
        assert not state['errors']
    finally:
        page.close()
