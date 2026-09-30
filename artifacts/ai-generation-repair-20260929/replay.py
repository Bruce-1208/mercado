import json
import sys
from pathlib import Path
from bit import bit_mysql
from erp.ai_original_products import generate_marketplace_copy, complete_marketplace_category
from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient
out=Path('artifacts/ai-generation-repair-20260929')
c=bit_mysql._appeal_connection()
with c.cursor() as q:
 q.execute("SELECT id,weight_g,package_length_cm,package_width_cm,package_height_cm,source_snapshot_json FROM erp_mercadolibre_products WHERE id IN (13088981,13088986,13088987,13088988,13088989)")
 rows=q.fetchall()
c.close()
if len(sys.argv)>1:
 rows=[r for r in rows if str(r['id']) in sys.argv[1:]]
tokens=bit_mysql.list_mercado_store_tokens()['rows']
token=next(t for t in tokens if t.get('enabled',True) and t.get('site_id') in ('CBT','',None))
client=DatabaseMercadoLibreClient(token['id'])
from bit import browser_extension_models
api_key=browser_extension_models.get_api_key(1,'deepseek',Path('bit/runtime_locks/workbench_secret.key').read_text().strip())
results=[]
def replay(row):
 original=json.loads(row['source_snapshot_json'])['original_1688']
 for field in ('weight_g','package_length_cm','package_width_cm','package_height_cm'):
  if row.get(field) is not None: original[field]=str(row[field])
 try:
  copy=generate_marketplace_copy(original,api_key=api_key)
  result=complete_marketplace_category(original,copy,client,api_key=api_key)
  results.append({'id':row['id'],'result':result})
  print(row['id'],'missing=',result['missing_required_attributes'],flush=True)
 except Exception as e:
  results.append({'id':row['id'],'error':str(e)})
  print(row['id'],str(e),flush=True)
 return
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=2) as pool:
 list(pool.map(replay,rows))
(out/'replay-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
