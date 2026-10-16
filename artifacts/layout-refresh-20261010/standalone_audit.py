import sys,json
from pathlib import Path
from urllib.parse import urlsplit
from jinja2 import Environment,FileSystemLoader
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_console_ui_browser import console_browser
out=Path(__file__).parent; root=Path.cwd()
bg=console_browser.__wrapped__();browser=next(bg)
ctx=dict(current_user={'id':1,'role_key':'super_admin','username':'preview','display_name':'界面预览','is_platform_admin':True}, embedded=False,can_manage=True,billing_admin=True,wallet_admin=True,local_only=False,can_execute=False,can_configure=False,can_view_all=True, agent_launch_available=True,remote_read_only=True,plugin_launch_only=False,ip_rights_mode=False,url_for=lambda endpoint,filename:'/static/'+filename,providers=[],csrf_token='offline',job_id='preview')
results=[]
for module in ['employee_tasks','mercado_action_center','integration_settings','ai_billing','ai_weight_price','ai_video_details','account_profile','account_billing','print_wallet','clearance_alerts','size_charts','mercado_promotions','infraction_dashboard','login','register','yandex']:
 base=root/('yandex/app' if module=='yandex' else 'bit')
 env=Environment(loader=FileSystemLoader(base/'templates'))
 try:html=env.get_template('index.html' if module=='yandex' else module+'.html').render(**ctx)
 except Exception as exc:
  results.append({'module':module,'error':str(exc)});continue
 page=browser.new_page()
 def route(r):
  path=urlsplit(r.request.url).path
  if path=='/':r.fulfill(body=html,content_type='text/html')
  elif path.startswith('/static/') and (base/path.lstrip('/')).is_file():r.fulfill(path=str(base/path.lstrip('/')))
  else:r.fulfill(json={'status':'success','success':True,'data':{},'items':[],'rows':[],'stores':[],'total':0})
 page.route('**/*',route)
 page.goto('http://layout.test/')
 for width in [1440,1024,390]:
  page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(50)
  info=page.evaluate('''()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,width:document.documentElement.scrollWidth,offenders:[...document.body.querySelectorAll('*')].filter(el=>{let r=el.getBoundingClientRect();return r.width>0 && r.right>innerWidth+2 && !el.closest('.scroll,.table-wrap,.table-scroll,.order-table-wrap')}).slice(0,8).map(el=>({tag:el.tagName,cls:el.className,id:el.id,width:Math.round(el.getBoundingClientRect().width)}))})''')
  results.append({'module':module,'viewport':width,**info})
  if width in [1440,390]:page.screenshot(path=str(out/f'standalone-{width}-{module}.png'))
 if module=='yandex':
  views=page.locator('.app-view').evaluate_all('(els)=>els.map(el=>el.id)')
  for view in views:
   page.evaluate("id=>document.querySelectorAll('.app-view').forEach(el=>el.classList.toggle('active',el.id===id))",view)
   for width in [1440,1024,390]:
    page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(30)
    info=page.evaluate('()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,width:document.documentElement.scrollWidth})')
    results.append({'module':'yandex-'+view,'viewport':width,**info})
    if width in [1440,390]:page.screenshot(path=str(out/f'yandex-{width}-{view}.png'))
 page.close()
(out/'standalone.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps({'checks':len(results),'problems':[r for r in results if r.get('overflow') or r.get('error')]},ensure_ascii=False))
try:next(bg)
except StopIteration:pass
