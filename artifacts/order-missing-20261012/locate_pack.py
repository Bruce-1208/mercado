import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from bit import bit_mysql as db
from mercado_api.client import MercadoLibreClient
conn=db.pymysql.connect(**db.config)
with conn.cursor() as cur:
 cur.execute('SELECT id,display_name,access_token FROM mercado_store_tokens WHERE enabled=1'); records=cur.fetchall()
conn.close()
def check(r):
 c=MercadoLibreClient(r['access_token'],timeout=12,admission_timeout=20)
 try:
  o=c.request('GET', '/packs/2000015470028643')
  Path('artifacts/order-missing-20261012/pack.json').write_text(json.dumps(o,ensure_ascii=False,indent=2))
  return {'token_id':r['id'],'shop':r['display_name'],'order':{k:o.get(k) for k in ['id','seller','status','date_created']}}
 except Exception as e:
  return {'token_id':r['id'],'error':str(e)[:180]}
with ThreadPoolExecutor(max_workers=10) as pool:
 results=[]
 for f in as_completed([pool.submit(check,r) for r in records]):
  r=f.result();results.append(r)
  if 'order' in r:print(json.dumps(r,ensure_ascii=False),flush=True)
Path('artifacts/order-missing-20261012/locate-pack.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print('Checked',len(results),'found',sum('order' in r for r in results),flush=True)
