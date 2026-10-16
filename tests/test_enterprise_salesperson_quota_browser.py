"""Real console with simulated enterprise data; no external requests."""
import json
from urllib.parse import urlsplit
import pytest
from jinja2 import Environment, FileSystemLoader
from test_console_ui_browser import ROOT, console_browser  # noqa: F401


@pytest.mark.parametrize('width', [1440, 1024, 390])
@pytest.mark.parametrize('role', ['enterprise_admin', 'super_admin'])
def test_enterprise_information_and_quota(console_browser, width, role):
    admin = role == 'super_admin'
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('index.html').render(
        current_user={'id': 31, 'username': 'preview', 'display_name': '预览管理员',
                      'role_key': role, 'role_name': '管理员', 'organization_key': 'company-a',
                      'is_platform_admin': admin, 'permissions': ['*'] if admin else ['access.view', 'access.manage']},
        can_change_task_workers=False, can_access_shop_status=True,
        runtime_role='server', url_for=lambda endpoint, filename: '/static/' + filename)
    page = console_browser.new_page(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
    organizations = [{'organization_key': 'company-a', 'organization_name': '模拟企业甲', 'salesperson_count': 2, 'salesperson_limit': 3}]
    writes = []
    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/':
            request.fulfill(body=html, content_type='text/html')
        elif path.startswith('/static/'):
            request.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        else:
            data = []
            if path == '/api/access/organizations':
                data = organizations
            elif path == '/api/access/organizations/company-a':
                writes.append(request.request.post_data_json)
                organizations[0]['salesperson_limit'] = writes[-1]['salesperson_limit']
            request.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': data, 'items': [], 'stores': [], 'groups': [], 'total': 0}))
    page.route('**/*', route)
    try:
        page.goto('http://console.test/')
        page.evaluate("switchTab('access')")
        body = page.locator('#access-organization-body')
        page.wait_for_function("document.querySelector('#access-organization-body').textContent.includes('模拟企业甲')")
        assert '2 / 3' in body.inner_text()
        if admin:
            field = body.locator('input')
            field.fill('5')
            field.focus()
            page.keyboard.press('Tab')
            assert body.locator('button').evaluate('e => e === document.activeElement')
            page.keyboard.press('Enter')
            page.wait_for_function("document.querySelector('#access-organization-body').textContent.includes('2 / 5')")
            assert writes == [{'salesperson_limit': 5}]
            assert page.locator('#access-organization-limit').input_value() == '3'
        else:
            assert body.locator('input').count() == 0
            assert page.locator('#access-organization-form').count() == 0
            assert not writes
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right > innerWidth + 1 && getComputedStyle(e).position !== 'fixed').slice(0,20).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right])")
        output = ROOT / 'artifacts/enterprise-salesperson-quota'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'{role}-{width}.png'))
    finally:
        page.close()
