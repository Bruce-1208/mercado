from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import pytest
from flask import Flask,render_template
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('width',[1440,390])
def test_checkbox_multiselect_search_restore_first_requests_and_reset_pages(width):
    account='["org", 1]'
    storage_key=f'zeshun-today-filters:v1:{account}'
    app=Flask(__name__,template_folder=str(ROOT/'bit/templates'),static_folder=str(ROOT/'bit/static'))
    with app.test_request_context('/'):
        html=render_template('mercado_action_center.html',current_user=dict(id=1,organization_key='org',username='test',role_key='operator'),can_execute=False)
    requests,errors=[],[]
    options=dict(salesperson=['张三','李四','王五'],group_name=['一组','二组，海外','三组'])
    def read(route):
        url=urlsplit(route.request.url)
        if url.path=='/mercado/today': return route.fulfill(body=html,content_type='text/html')
        if url.path.startswith('/static/'):
            return route.fulfill(body=(ROOT/'bit/static'/url.path.removeprefix('/static/')).read_bytes(),content_type='text/css' if url.path.endswith('.css') else 'text/javascript')
        params=parse_qs(url.query,keep_blank_values=True)
        requests.append((url.path,params))
        result=dict(rows=[])
        if url.path.endswith('/tasks'):
            result.update(events=[],filter_options=options,store_pagination=dict(page=int(params.get('store_page',['1'])[0]),page_size=20,total=40,pages=2))
        route.fulfill(json=dict(status='success',data=result))
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport=dict(width=width,height=1000))
        # Existing string preferences migrate to one-element selections.
        page.add_init_script(f"if(!localStorage.getItem({storage_key!r})) localStorage.setItem({storage_key!r},JSON.stringify({{salesperson:'张三',group_name:'一组'}}));")
        page.on('pageerror',lambda exc:errors.append(str(exc)))
        page.route('http://today.test/**',read)
        page.goto('http://today.test/mercado/today')
        people=page.locator('[data-filter="salesperson"]')
        groups=page.locator('[data-filter="group_name"]')
        page.wait_for_function("document.getElementById('salesperson').options.length===4")
        initial=[q for path,q in requests if path in ['/api/mercado/today/tasks','/api/mercado/today/claims']]
        assert len(initial)==2
        assert all(q['salesperson']==['张三'] and q['group_name']==['一组'] for q in initial)
        page.locator('#store-pagination button').last.click()
        page.wait_for_function('storePage===2')
        people.locator('summary').click()
        people.get_by_role('checkbox',name='李四',exact=True).check()
        page.wait_for_function('storePage===1')
        groups.locator('summary').click()
        groups.get_by_role('searchbox').fill('海外')
        assert groups.get_by_role('checkbox').count()==1
        groups.get_by_role('checkbox',name='二组，海外',exact=True).check()
        page.wait_for_function("filterValues('group_name').length===2")
        groups.get_by_role('searchbox').fill('')
        assert groups.get_by_role('checkbox',name='一组',exact=True).is_checked()
        page.wait_for_function("document.getElementById('refresh').disabled===false")
        for path in ['/api/mercado/today/tasks','/api/mercado/today/claims']:
            query=next(q for p,q in reversed(requests) if p==path)
            assert set(query['salesperson'])=={'张三','李四'}
            assert set(query['group_name'])=={'一组','二组，海外'}
            assert 'store_page' not in query
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        folder=ROOT/'artifacts/today-multi-filters-20261011'
        folder.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(folder/f'filters-{width}.png'))
        page.evaluate('sessionStorage.clear()')
        requests.clear()
        page.reload()
        page.wait_for_function("document.getElementById('salesperson').options.length===4")
        initial=[q for path,q in requests if path in ['/api/mercado/today/tasks','/api/mercado/today/claims']]
        assert len(initial)==2
        assert all(set(q['salesperson'])=={'张三','李四'} and set(q['group_name'])=={'一组','二组，海外'} for q in initial)
        groups.locator('summary').click()
        groups.get_by_role('button',name='全部店铺组',exact=True).click()
        page.wait_for_function("filterValues('group_name').length===0")
        people.locator('summary').click()
        people.get_by_role('button',name='全部业务员',exact=True).click()
        page.wait_for_function("filterValues('salesperson').length===0")
        assert not errors
        browser.close()
