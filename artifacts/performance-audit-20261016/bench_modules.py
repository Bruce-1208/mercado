"""Real storage methods, synthetic local MySQL; NOT end-to-end business routes.
Explicit guard allows only this disposable MySQL. No upstream service calls.
"""
import os,sys,json,time,math,socket,threading,resource,argparse,traceback,re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13326',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1',MYSQL_POOL_MAX_CONNECTIONS='12',MYSQL_POOL_ACQUIRE_TIMEOUT='5')
connect=socket.socket.connect
blocked=[]
def guard(self,address):
 if not isinstance(address,tuple) or address[:2]!=('127.0.0.1',13326):
  blocked.append(str(address));raise PermissionError('Only disposable MySQL is allowed')
 return connect(self,address)
socket.socket.connect=guard
from erp import mercadolibre_collection_store as products,mercadolibre_store_link_store as links,mercadolibre_prohibited_store as prohibited,mercadolibre_infraction_store as infractions
from bit import bit_inventory as inventory,bit_mysql as db,product_access
from bit.db_pool import get_db_connection,pool_status,close_all_pools
import pymysql
USER={'id':1,'role_key':'super_admin','organization_key':'synthetic','access_version':1}
# Actual migration checks are allowed only in the isolated database.
c=get_db_connection()
with c.cursor() as q:
 for fn in (products.ensure_collection_tables,links.ensure_store_link_table,prohibited.ensure_prohibited_tables,infractions.ensure_infraction_tables,inventory.ensure_inventory_tables):fn(q)
c.commit();c.close()
# label: real callable + arguments. Parameters mirror UI page size, plus costly variants.
CASES={
 'products.page':(products.list_product_items,dict(limit=50,include_total=False)),
 'products.total':(products.list_product_items,dict(limit=50,include_total=True)),
 'products.search':(products.list_product_items,dict(limit=50,search='fixture00000100')),
 'products.deep_page':(products.list_product_items,dict(limit=50,offset=15000,include_total=False)),
 'products.ai_filter':(products.list_product_items,dict(limit=50,source_type='ai_original',ai_status='completed',include_generator_options=True)),
 'products.ai_fast':(products.list_product_items,dict(limit=50,source_type='ai_original',ai_status='completed',include_total=False,include_generator_options=False)),
 'products.ai_count':(products.list_product_items,dict(limit=50,source_type='ai_original',ai_status='completed',include_generator_options=False)),
 'products.generator_options':(products.list_product_items,dict(limit=50,source_type='ai_original',include_total=False,include_generator_options=True)),
 'products.group_skus':(products.list_product_items,dict(limit=50,group_skus=True)),
 'collection.page':(products.list_collection_items,dict(limit=50)),
 'collection.search':(products.list_collection_items,dict(limit=50,search='fixture00000100')),
 'publish_records.page':(products.list_product_publish_records,dict(limit=50)),
 'links.page':(links.list_store_links,dict(page_size=50)),
 'links.store':(links.list_store_links,dict(page_size=50,token_id=1)),
 'links.search':(links.list_store_links,dict(page_size=50,search='fixture00000100')),
 'links.deep_page':(links.list_store_links,dict(page_size=50,page=300)),
 'prohibited.page':(prohibited.list_prohibited_listings,dict(page_size=50)),
 'prohibited.search':(prohibited.list_prohibited_listings,dict(page_size=50,search='fixture00000100')),
 'infractions.dashboard':(infractions.list_infraction_dashboard,dict(page_size=50)),
 'infractions.rows':(infractions.list_infraction_dashboard,dict(page_size=50,rows_only=True)),
 'inventory.stock':(inventory.list_inventory_stock,dict(page_size=50)),
 'inventory.search':(inventory.list_inventory_stock,dict(page_size=50,search='fixture00000100')),
 'inventory.movements':(inventory.list_inventory_movements,dict(page_size=50)),
 'inventory.shelves':(inventory.list_inventory_shelves,{}),
 'orders.page':(db.list_orders,dict(page_size=50,include_summary=False)),
 'orders.summary':(db.list_orders,dict(summary_only=True)),
 'orders.store':(db.list_orders,dict(allowed_store_ids=[1],include_summary=False)),
 'orders.search':(db.list_orders,dict(search='test00000100',include_summary=False)),
 'orders.profit_sort':(db.list_orders,dict(sort_by='profit',include_summary=False)),
 'orders.context':(db.get_mercado_order_access_contexts,dict(order_ids=['test00000100'])),
 'stores.authorized_list':(db.list_mercado_store_tokens,{}),
 'after_sale.order_candidates':(db.list_mercado_message_pack_candidates,dict(token_id=1)),
 'tasks.today_orders':(db.list_mercado_action_center_orders,dict(token_ids=[1],limit=50)),
}
def call(name):
 fn,kw=CASES[name]
 with product_access.scope(USER):return fn(**kw)
def validate(name,value):
 if not isinstance(value,(dict,list)):raise AssertionError('Expected structured result')
 if isinstance(value,list):
  if not value:raise AssertionError('Empty synthetic result')
  return len(value)
 rows=value.get('rows',value.get('items'))
 if rows is not None and not isinstance(rows,list):raise AssertionError('Invalid rows')
 if rows is not None and not rows and name!='orders.summary':raise AssertionError('No fixture rows matched')
 if name.endswith('.search') and rows is not None and len(rows)!=1:raise AssertionError('Expected exact synthetic search result')
 return len(rows) if rows is not None else -1

def one(name):
 start=time.perf_counter()
 try:
  result=call(name);count=validate(name,result)
  return {'ms':round((time.perf_counter()-start)*1000,3),'ok':True,'rows':count}
 except Exception as e:return {'ms':round((time.perf_counter()-start)*1000,3),'ok':False,'error':f'{type(e).__name__}: {e}'}
def summarize(samples):
 values=sorted(s['ms'] for s in samples)
 return {'requests':len(samples),'failed':sum(not s['ok'] for s in samples),**{f'p{p}_ms':values[max(0,math.ceil(len(values)*p/100)-1)] if values else None for p in (50,95,99)},'max_ms':max(values,default=0)}
def stage(name,workers,seconds):
 start=time.perf_counter();cpu=time.process_time();deadline=start+seconds;samples=[];pending=set();peak=0
 with ThreadPoolExecutor(max_workers=workers) as pool:
  while time.perf_counter()<deadline:
   while len(pending)<workers:pending.add(pool.submit(one,name))
   done,pending=wait(pending,timeout=.02,return_when=FIRST_COMPLETED);samples.extend(f.result() for f in done)
   peak=max(peak,pool_status()['active'])
   if len(samples)>10 and sum(not s['ok'] for s in samples[-10:])>=5:break
  samples.extend(f.result() for f in pending)
 elapsed=time.perf_counter()-start
 return {'name':name,'concurrency':workers,'duration_s':elapsed,'rps':len(samples)/elapsed,'cpu_s':time.process_time()-cpu,'peak_db_active':peak,'max_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,**summarize(samples),'samples':samples,'pool_after':pool_status()}
def profile(name):
 original=pymysql.cursors.Cursor.execute;queries=[]
 def trace(cursor,query,args=None):
  plan=None
  plan_sql=query if str(query).lstrip().upper().startswith('SELECT') else None
  if str(query).lstrip().upper().startswith('CREATE TEMPORARY TABLE'):
   match=re.search(r'\bAS\s+(SELECT\b[\s\S]*)',query,re.I)
   if match:plan_sql=match.group(1)
  if plan_sql:
   try:original(cursor,'EXPLAIN '+plan_sql,args);plan=cursor.fetchall()
   except Exception as e:plan={'error':str(e)}
  start=time.perf_counter()
  try:return original(cursor,query,args)
  finally:queries.append({'sql':query,'args':args,'execute_ms':round((time.perf_counter()-start)*1000,3),'explain':plan})
 pymysql.cursors.Cursor.execute=trace
 try:sample=one(name)
 finally:pymysql.cursors.Cursor.execute=original
 return {'name':name,'sample':sample,'query_count':len(queries),'queries':queries}
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['profile','load'],default='profile');parser.add_argument('--seconds',type=float,default=4);parser.add_argument('--cases',nargs='*');parser.add_argument('--output',type=Path);args=parser.parse_args()
 selected=args.cases or list(CASES)
 out=args.output or Path(__file__).with_name('modules-'+args.mode+'.json')
 report={'scope':__doc__,'dataset':'80k orders;20k products, collection rows, listings, publish records;10k prohibited,infractions,stock,movements;20 stores/shelves','mode':args.mode,'results':[]}
 try:
  for name in selected:
   if args.mode=='profile':
    result=profile(name);report['results'].append(result);print(name,result['sample'],result['query_count'],flush=True)
   else:
    cold=one(name)
    if not cold['ok']:
     report['results'].append({'name':name,'validation_failure':cold});print(name,cold,flush=True);continue
    for concurrency in (1,8,32):
     result=stage(name,concurrency,args.seconds);report['results'].append(result)
     print(json.dumps({k:v for k,v in result.items() if k not in ('samples','pool_after')}),flush=True)
   out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
 finally:
  report['blocked_network_attempts']=blocked;out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));close_all_pools()
