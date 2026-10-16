import sys,json,collections
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_promotion_store import PromotionStore,APPLIED_STATUSES
from bit.bit_runtime_lock import InterProcessLock
out=Path(__file__).parent;store=PromotionStore()
scope=json.loads((out/'scope.json').read_text());verified=[];remaining=[];locks=[]
for s in scope:
 v=json.loads((out/f"verification-{s['id']}.json").read_text());assert not v.get('error')
 assert all('remaining' in c and not c.get('error') for c in v['campaigns'])
 verified.append(v)
 for c in v['campaigns']:
  with store._connect() as db:
   local=db.execute('SELECT id FROM promotions WHERE token_id=? AND seller_id=? AND site_id=? AND promotion_id=? AND promotion_type=?',(s['id'],c['seller_id'],c['site_id'],c['promotion_id'],c['type'])).fetchall()
  current={r['id'] for r in c['remaining']}
  for i in c['remaining']:
   remaining.append({'store':s['name'],'token_id':s['id'],'site_id':c['site_id'],'promotion_id':c['promotion_id'],'item_id':i['id'],'platform_status':i['status'],'reason':'平台审核禁售 under_review/forbidden；取消接口返回 The item condition must be new'})
  for (pk,) in local:
   lease=InterProcessLock(f'promotion_campaign_{pk}',owner='用户授权取消结果核验')
   if not lease.acquire(timeout=0):locks.append(pk);continue
   try:
    with store._connect() as db:
     updates=[(r['item_id'],) for r in db.execute('SELECT item_id FROM promotion_items WHERE promotion_fk=?',(pk,)) if r['item_id'] not in current]
     db.executemany("UPDATE promotion_items SET status_raw='withdrawn' WHERE promotion_fk=? AND item_id=? AND status_raw IN ("+','.join('?' for _ in APPLIED_STATUSES)+')',[(pk,item,*APPLIED_STATUSES) for (item,) in updates])
    for r in c['remaining']:store.refresh_item_activity(pk,r['id'],r)
    for job in store.list_jobs(promotion_fk=pk,action='withdraw',limit=500):
     if not str(job['actor']).startswith('用户授权：'):continue
     results=job['results'];changed=False
     for r in results:
      if r.get('item_id') and r['item_id'] not in current and r.get('status')!='succeeded':
       r['original_message']=r.get('message','');r.update(status='succeeded',message='已通过本次官方参与状态完整分页核验，确认已退出');changed=True
     if changed:store.finish_job(job['job_id'],results=results)
   finally:lease.release()
with store._connect() as db:
 attempted=collections.defaultdict(set)
 for j in db.execute("SELECT j.results_json,p.token_id,p.promotion_id FROM promotion_jobs j JOIN promotion_previews v ON j.preview_id=v.preview_id JOIN promotions p ON p.id=v.promotion_fk WHERE j.actor LIKE '用户授权：%'"):
  for r in json.loads(j['results_json']):
   if r.get('item_id'):attempted[j['token_id']].add((j['promotion_id'],r['item_id']))
summary={'verified_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),'group':'遥遥领先','stores_checked':len(verified),'campaigns_checked':sum(len(v['campaigns']) for v in verified),'participations_processed':sum(len(x) for x in attempted.values()),'remaining_count':len(remaining),'cancelled_verified':sum(len(x) for x in attempted.values())-len(remaining),'stores_cleared':sum(not any(c['remaining'] for c in v['campaigns']) for v in verified),'remaining':remaining,'local_reconciliation_locked':locks}
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
