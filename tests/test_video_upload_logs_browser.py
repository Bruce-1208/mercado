import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from test_console_ui_browser import console_browser, console_page


def test_upload_logs_display_fields_paginate_filter_and_fit_mobile(console_page):
    page = console_page
    requests = []
    rows = [{
        'link_id': 42, 'item_id': 'MLM123', 'title': '折叠收纳盒', 'salesperson': '小张',
        'uploaded_at': '2026-10-10T02:30:00Z', 'store_name': '泽顺旗舰店', 'group_name': '墨西哥一组',
        'token_id': 7, 'site_id': 'MLM', 'thumbnail_url': 'https://example.com/main.png',
        'permalink': 'https://example.com/product', 'job_id': 'video-1', 'video_name': '商品展示视频',
        'status': 'UNDER_REVIEW', 'status_label': '已提交待审核', 'clip_uuid': 'clip-123',
    }]
    def serve(route):
        params = parse_qs(urlsplit(route.request.url).query)
        requests.append(params)
        current = int(params.get('page', ['1'])[0])
        data = rows if current == 1 else [{**rows[0], 'status': 'failed', 'status_label': '上传失败', 'message': '店铺授权失效'}]
        route.fulfill(content_type='application/json', body=json.dumps({
            'status': 'success', 'data': {'rows': data, 'total': 21, 'page': current, 'total_pages': 2},
        }))
    page.route('**/api/ai-videos/upload-logs?*', serve)
    page.route('https://example.com/main.png', lambda route: route.fulfill(
        body='<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56"><rect width="56" height="56" fill="#dbeafe"/></svg>', content_type='image/svg+xml'))
    page.evaluate('''() => {
        document.querySelectorAll('.tab-page').forEach(el => el.classList.remove('active'));
        document.getElementById('tab-ai-video').classList.add('active');
        VideoUploadLogs.load(true);
    }''')
    body = page.locator('#video-upload-log-rows')
    page.wait_for_function("document.querySelector('#video-upload-log-rows').textContent.includes('小张')")
    for label in ('折叠收纳盒', 'MLM123', '链接编号 42', '小张', '泽顺旗舰店', '墨西哥一组', '商品展示视频', '10:30:00', '已提交待审核', 'clip-123'):
        assert label in body.inner_text()
    assert body.locator('img').get_attribute('src') == 'https://example.com/main.png'
    assert body.locator('a').first.get_attribute('href') == 'https://example.com/product'
    assert page.locator('#video-upload-log-prev').is_disabled()
    page.locator('#video-upload-log-next').click()
    page.wait_for_function("document.querySelector('#video-upload-log-page').textContent.includes('第 2')")
    assert '店铺授权失效' in body.inner_text()
    assert page.locator('#video-upload-log-next').is_disabled()
    page.locator('#video-upload-log-filters [name="salesperson"]').fill('小张')
    page.locator('#video-upload-log-filters [name="search"]').fill('墨西哥一组')
    page.locator('#video-upload-log-filters [name="from"]').fill('2026-10-10')
    page.locator('#video-upload-log-filters [name="to"]').fill('2026-10-10')
    page.get_by_role('button', name='查询日志', exact=True).click()
    page.wait_for_function("document.querySelector('#video-upload-log-page').textContent.includes('第 1')")
    assert requests[-1]['salesperson'] == ['小张']
    assert requests[-1]['uploaded_from'] == ['2026-10-09T16:00:00.000Z']
    assert requests[-1]['uploaded_before'] == ['2026-10-10T16:00:00.000Z']
    folder = Path(__file__).resolve().parents[1] / 'artifacts/video-upload-logs-20261010'
    folder.mkdir(exist_ok=True)
    page.locator('#video-upload-logs').screenshot(path=str(folder / 'logs-1440.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#video-upload-logs').scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert page.locator('.video-upload-log-table').evaluate('el => el.scrollWidth > el.clientWidth')
    page.locator('#video-upload-logs').screenshot(path=str(folder / 'logs-390.png'))
