import os,sys,time,json
from pathlib import Path
ROOT=Path('/private/tmp/mercado-full-audit-20261012');sys.path.insert(0,str(ROOT));os.chdir(ROOT)
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13317',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1')
import pymysql
from bit import bit_mysql as db,yandex_order_sync
from erp import mercadolibre_profitability_cache as rates,mercadolibre_store_link_store as links
for n in ('_ensure_mercado_synced_orders_table','_ensure_mercado_store_tokens_table','_ensure_mercado_store_site_settings_table'):setattr(db,n,lambda c:None)
yandex_order_sync.ensure_tables=lambda c:None;rates.ensure_profitability_cache_tables=lambda c:None;links.ensure_store_link_table=lambda c:None
original=pymysql.cursors.Cursor.execute;queries=[]
def trace(self,query,args=None):
 plan=None;plan_error=None
 if isinstance(query,str) and query.strip().upper().startswith(('SELECT','INSERT')):
  try:original(self,'EXPLAIN '+query,args);plan=self.fetchall()
  except Exception as e:plan_error=str(e)
 start=time.monotonic()
 try:return original(self,query,args)
 finally:
  if isinstance(query,str):queries.append({'sql':query,'args':args,'ms':round((time.monotonic()-start)*1000,2),'explain':plan,'explain_error':plan_error})
pymysql.cursors.Cursor.execute=trace
results=[]
for label,kw in [('page',{'include_summary':False}),('summary',{'summary_only':True}),('search',{'search':'test00000100','include_summary':False})]:
 queries=[];start=time.monotonic();data=db.list_orders(**kw);results.append({'name':label,'elapsed_ms':round((time.monotonic()-start)*1000,2),'total':data['total'],'queries':queries[:]})
pymysql.cursors.Cursor.execute=original
c=pymysql.connect(host='127.0.0.1',port=13317,user='root',database='stress_test',cursorclass=pymysql.cursors.DictCursor)
with c.cursor() as q:
 q.execute('SELECT VERSION() AS version');version=q.fetchone()
 q.execute('SHOW GLOBAL STATUS WHERE Variable_name IN ("Threads_connected","Max_used_connections","Aborted_connects","Threads_running")');status=q.fetchall()
c.close()
Path('/Users/a11/mercado/artifacts/full-audit-20261012/sql-profile.json').write_text(json.dumps({'database':version,'status':status,'runs':results},ensure_ascii=False,indent=2,default=str))
print([(r['name'],r['elapsed_ms'],len(r['queries'])) for r in results])
