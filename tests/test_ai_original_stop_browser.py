"""Stop control in the real console, with every API request mocked."""
import json

import pytest
from test_console_ui_browser import console_browser, console_page  # noqa: F401


@pytest.mark.parametrize('width', [1440, 390])
def test_stop_button_keyboard_pending_and_finished_states(console_page, width):
    page = console_page
    page.set_viewport_size({'width': width, 'height': 1000})
    requests = []
    state = {'running': True, 'status': 'stopping', 'stop_requested': True,
             'requested_count': 2, 'completed_count': 1,
             'message': '正在结束任务，等待当前操作返回并保存结果',
             'results': [{'product_id': 1, 'status': 'completed'}]}
    def stop(route):
        requests.append(route.request.method)
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': state}))
    page.route('**/api/ai-original-products/process/stop', stop)
    page.route('**/api/ai-original-products/process/status', lambda route:
        route.fulfill(content_type='application/json', body=json.dumps({'status': 'success', 'data': state})))
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-original-products').classList.add('active');
        renderAiOriginalTaskStatus({status:'idle', running:false});
    }''')
    button = page.locator('#ai-original-stop')
    assert button.is_disabled()
    page.evaluate("renderAiOriginalTaskStatus({status:'running', running:true, requested_count:2})")
    assert button.is_enabled()
    button.focus()
    assert button.evaluate('(el) => el === document.activeElement')
    button.press('Enter')
    page.wait_for_function("document.getElementById('ai-original-stop').textContent === '正在结束…'")
    assert button.is_disabled()
    assert requests == ['POST']
    page.evaluate('stopAiOriginalTask()')
    assert requests == ['POST']
    page.evaluate("renderAiOriginalTaskStatus({status:'stopped', running:false, stop_requested:true, completed_count:1})")
    assert button.text_content() == '结束任务'
    assert button.is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate('if (aiOriginalTaskTimer) clearTimeout(aiOriginalTaskTimer)')
