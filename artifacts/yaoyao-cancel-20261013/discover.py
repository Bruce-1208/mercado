import json,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bit.mercado_promotions import visible_store_options,_adapter,_marketplace_accounts,bit_mysql
out=Path(__file__).parent
stores=[s for s in visible_store_options() if any(x['group_name']=='遥遥领先' for x in s['sites'])]
(out/'scope.json').write_text(json.dumps(stores,ensure_ascii=False,indent=2))
def read(s):
 a=None;r={'id':s['id'],'name':s['name'],'accounts':[]}
 try:
  a,t=_adapter(dict(bit_mysql.get_mercado_store_token(s['id'])))
  sites={x['site_id'] for x in s['sites'] if x['group_name']=='遥遥领先'}
  for account in _marketplace_accounts(a.client,str(t['meli_user_id'])):
   if account['site_id'] not in sites:continue
   entry={'seller_id':str(account['user_id']),'site_id':account['site_id']}
   try:entry['promotions']=list(a.iter_promotions(entry['seller_id']))
   except Exception as e:entry['error']=str(e)
   r['accounts'].append(entry)
 except Exception as e:r['error']=str(e)
 finally:
  if a:a.client.session.close()
 (out/f"discovery-{s['id']}.json").write_text(json.dumps(r,ensure_ascii=False,indent=2))
 print(json.dumps({'store':s['name'],'accounts':len(r['accounts']),'promotions':sum(len(x.get('promotions',[])) for x in r['accounts']),'errors':[x.get('error') for x in r['accounts'] if x.get('error')]+([r['error']] if r.get('error') else [])},ensure_ascii=False),flush=True)
 return r
with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(read,stores))
(out/'discovery.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
