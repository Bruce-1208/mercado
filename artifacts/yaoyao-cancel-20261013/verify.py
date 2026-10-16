import importlib.util,sys,json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
p=Path(__file__).with_name('cancel.py');sp=importlib.util.spec_from_file_location('op',p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
def verify(s):
 a=None;r={'id':s['id'],'name':s['name'],'campaigns':[]}
 try:
  a,t=m._adapter(dict(m.bit_mysql.get_mercado_store_token(s['id'])))
  for ac in s['accounts']:
   promotions=list(a.iter_promotions(ac['seller_id']))
   for promotion in promotions:
    rec={'promotion_id':promotion['id'],'type':promotion['type'],'site_id':ac['site_id'],'seller_id':ac['seller_id']};r['campaigns'].append(rec)
    try:rec['remaining']=m.snapshot(a,ac,promotion)
    except Exception as e:rec['error']=str(e)
 except Exception as e:r['error']=str(e)
 finally:
  if a:a.client.session.close()
 (m.out/f"verification-{s['id']}.json").write_text(json.dumps(r,ensure_ascii=False,indent=2))
 print(json.dumps({'store':s['name'],'remaining':sum(len(x.get('remaining',[])) for x in r['campaigns']),'errors':[x['error'] for x in r['campaigns'] if x.get('error')]+([r['error']] if r.get('error') else [])},ensure_ascii=False),flush=True)
 return r
selected=[s for s in m.data if not sys.argv[1:] or str(s['id']) in sys.argv[1:]]
with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(verify,selected))
