import pytest
from test_console_ui_browser import console_browser, console_page, ROOT


@pytest.mark.parametrize("width", [1440, 390])
def test_task_browser_selection_and_existing_multiselect(console_page, width):
    page = console_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-tasks').classList.add('active');
    }""")
    select = page.locator('#daily-task-browser-provider')
    assert select.is_visible()
    assert page.evaluate('getDailyTaskPayload().browser_provider') == 'bitbrowser'
    select.focus()
    assert select.evaluate('(el) => el === document.activeElement')
    select.select_option('ziniao')
    assert page.evaluate('getDailyTaskPayload().browser_provider') == 'ziniao'
    page.locator('input[name="daily-task-appeal-type"][value="投诉"]').locator('..').click()
    assert page.evaluate('getDailyTaskPayload().appeal_types') == ['侵权', '投诉']
    assert page.locator('#daily-task-salesperson-picker').is_visible()
    assert page.locator('#daily-task-group-picker').is_visible()
    bounds = select.bounding_box()
    assert bounds['width'] > 100
    assert bounds['x'] + bounds['width'] <= width
    # Navigate away and back through the console's existing navigation handler.
    page.evaluate("switchTab('overview'); switchTab('tasks')")
    assert page.locator('#tab-tasks').evaluate("el => el.classList.contains('active')")
    assert select.input_value() == 'ziniao'
    directory = ROOT / 'artifacts' / 'ziniao-tasks'
    directory.mkdir(parents=True, exist_ok=True)
    select.scroll_into_view_if_needed()
    page.screenshot(path=str(directory / f'tasks-{width}.png'))


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_task_scope_loads_after_status_and_searches_multiselect(console_page, width):
    import json
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/api/tasks/daily/options', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'status': 'success', 'data': {
            'salespeople': ['张三', '李四'], 'groups': ['精品一组', '精品二组'],
            'stores': [{'id': 11, 'name': '测试旗舰店', 'nickname': 'Shop Alpha'},
                       {'id': 22, 'name': '测试专营店', 'nickname': 'Shop Beta'}],
        }})))
    page.evaluate("tasksLoaded = true; switchTab('tasks')")
    page.wait_for_function("document.querySelector('#daily-task-store').options.length === 2")
    for kind, query, expected in [('salesperson', '张', ['张三']), ('group', '精品', ['精品一组', '精品二组']), ('store', 'sHoP', ['11', '22'])]:
        picker = page.locator(f'#daily-task-{kind}-picker')
        trigger = picker.locator('.market-multi-trigger')
        trigger.click()
        search = picker.locator('input[type=search]')
        search.fill('不存在')
        assert picker.locator('.zs-picker-empty').is_visible()
        search.fill(query)
        rows = picker.locator('.market-multi-option:not([hidden]) input')
        assert rows.count() == len(expected)
        search.press('Enter')
        page.keyboard.press('Space')
        for index in range(1, len(expected)):
            rows.nth(index).check()
        assert page.evaluate(f"mercadoSelectedValues(document.getElementById('daily-task-{kind}'))") == expected
        page.keyboard.press('Escape')
        assert trigger.evaluate('el => el === document.activeElement')
    payload = page.evaluate('getDailyTaskPayload()')
    assert payload['store_ids'] == [11, 22]
    assert payload['group_names'] == ['精品一组', '精品二组']
    assert payload['salespeople'] == []
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(ROOT / 'artifacts' / 'ziniao-tasks' / f'scope-{width}.png'))
    page.route('**/api/tasks/daily/options', lambda route: route.fulfill(
        status=503, content_type='application/json', body='{"status":"error","message":"测试加载失败"}'))
    page.evaluate('loadDailyTaskOptions()')
    assert '测试加载失败' in page.locator('#daily-task-options-message').inner_text()
    assert page.evaluate('getDailyTaskPayload().store_ids') == [11, 22]
