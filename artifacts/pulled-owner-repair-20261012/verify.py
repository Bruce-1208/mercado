import gzip,json
from collections import Counter
from pathlib import Path
import pymysql
from bit.db_pool import default_connection_config
base=Path('artifacts/pulled-owner-repair-20261012')
with gzip.open(base/'before.jsonl.gz','rt') as f:
 audit=[json.loads(line) for line in f]
settings=audit[0]['restore']; products=audit[1:]; counts=Counter()
excluded={'李泽1','李泽2','李超宇','李超宇2','姚杰','一路长虹3'}
assert not any(r['plugin'].get('store_name') in excluded for r in products)
c=pymysql.connect(**dict(default_connection_config(),read_timeout=15))
with c.cursor() as q:
 q.execute('SET SESSION max_execution_time=8000')
 for offset in range(0,len(products),500):
  batch=products[offset:offset+500];ids=[r['id'] for r in batch]
  q.execute('SELECT id,organization_key,owner_name,owner_user_id,source_type FROM erp_mercadolibre_products WHERE id IN ('+','.join(['%s']*len(ids))+')',ids)
  actual={r['id']:r for r in q.fetchall()}
  for r in batch:
   a=actual[r['id']]
   assert a['organization_key']==r['organization_key'] and a['owner_name']==r['salesperson'] and a['owner_user_id'] is None and a['source_type']=='pulled',r['id']
   counts[r['salesperson']]+=1
 for r in settings:
  q.execute('SELECT salesperson FROM mercado_store_site_settings WHERE token_id=%s AND site_id=%s',(r['token_id'],r['site_id']))
  assert q.fetchone()['salesperson']==r['salesperson']
c.close()
result=dict(verified_products=len(products),verified_settings=len(settings),owners=dict(counts),excluded_stores_untouched=True)
(base/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
