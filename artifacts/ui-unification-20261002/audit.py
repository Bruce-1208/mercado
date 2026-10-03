"""Read-only offline UI audit. All HTTP requests are fulfilled from fixtures."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from collections import Counter
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
env = Environment(loader=FileSystemLoader(ROOT / 'bit/templates'))
html = env.get_template('index.html').render(
    current_user={'username': 'audit', 'display_name': '界面审查', 'is_admin': True},
    can_change_task_workers=False, runtime_role='server',
    url_for=lambda endpoint, filename: '/static/' + filename)
report = {'mode': 'offline mock APIs; no production traffic', 'viewports': []}
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    for width, height in [(1440, 1000), (1280, 800), (390, 844)]:
        page = browser.new_page(viewport={'width': width, 'height': height})
        calls, errors = [], []
        def route(r):
            path = urlsplit(r.request.url).path
            if path == '/':
                r.fulfill(body=html, content_type='text/html')
            elif path.startswith('/static/') and (ROOT / 'bit' / path.lstrip('/')).is_file():
                r.fulfill(path=str(ROOT / 'bit' / path.lstrip('/')))
            else:
                calls.append(path)
                r.fulfill(content_type='application/json', body=json.dumps({
                    'status': 'success', 'success': True, 'data': [], 'items': [],
                    'stores': [], 'groups': [], 'total': 0}))
        page.route('**/*', route)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("""window.auditLongTasks=[];new PerformanceObserver(list=>{
            for(const e of list.getEntries())window.auditLongTasks.push({start:e.startTime,duration:e.duration});
        }).observe({type:'longtask',buffered:true});""")
        page.goto('http://audit.test/', wait_until='load')
        page.wait_for_timeout(500)
        item = page.evaluate("""() => ({
            width:innerWidth, domNodes:document.querySelectorAll('*').length,
            tabs:document.querySelectorAll('.tab-page').length,
            duplicateIds:[...new Set([...document.querySelectorAll('[id]')].map(e=>e.id))].filter(id=>document.querySelectorAll('[id="'+id+'"]').length>1),
            navigation:performance.getEntriesByType('navigation')[0].toJSON(),
            longTasks:window.auditLongTasks,
            stylesheets:[...document.styleSheets].length,
            resources:performance.getEntriesByType('resource').map(e=>({name:e.name,decodedBodySize:e.decodedBodySize}))
        })""")
        item['initialRequests'] = dict(Counter(calls))
        item['pages'] = []
        for tab in ['overview', 'orders', 'mercado-products', '1688-products', 'ai-original-products', 'inventory']:
            page.evaluate("""tab => {
                if (tab === 'mercado-products') mountMercadoListModule('products');
                document.querySelectorAll('.tab-page').forEach(el=>el.classList.toggle('active',el.id==='tab-'+tab));
            }""", tab)
            page.wait_for_timeout(100)
            metrics = page.evaluate("""() => ({
                scrollWidth:document.documentElement.scrollWidth,
                viewportWidth:innerWidth,
                visibleButtons:[...document.querySelectorAll('.tab-page.active button')].filter(e=>e.getClientRects().length).length,
                overflow:[...document.querySelectorAll('.tab-page.active *')].filter(e=>e.getClientRects().length&&e.getBoundingClientRect().right>innerWidth+5).slice(0,8).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right}))
            })""")
            item['pages'].append({'tab':tab, **metrics})
            if width in [1440,390] and tab in ['orders','mercado-products','1688-products']:
                page.screenshot(path=str(OUT / f'{tab}-{width}.png'))
        # Reproduce background polling after navigating away, with a mocked running job.
        if width == 1440:
            page.route('**/api/store-links/sync/status', lambda r: (calls.append('/poll-probe'), r.fulfill(content_type='application/json',body=json.dumps({'status':'success','data':{'running':True}}))))
            page.evaluate("loadStoreLinkSyncStatus()")
            page.wait_for_timeout(3400)
            item['storeLinkPollWhileOtherTabActive'] = calls.count('/poll-probe')
        item['errors'] = errors
        report['viewports'].append(item)
        page.close()
    browser.close()
(OUT / 'measurements.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
for r in report['viewports']:
    print(json.dumps({k:r[k] for k in ['width','domNodes','tabs','duplicateIds','longTasks','initialRequests','pages','errors']},ensure_ascii=False))
