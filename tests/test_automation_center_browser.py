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
    setting_writes=[]
    retry_writes=[]
    detail_requests=[]
    fail_setting=[False]
    fail_retry=[False]
    mock_runs=[
        {'id':101,'rule_key':'rights_cleanup','subject':'测试产品 <script>bad()</script>','status':'failed','retryable':True,
         'retry_blocked_reason':'','failure_reason':'链接正在平台审核中，当前不可暂停或删除；审核结束后可重试','started_at':'2026-10-11 12:00:00',
         'detail':[{'time':'2026-10-13T12:00:00+08:00','stage':'净收益','message':'尺寸未成功，跳过净收益'},
             {'stage':'删除链接','status':'error','errors':['平台未确认链接已暂停，未继续删除','平台失败 (400): {"status":400,"message":"Listing is not modifiable"}'],'item_id':'MLM123456','result':{'links':[{'item_id':'MLM123456','status':'failed'}]},'text':'<script>bad()</script>\n第二行'}]},
        {'id':102,'rule_key':'rights_cleanup','subject':'中断的清理任务','status':'interrupted','retryable':True,
         'retry_blocked_reason':'','failure_reason':'服务重启导致执行中断','started_at':'2026-10-11 12:05:00',
         'detail':[{'stage':'删除链接','error':'服务重启导致执行中断'}]},
        {'id':103,'rule_key':'freight','subject':'已经成功的任务','status':'success','retryable':False,
         'retry_blocked_reason':'成功记录无需重试','failure_reason':'','started_at':'2026-10-11 12:10:00','detail':[]},
        {'id':104,'rule_key':'rights_reply','subject':'结果待核实的任务','status':'unknown','retryable':False,
         'retry_blocked_reason':'提交结果待核实，禁止重复执行','failure_reason':'提交结果尚未确认','started_at':'2026-10-11 12:15:00','detail':[]},
    ]
    def list_data():
        return {'settings':settings,'next_before':None,
                'runs':[{key:value for key,value in run.items() if key!='detail'} for run in mock_runs]}
    def route_handler(route):
        from urllib.parse import parse_qs,urlparse
        parsed=urlparse(route.request.url)
        assert parsed.scheme=='http' and parsed.netloc=='automation.test', route.request.url
        if parsed.path=='/automation-center':
            return route.fulfill(body=html,content_type='text/html')
        if parsed.path.startswith('/static/'):
            path=ROOT/'bit/static'/parsed.path.removeprefix('/static/')
            return route.fulfill(body=path.read_bytes(),content_type='text/css' if path.suffix=='.css' else 'text/javascript')
        assert parsed.path=='/api/automation-center', route.request.url
        query=parse_qs(parsed.query)
        assert query.get('organization_key')==['company-a']
        if route.request.method=='PATCH':
            setting_writes.append(route.request.post_data_json)
            if fail_setting[0]: return route.fulfill(status=503,json={'status':'error','message':'服务暂不可用'})
            settings.update(route.request.post_data_json)
            return route.fulfill(json={'status':'success','data':list_data()})
        if route.request.method=='POST':
            body=route.request.post_data_json
            retry_writes.append(body)
            if fail_retry[0]: return route.fulfill(status=503,json={'status':'error','message':'重试服务暂不可用'})
            count=len(body.get('run_ids') or [])
            return route.fulfill(status=202,json={'status':'success','message':'已加入重试队列','data':{
                'requested_count':count,'queued_count':count,'skipped_count':0,
                'items':[{'run_id':run_id,'status':'queued','retry_run_id':1000+run_id} for run_id in body.get('run_ids') or []]}})
        assert route.request.method=='GET'
        if query.get('run_id'):
            run_id=int(query['run_id'][0])
            detail_requests.append(run_id)
            run=next(item for item in mock_runs if item['id']==run_id)
            return route.fulfill(json={'status':'success','data':{
                'run_id':run_id,'detail':run['detail'],'failure_reason':run['failure_reason']}})
        return route.fulfill(json={'status':'success','data':list_data()})
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('**/*',route_handler)
        page.goto('http://automation.test/automation-center?organization_key=company-a')
        policy_switch=page.get_by_role('switch',name=RULES['publish_infringement'])
        assert not policy_switch.is_checked()
        policy_switch.focus()
        page.keyboard.press('Space')
        page.get_by_text('已开启，对后续普通上架生效；人工复核上架仍跳过侵权拦截。').wait_for()
        assert setting_writes.pop()=={'publish_infringement':True}
        policy_switch.uncheck()
        page.get_by_text('已关闭，后续上架不再自动检测或拦截侵权风险。').wait_for()
        assert setting_writes.pop()=={'publish_infringement':False}
        switch=page.get_by_role('switch',name=RULES['freight'])
        switch.check()
        page.get_by_text('已开启，后台将在下一轮检查时执行。').wait_for()
        assert setting_writes==[{'freight':True}]
        failed_run=page.locator('#runs details').filter(has_text='测试产品').first
        interrupted_run=page.locator('#runs details').filter(has_text='中断的清理任务').first
        success_run=page.locator('#runs details').filter(has_text='已经成功的任务').first
        unknown_run=page.locator('#runs details').filter(has_text='结果待核实的任务').first
        assert detail_requests==[]
        failed_run.locator('summary').click()
        page.get_by_text('尺寸未成功，跳过净收益',exact=False).wait_for()
        assert detail_requests==[101]
        failed_run.get_by_text('链接正在平台审核中，当前不可暂停或删除',exact=False).first.wait_for()
        assert page.locator('#runs .log-entry').count()==2
        assert page.locator('#runs .log-error').count()==2
        assert page.locator('#runs script').count()==0
        assert '\n  "status": 400' in page.locator('#runs .log-error dd').nth(1).inner_text()
        assert page.locator('#runs .log-field dt').get_by_text('结果 / 链接处理结果 / 第 1 项 / 链接编号',exact=True).count()==1
        summary=failed_run.locator('summary')
        summary.focus()
        page.keyboard.press('Enter')
        assert failed_run.get_attribute('open') is None
        page.keyboard.press('Enter')
        assert detail_requests==[101]
        assert summary.evaluate('(el)=>getComputedStyle(el).outlineStyle')!='none'
        fail_setting[0]=True
        switch.uncheck()
        page.get_by_text('服务暂不可用').wait_for()
        assert switch.is_checked()
        with page.expect_response(lambda response: response.request.method=='GET' and '/api/automation-center?' in response.url):
            page.get_by_role('button',name='刷新',exact=True).click()
        assert page.locator('#message').inner_text()==''
        failed_run.locator('summary').click()
        page.get_by_text('尺寸未成功，跳过净收益',exact=False).wait_for()
        destination=ROOT/'artifacts/automation-log-readability-20261013'
        destination.mkdir(parents=True,exist_ok=True)
        for width in (1440,1024,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('#runs .log-field dd').first.evaluate('(el)=>getComputedStyle(el).whiteSpace')=='pre-wrap'
            field_positions=page.locator('#runs .log-field').evaluate_all('''fields => fields.map(field => {
              const term=field.querySelector('dt').getBoundingClientRect();
              const value=field.querySelector('dd').getBoundingClientRect();
              return {termX:term.x,valueX:value.x,termY:term.y,valueY:value.y};
            })''')
            assert field_positions
            assert all(abs(item['termY']-item['valueY'])<3 and item['termX']<item['valueX'] for item in field_positions)
            page.screenshot(path=str(destination/f'center-{width}.png'),full_page=True)

        for blocked,reason in ((success_run,'成功记录无需重试'),(unknown_run,'提交结果待核实，禁止重复执行')):
            checkbox=blocked.get_by_role('checkbox')
            retry=blocked.get_by_role('button',name='执行重试',exact=True)
            assert checkbox.count()==1 and checkbox.is_disabled()
            assert retry.count()==1 and retry.is_disabled()
            explanation=' '.join(filter(None,(blocked.inner_text(),checkbox.get_attribute('title'),retry.get_attribute('title'))))
            assert reason in explanation

        single_retry=failed_run.get_by_role('button',name='执行重试',exact=True)
        single_retry.focus()
        assert single_retry.evaluate('(el)=>el===document.activeElement && getComputedStyle(el).outlineStyle!=="none"')
        with page.expect_response(lambda response: response.request.method=='POST' and '/api/automation-center?' in response.url) as single_response:
            page.keyboard.press('Enter')
        assert single_response.value.status==202
        assert retry_writes[-1]=={'run_ids':[101]}
        page.get_by_text('已加入重试队列 1 条',exact=True).wait_for()

        failed_choice=failed_run.get_by_role('checkbox',name='选择重试：测试产品 <script>bad()</script>',exact=True)
        interrupted_choice=interrupted_run.get_by_role('checkbox',name='选择重试：中断的清理任务',exact=True)
        failed_choice.focus()
        page.keyboard.press('Space')
        interrupted_choice.check()
        batch_retry=page.get_by_role('button',name='重试所选',exact=False)
        assert failed_choice.is_checked() and interrupted_choice.is_checked() and batch_retry.is_enabled()
        fail_retry[0]=True
        with page.expect_response(lambda response: response.request.method=='POST' and '/api/automation-center?' in response.url) as failed_response:
            batch_retry.click()
        assert failed_response.value.status==503
        page.get_by_text('重试服务暂不可用',exact=True).wait_for()
        assert retry_writes[-1]=={'run_ids':[101,102]}
        assert failed_choice.is_checked() and interrupted_choice.is_checked() and batch_retry.is_enabled()

        fail_retry[0]=False
        with page.expect_response(lambda response: response.request.method=='POST' and '/api/automation-center?' in response.url) as batch_response:
            batch_retry.click()
        assert batch_response.value.status==202
        assert retry_writes[-1]=={'run_ids':[101,102]}
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
      function hasWorkbenchPermission(){return true;}
      function enterpriseScopedUrl(path){return path+'&organization_key=company-a';}
    </script><script>'''+switch+'</script>'
    requests=[]
    def handler(route):
        url=urlparse(route.request.url)
        assert url.scheme=='http' and url.netloc=='automation.test', route.request.url
        if url.path=='/': return route.fulfill(body=shell,content_type='text/html')
        if url.path=='/automation-center': return route.fulfill(body=embedded,content_type='text/html')
        if url.path.startswith('/static/'):
            path=ROOT/'bit'/url.path.lstrip('/')
            return route.fulfill(body=path.read_bytes(),content_type='text/css' if path.suffix=='.css' else 'text/javascript')
        assert url.path=='/api/automation-center', route.request.url
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
        page.route('**/*',handler)
        page.goto('http://automation.test/')
        page.get_by_role('button',name='自动化执行中心').click()
        frame=page.frame_locator('#automation-center-frame')
        frame.get_by_text('全部记录',exact=True).wait_for()
        assert page.url=='http://automation.test/' and len(page.context.pages)==1
        assert frame.get_by_role('link',name='返回工作台').count()==0
        frame.get_by_label('业务员',exact=True).fill('张三')
        frame.get_by_label('店铺',exact=True).fill('墨西哥一店')
        frame.get_by_text('高级筛选',exact=True).click()
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
        for width in (1440,1024,390):
            page.set_viewport_size({'width':width,'height':1000})
            assert frame.locator('body').evaluate('(el)=>el.scrollWidth <= innerWidth')
            page.screenshot(path=str(ROOT/f'artifacts/automation-log-readability-20261013/embedded-{width}.png'),full_page=True)
        frame.get_by_role('button',name='重置',exact=True).click()
        frame.get_by_text('全部记录',exact=True).wait_for()
        assert requests[-1]=={'organization_key':'company-a'}
        assert frame.get_by_label('业务员',exact=True).input_value()==''
        assert not errors
        browser.close()
