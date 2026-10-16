"""Real order route/SQL + lightweight DB probes on an isolated synthetic MySQL.
This does not emulate production product/store-list queries or public networking.
"""
import ast,json,math,os,sys,threading,time,logging
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13307',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1',MYSQL_POOL_MAX_CONNECTIONS='6',MYSQL_POOL_ACQUIRE_TIMEOUT='5')
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
node=next(n for n in ast.parse(Path(__file__).with_name('bit_mysql.before.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='list_orders')
old_namespace=dict(db.__dict__);exec(compile(ast.Module(body=[node],type_ignores=[]),'<before>','exec'),old_namespace)
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
server=create_server(app,host='127.0.0.1',port=15089,threads=32)
thread=threading.Thread(target=server.run,daemon=True);thread.start()
local=threading.local()
def request_one(i):
 if not hasattr(local,'session'):
  local.session=requests.Session();local.session.trust_env=False;local.session.headers['X-Synthetic-Test']='orders'
 path='/api/orders?page='+str((i//6)%3+1)+'&page_size=50' if i%6==0 else '/probe'
 start=time.monotonic()
 try:
  r=local.session.get('http://127.0.0.1:15089'+path,timeout=(2,12));valid=r.status_code==200 and r.json().get('status')=='success'
  return {'endpoint':'orders' if i%6==0 else 'probe','ms':round((time.monotonic()-start)*1000,2),'status':r.status_code,'ok':valid,'bytes':len(r.content)}
 except requests.RequestException as e:return {'endpoint':'orders' if i%6==0 else 'probe','ms':round((time.monotonic()-start)*1000,2),'status':type(e).__name__,'ok':False}
def summarize(rows):
 vals=sorted(x['ms'] for x in rows)
 return {'requests':len(rows),'failed':sum(not x['ok'] for x in rows),'p50_ms':vals[math.ceil(len(vals)*.5)-1] if vals else 0,'p95_ms':vals[math.ceil(len(vals)*.95)-1] if vals else 0,'max_ms':max(vals,default=0)}
def stage(slots,rps,seconds):
 start=time.monotonic();deadline=start+seconds;pending=set();rows=[];peak=0;db_peak=0;i=0;next_at=start;stop='completed'
 with ThreadPoolExecutor(slots) as pool:
  while time.monotonic()<deadline:
   now=time.monotonic()
   if len(pending)<slots and now>=next_at:
    pending.add(pool.submit(request_one,i));i+=1;next_at=max(next_at+1/rps,now);peak=max(peak,len(pending))
   done={f for f in pending if f.done()};pending-=done;rows.extend(f.result() for f in done)
   db_peak=max(db_peak,pool_status()['active'])
   if any(not x['ok'] for x in rows[-20:]) or sum(x['ms']>8000 for x in rows[-20:])>=3:
    stop='error_or_latency_guard';break
   time.sleep(.005)
  rows.extend(f.result() for f in pending)
 elapsed=time.monotonic()-start
 return {'slots':slots,'rps_limit':rps,'duration_seconds':round(elapsed,2),'actual_rps':round(len(rows)/elapsed,2),'peak_pending_futures':peak,'peak_db_active':db_peak,'stop':stop,**summarize(rows),'per_endpoint':{name:summarize([x for x in rows if x['endpoint']==name]) for name in ('orders','probe')},'samples':rows}
results={'scope':'isolated internal-disk MySQL, 80000 synthetic orders, real order HTTP handler and SQL; 1/6 orders, 5/6 lightweight DB probes; 32 HTTP threads, 6 DB connections; no production workload or network','runs':{}}
try:
 for label,loader in [('before',old_namespace['list_orders']),('after',db.list_orders)]:
  namespace['db_list_orders']=loader;order_read_cache.invalidate();stages=[]
  for slots,rps,seconds in [(1,1,15),(10,5,30),(20,10,30),(40,20,45)]+([(20,10,120)] if label=='after' else []):
   row=stage(slots,rps,seconds);stages.append(row)
   print(label,json.dumps({k:v for k,v in row.items() if k!='samples'}),flush=True)
   if row['stop']!='completed':break
  results['runs'][label]=stages
  Path(__file__).with_name('isolated-stress.json').write_text(json.dumps(results,indent=2))
finally:
 server.task_dispatcher.shutdown();server.close();thread.join(timeout=3);close_all_pools()
