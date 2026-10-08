from pathlib import Path
from urllib.parse import urlsplit

import pytest
from flask import Flask, render_template
from test_console_ui_browser import console_browser, console_page  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_console_profile_entry_is_visible_and_reachable(console_page, width):
    console_page.set_viewport_size({'width': width, 'height': 1000})
    link = console_page.locator('.topbar a[href="/settings/profile"]')
    assert link.is_visible()
    box = link.bounding_box()
    assert box['x'] >= 0 and box['x'] + box['width'] <= width
    assert link.evaluate("e => { const r = e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); }")
    assert console_page.evaluate('document.documentElement.scrollWidth <= innerWidth')


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_profile_form_save_password_errors_and_mobile_layout(width, console_browser):
    app = Flask(__name__, template_folder=str(ROOT / 'bit/templates'), static_folder=str(ROOT / 'bit/static'))
    with app.test_request_context():
        html = render_template('account_profile.html', csrf_token='csrf-test')
    profile = {'username': 'sales7', 'salesperson': '业务员甲', 'personal_name': '张三',
               'email': 'sales7@example.com', 'phone': '13800138000', 'wechat_id': 'wx-old', 'department': '运营部'}
    writes, errors = [], []
    unavailable = True
    save_failed = True

    def route(handler):
        nonlocal unavailable, save_failed
        request = handler.request
        path = urlsplit(request.url).path
        if path == '/settings/profile':
            handler.fulfill(body=html, content_type='text/html')
        elif path.startswith('/static/'):
            handler.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
        elif path == '/api/account/profile':
            if unavailable:
                unavailable = False
                handler.fulfill(status=503, json={'status': 'error', 'message': '个人资料读取失败'})
                return
            if request.method == 'PUT':
                assert request.headers['x-csrf-token'] == 'csrf-test'
                writes.append(request.post_data_json)
                if save_failed:
                    save_failed = False
                    handler.fulfill(status=503, json={'status': 'error', 'message': '保存失败，请重试'})
                    return
                profile.update(request.post_data_json)
            handler.fulfill(json={'status': 'success', 'data': profile, 'csrf_token': 'csrf-test'})
        elif path == '/api/account/password':
            assert request.headers['x-csrf-token'] == 'csrf-test'
            writes.append(request.post_data_json)
            if request.post_data_json['current_password'] == 'wrong-password':
                handler.fulfill(status=400, json={'status': 'error', 'message': '原密码不正确'})
            else:
                handler.fulfill(json={'status': 'success', 'login_url': '/login'})
        elif path == '/login':
            handler.fulfill(body='<meta charset="utf-8"><h1>重新登录</h1>', content_type='text/html')
        else:
            handler.fulfill(status=404, body='Not found')

    page = console_browser.new_page(viewport={'width': width, 'height': 1000})
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/*', route)
    try:
        page.goto('http://profile.test/settings/profile')
        page.locator('#retry-load').wait_for(state='visible')
        assert page.locator('#save-profile').is_disabled()
        page.locator('#retry-load').click()
        page.wait_for_function("!document.querySelector('#profile-fields').disabled")
        assert page.locator('#username').input_value() == 'sales7'
        assert page.locator('#salesperson').input_value() == '业务员甲'
        assert page.locator('#salesperson').get_attribute('readonly') is not None
        assert page.locator('[name=personal_name]').input_value() == '张三'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('[name=personal_name]').fill('李四')
        page.locator('[name=email]').fill('new@example.com')
        page.locator('[name=phone]').fill('13900139000')
        page.locator('#save-profile').click()
        page.wait_for_function("document.querySelector('#profile-message').textContent.includes('保存失败')")
        assert page.locator('[name=personal_name]').input_value() == '李四'
        assert page.locator('#save-profile').is_enabled()
        page.locator('#save-profile').click()
        page.wait_for_function("document.querySelector('#profile-message').textContent.includes('已保存')")
        assert writes[-1] == {'personal_name': '李四', 'email': 'new@example.com', 'department': '运营部',
                               'wechat_id': 'wx-old', 'phone': '13900139000'}
        assert page.locator('#salesperson').input_value() == '业务员甲'
        page.locator('[name=current_password]').fill('wrong-password')
        page.locator('[name=new_password]').fill('new-password')
        page.locator('[name=confirm_password]').fill('different')
        page.locator('#save-password').click()
        assert '不一致' in page.locator('#password-message').inner_text()
        assert len(writes) == 2
        page.locator('[name=confirm_password]').fill('new-password')
        page.locator('#save-password').click()
        page.wait_for_function("document.querySelector('#password-message').textContent.includes('原密码不正确')")
        assert page.locator('#save-password').is_enabled()
        artifact_dir = ROOT / 'artifacts' / 'workbench-profile'
        artifact_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact_dir / f'profile-{width}.png'), full_page=True)
        page.locator('[name=current_password]').fill('old-password')
        page.locator('#save-password').click()
        page.wait_for_url('**/login')
        assert page.locator('h1').inner_text() == '重新登录'
        assert not errors
    finally:
        page.close()
