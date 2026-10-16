"""Render actual standalone templates with entirely mocked network responses."""
import sys,json,time
from pathlib import Path
from urllib.parse import urlsplit
from collections import Counter
ROOT=Path('/private/tmp/mercado-performance-audit-20261016');sys.path.insert(0,str(ROOT))
from scripts.audit_console_performance import browser_path,payload
from bit.automation_center import RULES, REPLY_TEXT
from jinja2 import Environment,FileSystemLoader
from playwright.sync_api import sync_playwright
TEMPLATES=['ai_weight_price','infraction_dashboard','product_diagnosis','clearance_alerts','mercado_promotions','price_competition','size_charts','automation_center','account_billing','ai_billing','print_wallet','employee_tasks','integration_settings','account_profile','login','register']
def body(path):
 if path=='/api/ai-weight-price/status':return {'running':False,'login':{'confirmed':False},'counts':{},'quota':{'today':0},'computer':'offline','storage':'mock','execution_identity':{}}
 if path=='/api/ai-weight-price/config':return {'selectors':{},'phrases':[],'sku_price_mode':'selected_variant','daily_limit':25}
 if path in ('/api/ai-weight-price/owners','/api/ai-weight-price/categories'):return {'options':[],'meta':{}}
 if path=='/api/ai-weight-price/logs':return []
 if path in ('/api/ai-weight-price/tasks','/api/ai-weight-price/run-items'):return {'rows':[],'total':0}
 return {'status':'success','success':True,'data':{'rows':[],'items':[],'total':0,'summary':{},'filters':{},'running':False,'agents':[]},'rows':[],'items':[],'total':0}
report={'scope':'Real standalone HTML/CSS/JS; empty API stubs. Errors can be fixture shape mismatches; no business/backend throughput measured.','results':[]}
env=Environment(loader=FileSystemLoader(ROOT/'bit/templates'))
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path=browser_path(pw),headless=True)
 for width in (1440,390):
  for name in TEMPLATES:
   errors=[];calls=[]
   try:
    html=env.get_template(name+'.html').render(rules=RULES,reply_text=REPLY_TEXT,current_user={'id':1,'role_key':'super_admin','username':'offline','is_admin':True,'is_platform_admin':True,'permissions':['*']},embedded=False,local_only=False,can_execute=False,can_configure=False,can_view_all=True,agent_launch_available=False,remote_read_only=True,plugin_launch_only=False,ip_rights_mode=False,url_for=lambda endpoint,**kw:'/static/'+kw['filename'] if 'filename' in kw else '/offline')
    page=browser.new_page(viewport={'width':width,'height':900})
    def route(r):
     path=urlsplit(r.request.url).path
     if path=='/':r.fulfill(body=html,content_type='text/html')
     elif path.startswith('/static/') and (ROOT/'bit'/path.lstrip('/')).is_file():r.fulfill(path=str(ROOT/'bit'/path.lstrip('/')))
     elif r.request.resource_type=='document':r.fulfill(body='<html><body>Offline embedded fixture</body></html>',content_type='text/html')
     else:calls.append({'method':r.request.method,'path':path});r.fulfill(json=body(path))
    page.route('**/*',route);page.on('pageerror',lambda error:errors.append(str(error)))
    page.add_init_script("window.longTasks=[];new PerformanceObserver(l=>{for(const e of l.getEntries())longTasks.push(e.duration)}).observe({type:'longtask',buffered:true})")
    page.goto('http://standalone-audit.test/',wait_until='load');page.wait_for_timeout(300)
    metrics=page.evaluate("() => ({loadMs:performance.getEntriesByType('navigation')[0].loadEventEnd,domNodes:document.querySelectorAll('*').length,documentWidth:document.documentElement.scrollWidth,longTasks})")
    report['results'].append({'template':name,'width':width,**metrics,'errors':errors,'requests':calls,'html_bytes':len(html.encode())});page.close()
   except Exception as e:report['results'].append({'template':name,'width':width,'setup_error':str(e)})
 browser.close()
Path(__file__).with_name('browser-standalone.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print([(x['template'],x['width'],round(x.get('loadMs',0)),len(x.get('errors',[])),x.get('setup_error','')) for x in report['results']])
