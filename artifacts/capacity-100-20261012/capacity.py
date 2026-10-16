"""Real order route/SQL + lightweight DB probes on an isolated synthetic MySQL.
This does not emulate production product/store-list queries or public networking.
"""
import ast,json,math,os,sys,threading,time,logging
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13317',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1',MYSQL_POOL_MAX_CONNECTIONS='6',MYSQL_POOL_ACQUIRE_TIMEOUT='5')
from flask import Flask,jsonify,request,has_request_context,g,session as flask_session
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
if os.environ.get('CAPACITY_BASELINE') == '1':
 old_node=next(n for n in ast.parse(Path(__file__).with_name('bit_mysql.before.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='list_orders')
 old_ns=dict(db.__dict__);exec(compile(ast.Module(body=[old_node],type_ignores=[]),'<baseline>','exec'),old_ns)
 db.list_orders=old_ns['list_orders']
app=Flask(__name__);app.secret_key='isolated-capacity-cookie-key';install_database_error_handlers(app)
account_node=next(n for n in ast.parse((ROOT/'bit/bit_interface.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='get_workbench_user')
account_ns=dict(pymysql=db.pymysql,mysql_config=db.config)
exec(compile(ast.Module(body=[account_node],type_ignores=[]),'<real-account-lookup>','exec'),account_ns)

user={'id':1,'role_key':'super_admin','access_version':1}
names={'api_orders','_order_list_query_params','_parse_int_param'}
nodes=[n for n in ast.parse((ROOT/'bit/bit_interface.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name in names]
for n in nodes:n.decorator_list=[]
if os.environ.get('CAPACITY_BASELINE')=='1':
 before_nodes=[n for n in ast.parse(Path(__file__).with_name('bit_interface.before.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name in names]
 for n in before_nodes:n.decorator_list=[]
 nodes=before_nodes
namespace=dict(has_request_context=has_request_context,workbench_user_own_store_only=lambda:False,app=app,request=request,jsonify=jsonify,logging=logging,db_list_orders=db.list_orders,_authorized_order_store_ids=lambda:None,user_warehouse=lambda u:None,get_current_workbench_user=lambda:user)
exec(compile(ast.Module(body=nodes,type_ignores=[]),'<real-order-route>','exec'),namespace)
app.add_url_rule('/api/orders',view_func=namespace['api_orders'])
@app.before_request
def authenticate():
 if request.headers.get('X-Synthetic-Test')!='orders':return jsonify(status='error'),401
 if os.environ.get('CAPACITY_DB_ACCOUNTS')=='1':
  uid=int(request.headers.get('X-Audit-User','0'))
  if flask_session.get('audit_account') != uid:return jsonify(status='error'),401
  row=account_ns['get_workbench_user'](user_id=uid)
  if not row or not row['is_active']:return jsonify(status='error'),401
  g.capacity_user={k:row[k] for k in ['id','organization_key','role_key','personal_name']}
  return None
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
server=create_server(app,host='127.0.0.1',port=15099,threads=32,connection_limit=500,backlog=1024,channel_timeout=120,cleanup_interval=30)
def serve():
 try:server.run()
 except OSError as exc:
  if exc.errno!=9:raise
thread=threading.Thread(target=serve,daemon=True);thread.start()

profile='staff'
account_offset=0
namespace['get_current_workbench_user']=lambda: getattr(g,'capacity_user',None) or {'id':int(request.headers.get('X-Audit-User','1')),'role_key':'member','access_version':1,'organization_key':'synthetic'}
namespace['_authorized_order_store_ids']=lambda: None if profile=='admin' or (profile=='mixed' and int(request.headers.get('X-Audit-User','1'))%10==0) else [((int(request.headers.get('X-Audit-User','1'))-1)%20)+1]
CASES=[('list','/api/orders?page=1&page_size=50&include_summary=0'),('summary','/api/orders?summary_only=1'),('page2','/api/orders?page=2&page_size=50&include_summary=0'),('search','/api/orders?search=Synthetic&include_summary=0')]
def summarize(rows):
 vals=sorted(x['ms'] for x in rows)
 return {'requests':len(rows),'failed':sum(not x['ok'] for x in rows),'error_percent':round(100*sum(not x['ok'] for x in rows)/max(len(rows),1),2),**{'p'+str(p)+'_ms':vals[min(math.ceil(len(vals)*p/100)-1,len(vals)-1)] if vals else 0 for p in (50,95,99)},'max_ms':max(vals,default=0)}
def stage(kind,users,seconds,think=5,offset=0):
 global profile
 profile=kind;order_read_cache.invalidate();begin=time.monotonic();deadline=begin+seconds;barrier=threading.Barrier(users);records=[];lock=threading.Lock();peak_db=0;peak_flights=0
 def user_run(uid):
  uid += offset
  session=requests.Session();session.trust_env=False;session.headers.update({'X-Synthetic-Test':'orders','X-Audit-User':str(uid)});session.cookies.set('session',app.session_interface.get_signing_serializer(app).dumps({'audit_account':uid}));barrier.wait();step=0;rows=[]
  while time.monotonic()<deadline:
   case=1 if step%6==1 else 2 if step%6==5 else 3 if step%6==3 else 0;name,path=CASES[case]
   if name=='search' and os.environ.get('CAPACITY_SEARCH_VARIETY')=='1':
    from urllib.parse import quote
    term=['Synthetic','pack123','test00000100','not-found-unique','product42'][(uid//10+step//6)%5]
    path='/api/orders?include_summary=0&search='+quote(term)
   start=time.monotonic()
   try:
    r=session.get('http://127.0.0.1:15099'+path,timeout=(3,15));data=r.json();ok=r.status_code==200 and data.get('status')=='success'
    if ok and (kind=='staff' or (kind=='mixed' and uid%10!=0)):
     allowed=((uid-1)%20)+1
     ok=all(int(row.get('store_id',allowed))==allowed for row in data.get('data',{}).get('rows',[]))
    row={'user':uid,'endpoint':name,'ms':round((time.monotonic()-start)*1000,2),'ok':ok,'status':r.status_code}
   except Exception as e:row={'user':uid,'endpoint':name,'ms':round((time.monotonic()-start)*1000,2),'ok':False,'status':type(e).__name__}
   rows.append(row);step+=1;time.sleep(min(max(0,think-(time.monotonic()-start)),max(0,deadline-time.monotonic())))
  session.close();return rows
 with ThreadPoolExecutor(users) as pool:
  futures=[pool.submit(user_run,i+1) for i in range(users)]
  while not all(f.done() for f in futures):
   peak_db=max(peak_db,pool_status()['active']);peak_flights=max(peak_flights,sum(x[1] for x in order_read_cache._FLIGHTS.copy().values()));time.sleep(.05)
  for future in futures:records.extend(future.result())
 elapsed=time.monotonic()-begin
 r={'profile':kind,'account_offset':offset,'users':users,'think_seconds':think,'duration_seconds':round(elapsed,2),'actual_rps':round(len(records)/elapsed,2),'peak_db_active':peak_db,'peak_order_requests':peak_flights,**summarize(records),'per_role':{role:summarize([x for x in records if ('admin' if kind=='admin' or (kind=='mixed' and x['user']%10==0) else 'staff')==role]) for role in ['staff','admin']},'per_endpoint':{n:summarize([x for x in records if x['endpoint']==n]) for n,_ in CASES},'samples':records,'pool_after':pool_status(),'distinct_accounts':len(set(x['user'] for x in records))}
 print(json.dumps({k:v for k,v in r.items() if k!='samples'}),flush=True);return r
out=Path(__file__).with_name(os.environ.get('CAPACITY_OUTPUT','capacity.json'));results={'scope':'Distinct account sessions; optimized cache shares identical resolved visibility within an organization, baseline cache remains per-account. Staff each authorized for one of 20 stores (4000 orders/store); admins authorized for all 80000 orders. Actual current order handler and SQL, signed synthetic session and actual get_workbench_user SQL against 500 seeded database accounts (when CAPACITY_DB_ACCOUNTS=1), with fixture store grants, Waitress32 / DB pool6 / isolated MySQL128MiB. Each user starts a request every 5s when the previous request has completed; slow requests run immediately on completion (no overlapping requests per user). 500 account identities, five 100-user cohorts, synchronized cold starts. Mixed profile: 90% store-scoped staff and 10% full-dataset admins. Every6 requests:3 list,1 summary,1 search,1 next-page. 65s stages deliberately include keyword search and multiple cache expiry cycles. No real background jobs or external APIs. Closed-loop model underdelivers target traffic when slow.','settings':{'database_accounts':os.environ.get('CAPACITY_DB_ACCOUNTS')=='1','varied_search':os.environ.get('CAPACITY_SEARCH_VARIETY')=='1','baseline':os.environ.get('CAPACITY_BASELINE')=='1','server_connections':500,'database_connections':6},'stages':[]}
try:
 for kind,users,seconds,offset in json.loads(os.environ.get('CAPACITY_STAGES','[["mixed",100,65,0]]')):
  results['stages'].append(stage(kind,users,seconds,offset=offset));out.write_text(json.dumps(results,indent=2))
finally:
 server.task_dispatcher.shutdown();server.close();thread.join(timeout=3);close_all_pools();out.write_text(json.dumps(results,indent=2))
