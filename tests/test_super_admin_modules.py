"""Restricted workspaces: offline route guard and real-template navigation."""
import ast
import json
from urllib.parse import urlsplit

import pytest
from flask import Flask, request, jsonify
from jinja2 import Environment, FileSystemLoader
from test_console_ui_browser import ROOT, console_browser  # noqa: F401

TABS = ('product-diagnosis', 'clearance-alerts', 'yandex', 'price-competition')
ROLES = ('super_admin', 'enterprise_admin', 'operator', 'premium_member')


@pytest.mark.parametrize('role', (*ROLES, None))
@pytest.mark.parametrize('path', [
    '/product-diagnosis', '/api/product-diagnosis/run',
    '/clearance-alerts', '/api/clearance-alerts/config',
    '/price-competition', '/api/price-competition/run',
    '/yandex-console/', '/yandex-console/api/stores',
    '/api/yandex-console/start', '/api/yandex-console/status',
])
def test_module_guard(role, path):
    tree = ast.parse((ROOT / 'bit/bit_interface.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'enforce_super_admin_modules')
    function.decorator_list = []
    env = {'request': request, 'jsonify': jsonify,
           'get_current_workbench_user': lambda: {'role_key': role, 'permissions': ['*']} if role else None}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<guard>', 'exec'), env)
    with Flask(__name__).test_request_context(path):
        result = env['enforce_super_admin_modules']()
        if role == 'super_admin':
            assert result is None
        else:
            assert result[1] == (403 if role else 401)


@pytest.mark.parametrize('width', [1440, 1024, 390])
@pytest.mark.parametrize('role', ROLES)
def test_module_navigation(console_browser, role, width):
    html = Environment(loader=FileSystemLoader(ROOT / 'bit/templates')).get_template('index.html').render(
        current_user={'id': 31, 'username': 'preview', 'role_key': role, 'permissions': ['*'],
                      'is_platform_admin': role == 'super_admin'},
        can_change_task_workers=False, can_access_shop_status=True,
        runtime_role='server', url_for=lambda endpoint, filename: '/static/' + filename)
    page = console_browser.new_page(viewport={'width': width, 'height': 1000})
    def intercept(route):
        path = urlsplit(route.request.url).path
        if path == '/':
            route.fulfill(body=html, content_type='text/html')
        elif path.startswith('/static/'):
            route.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        else:
            route.fulfill(content_type='application/json', body=json.dumps(
                {'status': 'success', 'data': [], 'items': [], 'stores': [], 'total': 0}))
    page.route('**/*', intercept)
    try:
        page.goto('http://console.test/')
        for tab in TABS:
            assert page.locator(f'.sidebar [data-tab="{tab}"]').count() == (1 if role == 'super_admin' else 0)
            page.evaluate('(tab) => switchTab(tab)', tab)
            assert page.locator(f'#tab-{tab}').evaluate("e => e.classList.contains('active')") == (role == 'super_admin')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        page.close()
