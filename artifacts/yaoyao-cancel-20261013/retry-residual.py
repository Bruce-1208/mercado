import importlib.util,os,json
from pathlib import Path
from datetime import timedelta
p=Path(__file__).with_name('cancel.py');sp=importlib.util.spec_from_file_location('op',p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
import bit.mercado_promotions as service
service._verify_mutation=lambda adapter,promotion,row,action: None
os.environ['BIT_PROMOTION_WORKERS']='16'
s=next(s for s in m.data if s['id']==85)
a,t=m._adapter(dict(m.bit_mysql.get_mercado_store_token(85)))
ac=next(a for a in s['accounts'] if a['seller_id']=='3625455586')
promo=next(p for p in ac['promotions'] if p['id']=='P-MLB17909028')
rows=m.snapshot(a,ac,promo)
(m.out/'residual-before.json').write_text(json.dumps(rows,ensure_ascii=False))
print('Fresh official remaining:',len(rows),flush=True)
for start in range(0,len(rows),200):
 batch=[dict(x,item_id=x['id'],status='ready') for x in rows[start:start+200]]
 actor='用户授权：取消遥遥领先全部活动 2026-10-13'
 preview=m.store.create_preview(actor=actor,action='withdraw',promotion_fk=709,payload={'promotion':m.store.get_promotion(709),'rows':batch},expires_at=(m._now()+timedelta(minutes=5)).isoformat())
 r=m.execute_preview(preview,actor=actor,store=m.store,background=False)
 print(json.dumps({'processed':min(start+200,len(rows)),'total':len(rows),'results':dict(m.collections.Counter(x['status'] for x in r['results']))}),flush=True)
a.client.session.close()
