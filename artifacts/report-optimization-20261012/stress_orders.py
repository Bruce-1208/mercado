"""Real order route/SQL + lightweight DB probes on an isolated synthetic MySQL.
This does not emulate production product/store-list queries or public networking.
"""
import ast,json,math,os,sys,threading,time,logging
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13317',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1',MYSQL_POOL_MAX_CONNECTIONS='12',MYSQL_POOL_ACQUIRE_TIMEOUT='5')
from flask import Flask,jsonify,request,has_request_context
from waitress import create_server
import requests
from bit import bit_mysql as db,yandex_order_sync,order_read_cache
from bit.db_pool import get_db_connection,pool_status,close_all_pools
from bit.database_availability import install_database_error_handlers
from erp import mercadolibre_profitability_cache as rates,mercadolibre_store_link_store as links
for name in ('_ensure_mercado_synced_orders_table','_ensure_mercado_store_tokens_table','_ensure_mercado_store_site_settings_table'):setattr(db,name,lambda c:None)
yandex_order_sync.ensure_tables=lambda c:None
rates.ensure_profitability_cache_tables=lambda c:None
links.ensure_store_link_table=lambda c:None
app=Flask(__name__);install_database_error_handlers(app)
user={'id':1,'role_key':'super_admin','access_version':1}
names={'api_orders','_order_list_query_params','_parse_int_param'}
nodes=[n for n in ast.parse((ROOT/'bit/bit_interface.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name in names]
for n in nodes:n.decorator_list=[]
namespace=dict(has_request_context=has_request_context,workbench_user_own_store_only=lambda:False,app=app,request=request,jsonify=jsonify,logging=logging,db_list_orders=db.list_orders,_authorized_order_store_ids=lambda:None,user_warehouse=lambda u:None,get_current_workbench_user=lambda:user)
exec(compile(ast.Module(body=nodes,type_ignores=[]),'<real-order-route>','exec'),namespace)
app.add_url_rule('/api/orders',view_func=namespace['api_orders'])
@app.before_request
def authenticate():
 if request.headers.get('X-Synthetic-Test')!='orders':return jsonify(status='error'),401
 c=get_db_connection(priority=True)
 try:
  with c.cursor() as q:q.execute('SELECT 1')
 finally:c.close()
@app.get('/probe')
def probe():
 c=get_db_connection()
 try:
  with c.cursor() as q:q.execute('SELECT COUNT(*) AS total FROM unified_yandex_stores WHERE id=0');data=q.fetchone()
 finally:c.close()
 return jsonify(status='success',data=data)
server=create_server(app,host='127.0.0.1',port=15099,threads=32)
thread=threading.Thread(target=server.run,daemon=True);thread.start()
local=threading.local()
CASES=[('page', '/api/orders?page=1&page_size=50&include_summary=0'),
       ('page2', '/api/orders?page=2&page_size=50&include_summary=0'),
       ('summary','/api/orders?summary_only=1'),
       ('store','/api/orders?store_id=1&include_summary=0'),
       ('search','/api/orders?search=test00000100&include_summary=0'),
       ('probe','/probe')]
def request_one(i,refresh=False):
 if not hasattr(local,'session'):
  local.session=requests.Session();local.session.trust_env=False;local.session.headers['X-Synthetic-Test']='orders'
 name,path=CASES[i%len(CASES)]
 if refresh and name!='probe':path+='&refresh=1'
 start=time.monotonic()
 try:
  r=local.session.get('http://127.0.0.1:15099'+path,timeout=(3,20));body=r.json();valid=r.status_code==200 and body.get('status')=='success'
  if valid and name!='probe':
   data=body.get('data',{});valid=isinstance(data,dict) and 'total' in data
   if name in ('page','page2'):valid=valid and len(data.get('rows',[]))==50
   if name=='search':valid=valid and data.get('total')==1
  return {'endpoint':name,'ms':round((time.monotonic()-start)*1000,2),'status':r.status_code,'ok':valid,'bytes':len(r.content),'error':body.get('message') if not valid else None}
 except Exception as e:return {'endpoint':name,'ms':round((time.monotonic()-start)*1000,2),'status':type(e).__name__,'ok':False,'error':str(e)}
def summarize(rows):
 vals=sorted(x['ms'] for x in rows)
 return {'requests':len(rows),'failed':sum(not x['ok'] for x in rows),'error_percent':round(100*sum(not x['ok'] for x in rows)/max(len(rows),1),2),
         **{'p'+str(p)+'_ms':vals[min(math.ceil(len(vals)*p/100)-1,len(vals)-1)] if vals else 0 for p in (50,95,99)},'max_ms':max(vals,default=0)}
def stage(label,slots,seconds,refresh=False):
 start=time.monotonic();deadline=start+seconds;pending=set();rows=[];peak=0;db_peak=0;i=0;stop='completed'
 with ThreadPoolExecutor(slots) as pool:
  while time.monotonic()<deadline:
   while len(pending)<slots:
    pending.add(pool.submit(request_one,i,refresh));i+=1;peak=max(peak,len(pending))
   done,_=wait(pending,timeout=.02,return_when=FIRST_COMPLETED);pending-=done;rows.extend(f.result() for f in done)
   db_peak=max(db_peak,pool_status()['active'])
   if len(rows)>=30 and sum(not x['ok'] for x in rows[-30:])>=10:
    stop='error_guard';break
  rows.extend(f.result() for f in pending)
 elapsed=time.monotonic()-start
 result={'label':label,'concurrent_clients':slots,'refresh':refresh,'duration_seconds':round(elapsed,2),'actual_rps':round(len(rows)/elapsed,2),'peak_pending':peak,'peak_db_active':db_peak,'stop':stop,**summarize(rows),'per_endpoint':{name:summarize([x for x in rows if x['endpoint']==name]) for name,_ in CASES},'samples':rows,'pool_after':pool_status()}
 print(json.dumps({k:v for k,v in result.items() if k not in ('samples','pool_after')}),flush=True)
 return result
out=Path(__file__).with_name('stress.json')
results={'scope':'Loopback HTTP / extracted current order handler / real current SQL / MySQL 8 / 80000 synthetic orders / 20 stores / 32 Waitress threads / 12 pooled DB connections. Authentication replaced with fixed synthetic identity plus SELECT 1; startup schema already prepared. No production network or full production authentication. Five order query variants and one lightweight probe in equal proportions. Closed-loop clients; cached and forced-refresh results separate.', 'stages':[]}
try:
 results['cold_queries']=[request_one(i,True) for i in range(len(CASES))]
 print('cold',results['cold_queries'],flush=True)
 if any(not x['ok'] for x in results['cold_queries']):raise RuntimeError('Synthetic fixture validation failed')
 for label,slots,seconds,refresh in [('cached-1',1,8,False),('cached-8',8,10,False),('cached-32',32,12,False),('cached-64',64,12,False),('cached-128',128,12,False),('recovery-1',1,8,False),('uncached-1',1,8,True),('uncached-8',8,12,True),('uncached-32',32,12,True),('uncached-64',64,12,True),('soak-16',16,60,False),('final-recovery-1',1,8,False)]:
  order_read_cache.invalidate();results['stages'].append(stage(label,slots,seconds,refresh));out.write_text(json.dumps(results,indent=2))
finally:
 server.task_dispatcher.shutdown();server.close();thread.join(timeout=3);close_all_pools()
 out.write_text(json.dumps(results,indent=2))
