import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bit import bit_mysql as db
from erp import mercadolibre_collection_store as products,mercadolibre_store_link_store as links
from bit.db_pool import default_connection_config
import pymysql
for name in ('_ensure_infringement_knowledge_table','_ensure_infringement_risk_checks_table','_ensure_zying_product_table','_backfill_legacy_zying_risk_checks','_ensure_mercado_store_site_settings_table'):
 setattr(db,name,lambda c:None)
products.ensure_collection_tables=lambda c:None
links.ensure_store_link_table=lambda c:None
class Cursor:
 def __init__(self,c):self.c=c
 def __enter__(self):return self
 def __exit__(self,*a):self.c.close()
 def execute(self,sql,params=None):
  if not sql.lstrip().upper().startswith(('SELECT','WITH','SHOW')):raise RuntimeError('Disallowed statement '+sql[:80])
  return self.c.execute(sql,params)
 def __getattr__(self,n):return getattr(self.c,n)
class Connection:
 def __init__(self):
  self.c=pymysql.connect(**default_connection_config())
  with self.c.cursor() as q:q.execute('SET SESSION max_execution_time=5000')
 def cursor(self):return Cursor(self.c.cursor())
 def __getattr__(self,n):return getattr(self.c,n)
db.pymysql.connect=lambda **kw:Connection()
samples=[]
for page in (1,2):
 t=time.perf_counter()
 r=db.get_infringement_risk_results(page=page,sort_by='row_id',limit=50)
 sample={'page':page,'ms':round((time.perf_counter()-t)*1000,1),'rows':len(r['rows']),'total':r['total']};samples.append(sample);print(sample,flush=True)
Path(sys.argv[1]).write_text(json.dumps(samples,indent=2))
