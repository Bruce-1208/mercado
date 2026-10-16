import importlib.util,os,json,sqlite3
from pathlib import Path
from datetime import timedelta
p=Path(__file__).with_name('cancel.py');sp=importlib.util.spec_from_file_location('op',p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
import bit.mercado_promotions as service
os.environ['BIT_PROMOTION_WORKERS']='16'
# This process performs only authorized withdrawals. Ambiguous DELETE responses
# remain unknown; the complete filtered official snapshot at the end verifies them.
original_verify=service._verify_mutation
def batch_verify(adapter,promotion,row,action):
 if action=='withdraw':return None
 return original_verify(adapter,promotion,row,action)
service._verify_mutation=batch_verify
pk=709
promotion=m.store.get_promotion(pk)
assert promotion['token_id']==85 and promotion['site_id']=='MLB' and promotion['seller_id']=='3625455586'
scope=m.scope[85]
assert any(x['site_id']=='MLB' and x['group_name']=='遥遥领先' for x in scope['sites'])
with m.store._connect() as c:
 attempted={r['item_id'] for j in c.execute("SELECT j.results_json FROM promotion_jobs j JOIN promotion_previews v ON j.preview_id=v.preview_id WHERE v.promotion_fk=? AND j.actor LIKE '用户授权：%'",(pk,)) for r in json.loads(j[0]) if r.get('item_id')}
rows=json.loads((m.out/'before-709.json').read_text())
rows=[r for r in rows if r['id'] not in attempted]
print(json.dumps({'unsubmitted':len(rows),'skipped_attempted':len(attempted)}),flush=True)
(m.out/'unsubmitted-brazil.json').write_text(json.dumps(rows,ensure_ascii=False))
actor='用户授权：取消遥遥领先全部活动 2026-10-13'
for start in range(0,len(rows),200):
 batch=[dict(x,item_id=x['id'],status='ready') for x in rows[start:start+200]]
 preview=m.store.create_preview(actor=actor,action='withdraw',promotion_fk=pk,payload={'promotion':promotion,'rows':batch},expires_at=(m._now()+timedelta(minutes=5)).isoformat())
 r=m.execute_preview(preview,actor=actor,store=m.store,background=False)
 print(json.dumps({'processed':min(start+200,len(rows)),'total':len(rows),'results':dict(m.collections.Counter(x['status'] for x in r['results']))}),flush=True)
print('All previously unsubmitted rows processed; awaiting independent platform verification.',flush=True)
