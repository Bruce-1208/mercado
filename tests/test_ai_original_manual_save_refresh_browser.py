"""Manual fields remain editable across asynchronous list refreshes."""
import json

import pytest

from test_console_ui_browser import console_browser, console_page  # noqa: F401


@pytest.fixture
def manual_page(console_page):
    page = console_page
    row = {
        'id': 1, 'category_id': '', 'review_status': 'unreviewed',
        'weight_g': 200, 'net_proceeds_usd': 8,
        'package_length_cm': 10, 'package_width_cm': 9, 'package_height_cm': 8,
        'original_1688': {'title': '离线包装编辑测试'}, 'ai_original': {'status': 'pending'},
    }
    page.evaluate('''row => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        aiOriginalRows = [row]; renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''', row)
    return page, row


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_background_refresh_preserves_manual_input_and_focus(manual_page, width):
    page, row = manual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/api/ai-original-products?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {'rows': [row]}})))
    page.locator('#ai-original-length-1').fill('25')
    page.locator('#ai-original-width-1').fill('15')
    page.locator('#ai-original-height-1').fill('5')
    page.locator('#ai-original-net-1').fill('12')
    page.evaluate('loadAiOriginalProducts({preserveTaskStatus: true})')
    for name, value in [('length', '25'), ('width', '15'), ('height', '5'), ('net', '12')]:
        assert page.locator(f'#ai-original-{name}-1').input_value() == value
    assert page.locator('#ai-original-net-1').evaluate('(el) => el === document.activeElement')
    page.locator('#ai-original-grid input[type=checkbox]').check()
    page.evaluate('loadAiOriginalProducts({preserveTaskStatus: true})')
    assert page.locator('#ai-original-net-1').input_value() == '12'
    assert page.locator('#ai-original-grid input[type=checkbox]').is_checked()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


@pytest.mark.parametrize('width', [1440, 390])
def test_editor_save_updates_outer_card_without_list_reload(manual_page, width):
    page, row = manual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/api/ai-original-products?*', lambda route: route.fulfill(
        status=503, content_type='application/json', body='{"status":"error","message":"刷新失败"}'))
    def save(route):
        changes = route.request.post_data_json
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {**row, **changes},
        }))
    page.route('**/api/ai-original-products/1/listing', save)
    page.locator('#ai-original-grid .ai-original-edit-button').click()
    for name, value in [('length', '25'), ('width', '15'), ('height', '5'), ('net', '12')]:
        page.locator(f'#ai-original-editor-{name}').fill(value)
    page.get_by_role('button', name='保存并同步', exact=True).click()
    page.wait_for_function('!document.getElementById("ai-original-editor-dialog").open')
    for name, value in [('length', '25'), ('width', '15'), ('height', '5'), ('net', '12')]:
        assert page.locator(f'#ai-original-{name}-1').input_value() == value
    assert '已保存' in page.locator('#ai-original-task-status').inner_text()
    page.locator('#ai-original-grid .ai-original-edit-button').click()
    assert page.locator('#ai-original-editor-net').input_value() == '12'
    assert page.locator('#ai-original-editor-length').input_value() == '25'


def test_background_request_started_before_typing_keeps_edits(manual_page):
    page, row = manual_page
    page.evaluate("""row => {
        const originalFetch = window.fetch;
        window.fetch = (url, options) => String(url).includes('/api/ai-original-products?')
            ? new Promise(resolve => { window.finishManualListRefresh = () => resolve({
                ok: true, json: async () => ({status: 'success', data: {rows: [row]}})
            }); }) : originalFetch(url, options);
        window.manualRefresh = loadAiOriginalProducts({preserveTaskStatus: true});
    }""", row)
    page.locator('#ai-original-net-1').fill('19.5')
    page.evaluate('async () => { finishManualListRefresh(); await manualRefresh; }')
    assert page.locator('#ai-original-net-1').input_value() == '19.5'
    assert page.locator('#ai-original-net-1').evaluate('(el) => el === document.activeElement')


@pytest.mark.parametrize('width', [1440, 390])
def test_manual_controls_follow_current_product_permission(manual_page, width):
    page, row = manual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    for allowed in (False, True):
        page.evaluate('''row => {
            aiOriginalRows = [row]; renderAiOriginalProducts(); decorateAiOriginalWorkbench();
        }''', {**row, 'can_operate': allowed})
        for name in ('length', 'width', 'height', 'net'):
            assert page.locator(f'#ai-original-{name}-1').is_enabled() == allowed
        page.locator('#ai-original-grid .ai-original-edit-button').click()
        for name in ('length', 'width', 'height', 'net'):
            assert page.locator(f'#ai-original-editor-{name}').is_enabled() == allowed
        page.locator('#ai-original-editor-dialog').get_by_role('button', name='取消', exact=True).click()


@pytest.mark.parametrize('role', ['super_admin', 'enterprise_admin'])
def test_administrators_can_edit_when_cached_row_permission_is_stale(manual_page, role):
    page, row = manual_page
    page.evaluate('''({row, role}) => {
        currentWorkbenchUser.role_key = role;
        aiOriginalRows = [{...row, can_operate: false}];
        renderAiOriginalProducts(); decorateAiOriginalWorkbench();
    }''', {'row': row, 'role': role})

    assert page.locator('#ai-original-net-1').is_enabled()
    assert page.locator('#ai-original-length-1').is_enabled()
    page.locator('#ai-original-grid .ai-original-edit-button').click()
    assert page.locator('#ai-original-editor-net').is_enabled()
    assert page.locator('#ai-original-editor-length').is_enabled()
