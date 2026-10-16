from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json, re
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BASE = 'http://127.0.0.1:5000'
results = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    response = page.goto(BASE + '/static/beginner-guide.html')
    assert response.status == 200
    assert page.locator('[data-chapter]').count() == 8
    assert page.locator('details.faq').count() == 7
    assert page.locator('#progress-text').inner_text() == '已完成 0 / 6'
    page.locator('.checklist input').first.check()
    page.reload()
    assert page.locator('.checklist input').first.is_checked()
    assert page.locator('#progress-percent').inner_text() == '17%'
    for checkbox in page.locator('.checklist input').all():
        checkbox.check()
    assert '入门清单已完成' in page.locator('#progress-text').inner_text()
    page.locator('#reset-progress').click()
    assert not any(check.is_checked() for check in page.locator('.checklist input').all())
    page.locator('#guide-search').fill('采购')
    assert page.locator('#orders').is_visible()
    assert not page.locator('#stores').is_visible()
    page.locator('#guide-search').fill('不存在的查询xyz')
    assert page.locator('#no-results').is_visible()
    page.locator('.sidebar a[href="#stores"]').click()
    assert page.locator('#stores').is_visible()
    assert page.locator('#guide-search').input_value() == ''
    page.locator('#guide-search').fill('上架失败')
    assert page.locator('#faq details[open]').count() == 1
    page.locator('#clear-search').click()
    assert page.locator('#faq details[open]').count() == 0
    page.locator('#faq details').first.locator('summary').click()
    assert page.locator('#faq details').first.get_attribute('open') is not None
    page.evaluate("window.dispatchEvent(new Event('beforeprint'))")
    assert page.locator('details[open]').count() == page.locator('details').count()
    page.emulate_media(media='print')
    page.pdf(path=str(OUT / 'beginner-guide-print.pdf'), format='A4', print_background=True, margin={'top':'12mm','bottom':'12mm','left':'12mm','right':'12mm'})
    page.emulate_media(media='screen')
    page.evaluate("window.dispatchEvent(new Event('afterprint'))")
    assert page.locator('details[open]').count() == 1
    template = (ROOT / 'bit/templates/index.html').read_text()
    allowed_tabs = set(re.findall(r'data-tab="([^"]+)"', template))
    links = page.locator('a[href]').evaluate_all('(els) => els.map(el => el.getAttribute("href"))')
    for link in links:
        if link.startswith('/?tab='):
            assert parse_qs(urlsplit(link).query)['tab'][0] in allowed_tabs, link
        if link.startswith('#'):
            assert page.locator(link).count() == 1, link
    results.append('8 chapters, 7 FAQs, progress save/reload/reset, search/empty state, anchors, print restore and all tab destinations passed')
    for width, height in [(1440,1100),(1024,900),(390,844),(320,780)]:
        page.set_viewport_size({'width':width,'height':height})
        page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
        page.wait_for_timeout(200)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.screenshot(path=str(OUT / f'guide-{width}.png'), full_page=width in (1440,390))
    results.append('No horizontal overflow at 1440, 1024, 390 and 320 pixels')
    # Both freshly rendered and cached templates get exactly one entry per navigation surface.
    script = (ROOT / 'bit/static/console-navigation.js').read_text()
    for existing in (False, True):
        link = '<a class="tab-button" href="/static/beginner-guide.html">新手教程</a>' if existing else ''
        page.set_content('<div class="topbar-tools"></div><aside class="sidebar"><div class="sidebar-scroll"><button class="tab-button active" data-tab="overview">总览</button>' + link + '</div></aside>')
        page.add_script_tag(content=script)
        assert page.locator('.sidebar a[href="/static/beginner-guide.html"]').count() == 1
        assert page.locator('.topbar-tools a[href="/static/beginner-guide.html"]').count() == 1
        assert page.locator('[data-group="work"] a').inner_text() == '新手教程'
    results.append('Guide entries work for fresh and cached templates without duplicate links')
    assert not errors, errors
    nojs = browser.new_context(java_script_enabled=False)
    fallback = nojs.new_page()
    fallback.goto(BASE + '/static/beginner-guide.html')
    assert fallback.locator('#orders').is_visible()
    assert not fallback.locator('#search-bar').is_visible()
    nojs.close()
    blocked = browser.new_context()
    blocked.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('Storage blocked');}})")
    fallback = blocked.new_page()
    fallback.goto(BASE + '/static/beginner-guide.html')
    fallback.locator('.checklist input').first.check()
    assert '当前页面有效' in fallback.locator('#storage-note').inner_text()
    assert '1 / 6' in fallback.locator('#progress-text').inner_text()
    blocked.close()
    results.append('Readable without JavaScript; blocked browser storage degrades gracefully; no JavaScript errors')
    browser.close()
(OUT / 'verification.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
print('\n'.join(results))
