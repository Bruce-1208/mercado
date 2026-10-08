import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bit import bit_mysql as db
from bit import yandex_order_sync
from erp import mercadolibre_profitability_cache as rates, mercadolibre_store_link_store as links
import pymysql
from bit.db_pool import default_connection_config
for name in ('_ensure_mercado_synced_orders_table','_ensure_mercado_store_tokens_table','_ensure_mercado_store_site_settings_table'):
 setattr(db,name,lambda c:None)
yandex_order_sync.ensure_tables=lambda c:None
rates.ensure_profitability_cache_tables=lambda c:None
links.ensure_store_link_table=lambda c:None
samples=[]
class Cursor:
 def __init__(self,c):self.c=c
 def __enter__(self):return self
 def __exit__(self,*a):self.c.close()
 def execute(self,sql,params=None):
  if not sql.lstrip().upper().startswith(('SELECT','WITH','DROP TEMPORARY TABLE','CREATE TEMPORARY TABLE','ALTER TABLE `TMP_')):raise RuntimeError('Disallowed statement '+sql[:50])
  t=time.perf_counter()
  try:return self.c.execute(sql,params)
  finally:samples.append({'ms':round(1000*(time.perf_counter()-t),2),'sql':sql})
 def __getattr__(self,n):return getattr(self.c,n)
class Connection:
 def __init__(self):
  self.c=pymysql.connect(**default_connection_config())
  with self.c.cursor() as q:q.execute('SET SESSION max_execution_time=15000')
 def cursor(self):return Cursor(self.c.cursor())
 def __getattr__(self,n):return getattr(self.c,n)
db.pymysql.connect=lambda **kw:Connection()
checks=[]
for params in ({}, {'country':'墨西哥'}, {'page':2}, {'search':'FunctionalAuditNoMatch20261008'}, {'sort_by':'cost'}):
 t=time.perf_counter(); full=db.list_orders(page_size=50,**params); full_ms=round((time.perf_counter()-t)*1000,1)
 t=time.perf_counter(); fast=db.list_orders(page_size=50,include_summary=False,**params);fast_ms=round((time.perf_counter()-t)*1000,1)
 fields=['rows','total','pages','status_counts','country_counts','origin_counts','store_counts','salesperson_counts','group_counts']
 equal={key:full[key]==fast[key] for key in fields}
 checks.append({'filter':params,'full_ms':full_ms,'fast_ms':fast_ms,'equal':equal})
 print(checks[-1],flush=True)
Path(sys.argv[1]).write_text(json.dumps(checks,ensure_ascii=False,indent=2))
assert all(all(row['equal'].values()) for row in checks)
