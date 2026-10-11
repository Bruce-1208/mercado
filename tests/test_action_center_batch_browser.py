"""Offline browser coverage: actual templates/scripts, no business writes."""
import json
from pathlib import Path

import pytest

from flask import Flask, render_template
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def test_each_today_section_supports_multi_select_and_status_updates():
    app = Flask(__name__, template_folder=str(ROOT/'bit/templates'), static_folder=str(ROOT/'bit/static'))
    with app.test_request_context('/'):
        html = render_template('mercado_action_center.html', current_user=dict(id=1, organization_key='org', username='test', role_key='operator'), can_execute=True)
    rows = [dict(id=i, token_id=7, title=f'任务 {i}', topic='items' if i>22 else 'purchase_tracking_missing_48h', status='open') for i in range(1,25)]
    data = dict(rows=rows, events=[dict(id=1, token_id=7, status='retry', work_ref='event', work_status='open')], rights_holder_replies=[dict(token_id=7, infraction_id='case', work_ref='rights', work_status='open')])
    post = dict(rows=[dict(token_id=7, claim_id='P', text='Olá', text_zh='请回复', work_ref='post', work_status='open')])
    employees = [dict(id=i, assignee_id=1, title=f'员工任务 {i}', is_completed=False) for i in (1,2)]
    writes, errors = [], []
    def route_handler(route):
        request = route.request
        url = request.url.split('today.test',1)[1]
        if url == '/mercado/today':
            return route.fulfill(body=html, content_type='text/html')
        if url.startswith('/static/'):
            return route.fulfill(body=(ROOT/'bit/static'/url.split('?')[0].removeprefix('/static/')).read_bytes(), content_type='text/javascript')
        if '/batch-status' in url:
            body = request.post_data_json
            writes.append(body)
            for item in body['items']:
                if 'id' in item:
                    next(row for row in rows if row['id']==item['id'])['status']=body['status']
                else:
                    for row in data['events']+data['rights_holder_replies']+post['rows']:
                        if row['work_ref']==item['ref']: row['work_status']=body['status']
            result = dict(results=[dict(item=item,task={}) for item in body['items']],errors=[])
        elif url.startswith('/api/employee-tasks/') and request.method=='PATCH':
            writes.append(request.post_data_json)
            task_id=int(url.split('/api/employee-tasks/')[1].split('?')[0])
            next(row for row in employees if row['id']==task_id).update(request.post_data_json)
            result={}
        elif url.startswith('/api/employee-tasks'):
            result=dict(rows=employees,actor_id=1)
        elif url.startswith('/api/mercado/today/tasks'): result=data
        elif url.startswith('/api/mercado/today/claims'): result=post
        else: result={}
        route.fulfill(json=dict(status='success',data=result))
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page()
        page.on('pageerror',lambda exc:errors.append(str(exc)))
        page.route('http://today.test/**',route_handler)
        page.goto('http://today.test/mercado/today')
        page.locator('#purchase-tasks-batch').wait_for()
        page.locator('#purchase-tasks [data-batch-choice] input').first.check()
        page.locator('#purchase-tasks-pagination button').last.click()
        page.locator('#purchase-tasks [data-batch-choice] input').first.check()
        assert page.locator('#purchase-tasks-batch [data-count]').inner_text()=='已选 2 条'
        for key in ['purchase-tasks','tasks','claim-tasks','rights-cases','events']:
            bar=page.locator('#'+key+'-batch')
            if key!='purchase-tasks': bar.locator('[data-all]').check()
            bar.locator('[data-status]').select_option('resolved')
            bar.locator('[data-save]').click()
            page.wait_for_function('(key)=>document.querySelector(`#${key}-batch [data-result]`).textContent.includes("已更新")',arg=key)
        assert {item['id'] for item in writes[0]['items']}=={1,21}
        page.locator('#employee-select-all').check()
        page.locator('#employee-batch-status').select_option('true')
        page.locator('#employee-batch-save').click()
        page.wait_for_function('document.querySelector("#employee-error").textContent.includes("已更新 2 条")')
        assert all(row['is_completed'] for row in employees)
        assert not errors
        browser.close()


@pytest.mark.parametrize('width', [1440,390])
def test_claims_replace_post_sale_and_keep_chinese_on_every_page(width):
    app=Flask(__name__, template_folder=str(ROOT/'bit/templates'), static_folder=str(ROOT/'bit/static'))
    with app.test_request_context('/'):
        html=render_template('mercado_action_center.html', current_user=dict(id=1, organization_key='org', username='test', role_key='operator'), can_execute=False)
    rows=[dict(token_id=7, store_name='测试店铺', claim_id=str(i), order_id='200', latest_at='2026-10-10T01:00:00Z', text='Olá, preciso de ajuda.', text_zh='您好，我需要帮助。') for i in range(25)]
    urls,errors=[],[]
    def read(route):
        url=route.request.url.split('today.test',1)[1]
        urls.append(url)
        if url=='/mercado/today': return route.fulfill(body=html, content_type='text/html')
        if url.startswith('/static/'):
            return route.fulfill(body=(ROOT/'bit/static'/url.split('?')[0].removeprefix('/static/')).read_bytes(),content_type='text/javascript')
        result=dict(rows=rows) if url.startswith('/api/mercado/today/claims') else dict(rows=[])
        route.fulfill(json=dict(status='success',data=result))
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport=dict(width=width,height=1000))
        page.on('pageerror',lambda exc:errors.append(str(exc)))
        page.route('http://today.test/**',read)
        page.goto('http://today.test/mercado/today')
        page.wait_for_function("document.getElementById('claim-count').textContent==='25'")
        assert page.locator('#post-sale-tasks').count()==0
        assert not any('/today/post-sale' in url for url in urls)
        assert page.locator('#claim-tasks article').count()==20
        assert '中文：您好，我需要帮助。' in page.locator('#claim-tasks article').first.inner_text()
        assert '原文：Olá, preciso de ajuda.' in page.locator('#claim-tasks article').first.inner_text()
        page.locator('#claim-tasks-pagination button').last.click()
        assert page.locator('#claim-tasks article').count()==5
        assert '中文：您好，我需要帮助。' in page.locator('#claim-tasks article').last.inner_text()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        folder=ROOT/'artifacts/today-claims-20261010'
        folder.mkdir(parents=True,exist_ok=True)
        page.locator('#claim-tasks').screenshot(path=str(folder/f'claims-{width}.png'))
        assert not errors
        browser.close()
