from pathlib import Path

from flask import Flask, render_template
from playwright.sync_api import sync_playwright

from bit.automation_center import RULES, REPLY_TEXT

ROOT=Path(__file__).resolve().parents[1]


def test_switches_logs_mobile_and_errors():
    app=Flask(__name__,template_folder=str(ROOT/'bit/templates'),static_folder=str(ROOT/'bit/static'))
    with app.test_request_context('/'):
        html=render_template('automation_center.html',rules=RULES,reply_text=REPLY_TEXT)
    settings={key:False for key in RULES}
    writes=[]
    fail=[False]
    def route_handler(route):
        url=route.request.url.split('automation.test',1)[1]
        if url.startswith('/automation-center'):
            return route.fulfill(body=html,content_type='text/html')
        if url.startswith('/static/'):
            path=ROOT/'bit/static'/url.split('?')[0].removeprefix('/static/')
            return route.fulfill(body=path.read_bytes(),content_type='text/css' if path.suffix=='.css' else 'text/javascript')
        assert 'organization_key=company-a' in url
        if route.request.method=='PATCH':
            writes.append(route.request.post_data_json)
            if fail[0]: return route.fulfill(status=503,json={'status':'error','message':'服务暂不可用'})
            settings.update(route.request.post_data_json)
        return route.fulfill(json={'status':'success','data':{'settings':settings,'next_before':None,'runs':[
            {'rule_key':'freight','subject':'测试产品 <script>bad()</script>','status':'failed','started_at':'2026-10-11 12:00:00','detail':[{'stage':'净收益','message':'尺寸未成功，跳过净收益'}]}
        ]}})
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('http://automation.test/**',route_handler)
        page.goto('http://automation.test/automation-center?organization_key=company-a')
        switch=page.get_by_role('switch',name=RULES['freight'])
        switch.check()
        page.get_by_text('已开启，后台将在下一轮检查时执行。').wait_for()
        assert writes==[{'freight':True}]
        page.locator('#runs summary').click()
        assert page.get_by_text('尺寸未成功，跳过净收益',exact=False).is_visible()
        fail[0]=True
        switch.uncheck()
        page.get_by_text('服务暂不可用').wait_for()
        assert switch.is_checked()
        destination=ROOT/'artifacts/automation-center'
        destination.mkdir(parents=True,exist_ok=True)
        for width in (1440,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(destination/f'center-{width}.png'),full_page=True)
        assert errors==[]
        browser.close()


def test_workbench_embedded_navigation_and_log_filter_pagination():
    import re
    from urllib.parse import urlparse, parse_qs
    source=(ROOT/'bit/templates/index.html').read_text()
    button=re.search(r'<button[^>]*data-tab="automation-center".*?</button>',source).group()
    panel=re.search(r'<div class="tab-page" id="tab-automation-center">.*?</div>',source,re.S).group()
    start=source.index('        function switchTab(tabName) {')
    switch=source[start:source.index('\n        function setStatus',start)]
    app=Flask(__name__,template_folder=str(ROOT/'bit/templates'),static_folder=str(ROOT/'bit/static'))
    with app.test_request_context('/'):
        embedded=render_template('automation_center.html',rules=RULES,reply_text=REPLY_TEXT,embedded=True)
    shell='<meta charset="utf-8"><style>.tab-page{display:none}.tab-page.active{display:block}</style>'+button+panel+'''<script>
      function stopStoreLinkStatusPolling(){} function syncWorkspaceTabs(){}
      function enterpriseScopedUrl(path){return path+'&organization_key=company-a';}
    </script><script>'''+switch+'</script>'
    requests=[]
    def handler(route):
        url=urlparse(route.request.url)
        if url.path=='/': return route.fulfill(body=shell,content_type='text/html')
        if url.path=='/automation-center': return route.fulfill(body=embedded,content_type='text/html')
        if url.path.startswith('/static/'):
            path=ROOT/'bit'/url.path.lstrip('/')
            return route.fulfill(body=path.read_bytes(),content_type='text/css' if path.suffix=='.css' else 'text/javascript')
        query={key:values[0] for key,values in parse_qs(url.query).items()}
        requests.append(query)
        filtered=query.get('status')=='failed'
        route.fulfill(json={'status':'success','data':{'settings':{key:False for key in RULES},'next_before':40 if filtered and not query.get('before') else None,
            'runs':[{'rule_key':'freight','subject':'筛选记录' if filtered else '全部记录','status':'failed','started_at':'2026-10-11 12:00:00',
                     'detail':[{'salesperson':'张三','store_name':'墨西哥一店','message':'尺寸已回填'}]}]}})
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('http://automation.test/**',handler)
        page.goto('http://automation.test/')
        page.get_by_role('button',name='自动化执行中心').click()
        frame=page.frame_locator('#automation-center-frame')
        frame.get_by_text('全部记录',exact=True).wait_for()
        assert page.url=='http://automation.test/' and len(page.context.pages)==1
        assert frame.get_by_role('link',name='返回工作台').count()==0
        frame.get_by_label('业务员',exact=True).fill('张三')
        frame.get_by_label('店铺',exact=True).fill('墨西哥一店')
        frame.get_by_label('执行类型').select_option('freight')
        frame.get_by_label('执行状态').select_option('failed')
        frame.get_by_label('开始时间（北京时间）').fill('2026-10-10T00:00')
        frame.get_by_label('结束时间（北京时间）').fill('2026-10-11T23:59')
        frame.get_by_role('button',name='筛选日志').click()
        frame.get_by_text('筛选记录',exact=True).wait_for()
        expected={'organization_key':'company-a','salesperson':'张三','store':'墨西哥一店','type':'freight','status':'failed','date_from':'2026-10-10T00:00','date_to':'2026-10-11T23:59'}
        assert requests[-1]==expected
        frame.get_by_role('button',name='加载更早日志').click()
        frame.get_by_text('已显示 2 条筛选结果').wait_for()
        assert requests[-1]=={**expected,'before':'40'}
        frame.get_by_role('button',name='刷新',exact=True).click()
        frame.get_by_text('已显示 1 条筛选结果').wait_for()
        assert requests[-1]==expected
        for width in (1440,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert frame.locator('body').evaluate('(el)=>el.scrollWidth <= innerWidth')
            page.screenshot(path=str(ROOT/f'artifacts/automation-center/embedded-{width}.png'),full_page=True)
        frame.get_by_role('button',name='重置',exact=True).click()
        frame.get_by_text('全部记录',exact=True).wait_for()
        assert requests[-1]=={'organization_key':'company-a'}
        assert frame.get_by_label('业务员',exact=True).input_value()==''
        assert not errors
        browser.close()
