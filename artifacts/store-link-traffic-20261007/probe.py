import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pymysql, requests
from bit.db_pool import default_connection_config
c=default_connection_config(); c.update(host='127.0.0.1',port=3306,connect_timeout=5,read_timeout=15)
db=pymysql.connect(**c)
with db.cursor() as q:
 q.execute('SELECT t.id,t.access_token FROM mercado_store_tokens t WHERE t.enabled=1 AND EXISTS (SELECT 1 FROM erp_mercadolibre_store_links l WHERE l.token_id=t.id AND l.is_current=1 AND l.status=\'active\') LIMIT 1')
 row=q.fetchone()
 token=row['access_token']
 q.execute('SELECT item_id FROM erp_mercadolibre_store_links WHERE token_id=%s AND is_current=1 AND status=%s LIMIT 3',(row['id'],'active'))
 items=[r['item_id'] for r in q.fetchall()]
db.close()

def check(args):
 item,kind=args
 path='/visits/items' if kind=='total' else f'/items/{item}/visits/time_window'
 params={'ids':item} if kind=='total' else {'last':30,'unit':'day','ending':'2026-10-05'}
 try:
  r=requests.get('https://api.mercadolibre.com'+path,params=params,headers={'Authorization':'Bearer '+token},timeout=25)
  data=r.json()
  return {'item_id':item,'kind':kind,'status':r.status_code,'data':data}
 except Exception as e: return {'item_id':item,'kind':kind,'error':type(e).__name__}
with ThreadPoolExecutor(max_workers=3) as pool: results=list(pool.map(check,[(i,k) for i in items for k in ['total','30days']]))
Path('artifacts/store-link-traffic-20261007/results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
for r in results:
 d=r.get('data',{}); print(json.dumps({**{k:v for k,v in r.items() if k!='data'},'summary':{k:v for k,v in d.items() if k!='results'}},ensure_ascii=False))
