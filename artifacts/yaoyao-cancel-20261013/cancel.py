import sys,json,os,collections
from pathlib import Path
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bit.mercado_promotions import _adapter,bit_mysql,visible_store_options,_now,execute_preview
from erp.mercadolibre_promotion_store import PromotionStore,APPLIED_STATUSES
out=Path(__file__).parent
os.environ['BIT_PROMOTION_WORKERS']='16'
scope={s['id']:s for s in visible_store_options()}
data=json.loads((out/'discovery.json').read_text())
store=PromotionStore()
with store._connect() as c:
 backup=[dict(x) for x in c.execute('SELECT * FROM promotions') if x['token_id'] in {s['id'] for s in data}]
if not (out/'local-before.json').exists():(out/'local-before.json').write_text(json.dumps(backup,ensure_ascii=False,indent=2))
def snapshot(a,account,p):
 rows=[]
 for status in ('started','pending'):
  cursor='';seen=set();n=0
  while True:
   params={'user_id':account['seller_id'],'promotion_type':p['type'],'status':status,'limit':50}
   if cursor:params['searchAfter']=cursor
   page=a.client.request('GET',f"/marketplace/seller-promotions/promotions/{p['id']}/items",params=params,headers=a._headers())
   results=page.get('results');paging=page.get('paging') or {}
   if results is None and paging.get('total')==0:results=[]
   if not isinstance(results,list) or any(not isinstance(x,dict) or not (x.get('id') or x.get('item_id')) for x in results):raise ValueError('Incomplete filtered response: '+json.dumps(page)[:500])
   if any(x.get('status')!=status for x in results):raise ValueError('Platform did not honor status filter')
   rows.extend(results);n+=len(results)
   if n and n%500==0:print(json.dumps({'scan':p['id'],'seller':account['seller_id'],'status':status,'read':n},ensure_ascii=False),flush=True)
   cursor=str(paging.get('searchAfter') or paging.get('search_after') or '')
   if not cursor:
    if paging.get('total') is not None and n<int(paging['total']):raise ValueError('Incomplete filtered pagination')
    break
   if cursor in seen or not results:raise ValueError('Repeated/empty cursor page')
   seen.add(cursor)
 return rows

def run(s):
 a=None;report={'id':s['id'],'name':s['name'],'campaigns':[]}
 try:
  current=scope[s['id']]
  sites={x['site_id'] for x in current['sites'] if x['group_name']=='遥遥领先'}
  a,t=_adapter(dict(bit_mysql.get_mercado_store_token(s['id'])))
  for account in s['accounts']:
   if account['site_id'] not in sites:raise ValueError('Scope changed')
   if account.get('error'):raise ValueError(account['error'])
   for p in account['promotions']:
    result={'promotion_id':p['id'],'type':p['type'],'site':account['site_id'],'seller_id':account['seller_id']}
    report['campaigns'].append(result)
    try:
     if p['type'] not in ('DEAL','SMART'):raise ValueError('Needs separate type handling')
     rows=snapshot(a,account,p)
     result['before']=len(rows)
     print(json.dumps({'store':s['name'],**result},ensure_ascii=False),flush=True)
     pk=store.upsert_promotion(token_id=s['id'],store_name=s['name'],salesperson='管理员',group_name='遥遥领先',application_id=str(t.get('application_id') or ''),seller_id=account['seller_id'],site_id=account['site_id'],row=p)
     result['local_id']=pk
     (out/f"before-{pk}.json").write_text(json.dumps(rows,ensure_ascii=False))
     result['jobs']=[]
     for start in range(0,len(rows),100):
      batch=[dict(x,item_id=x.get('id') or x['item_id'],status='ready') for x in rows[start:start+100]]
      actor='用户授权：取消遥遥领先全部活动 2026-10-13'
      preview=store.create_preview(actor=actor,action='withdraw',promotion_fk=pk,payload={'promotion':store.get_promotion(pk),'rows':batch},expires_at=(_now()+timedelta(minutes=5)).isoformat())
      r=execute_preview(preview,actor=actor,store=store,background=False)
      result['jobs'].append(r['job_id'])
      print(json.dumps({'store':s['name'],'promotion':p['id'],'processed':min(start+100,len(rows)),'total':len(rows),'results':dict(collections.Counter(x['status'] for x in r['results']))},ensure_ascii=False),flush=True)
     remaining=snapshot(a,account,p)
     result['remaining']=len(remaining)
     (out/f"after-{pk}.json").write_text(json.dumps(remaining,ensure_ascii=False))
     if not remaining:
      with store._connect() as c:
       c.execute("UPDATE promotion_items SET status_raw='withdrawn' WHERE promotion_fk=? AND status_raw IN ("+','.join('?' for _ in APPLIED_STATUSES)+')',[pk,*APPLIED_STATUSES])
    except Exception as e:result['error']=str(e)
    (out/f"result-{s['id']}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
   # Expire only absent campaigns for this exact seller after complete discovery.
   ids=[p['id'] for p in account['promotions']]
   with store._connect() as c:
    c.execute("UPDATE promotions SET status_raw='finished' WHERE token_id=? AND seller_id=? AND site_id=?"+(' AND promotion_id NOT IN ('+','.join('?' for _ in ids)+')' if ids else ''),[s['id'],account['seller_id'],account['site_id'],*ids])
 except Exception as e:report['error']=str(e)
 finally:
  if a:a.client.session.close()
 (out/f"result-{s['id']}.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'completed_store':s['name'],'before':sum(x.get('before',0) for x in report['campaigns']),'remaining':sum(x.get('remaining',0) for x in report['campaigns']),'errors':[x['error'] for x in report['campaigns'] if x.get('error')]+([report['error']] if report.get('error') else [])},ensure_ascii=False),flush=True)
 return report
if __name__=='__main__':
 selected=[s for s in data if not sys.argv[1:] or str(s['id']) in sys.argv[1:]]
 with ThreadPoolExecutor(max_workers=4) as pool:reports=list(pool.map(run,selected))
 (out/('retry-result.json' if sys.argv[1:] else 'result.json')).write_text(json.dumps(reports,ensure_ascii=False,indent=2))
