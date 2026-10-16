import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from bit import bit_mysql as db
from mercado_api.client import MercadoLibreClient
c=db.pymysql.connect(**db.config)
with c.cursor() as q:
 q.execute('SELECT id,display_name,meli_user_id,access_token FROM mercado_store_tokens WHERE enabled=0'); records=q.fetchall()
c.close()
root=Path('artifacts/order-missing-20261012/search-disabled');root.mkdir(exist_ok=True)
def run(r):
 cli=MercadoLibreClient(r['access_token'],timeout=20,admission_timeout=30)
 results=[]; offset=0
 try:
  while True:
   page=cli.request('GET','/marketplace/orders/search',params={'seller':r['meli_user_id'],'date_created.from':'2026-10-10T00:00:00.000Z','sort':'date_asc','offset':offset,'limit':50})
   batch=page.get('results') or [];results+=batch;offset+=len(batch)
   if not batch or offset>=int(page.get('paging',{}).get('total',offset)):break
  (root/f'{r["id"]}.json').write_text(json.dumps(results,ensure_ascii=False))
  matches=[o for o in results if '2000015470028643' in json.dumps(o)]
  return {'token_id':r['id'],'shop':r['display_name'],'count':len(results),'matches':matches}
 except Exception as e:return {'token_id':r['id'],'error':str(e)[:250]}
results=[]
with ThreadPoolExecutor(max_workers=6) as pool:
 for f in as_completed([pool.submit(run,r) for r in records]):
  r=f.result();results.append(r)
  if r.get('matches'):print(json.dumps(r,ensure_ascii=False),flush=True)
(root/'summary.json').write_text(json.dumps(results,ensure_ascii=False))
print('done',len(results),'errors',sum('error' in r for r in results),flush=True)
