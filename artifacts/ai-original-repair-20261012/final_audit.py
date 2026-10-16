import json,sys,os
from pathlib import Path
from collections import Counter
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));os.environ.setdefault('BIT_SERVICE_MODE','web')
from bit import bit_interface as app,product_access
from erp.mercadolibre_collection_store import _connect_list,get_product_items_by_ids,update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
OUT=Path(__file__).resolve().parent
results=json.loads((OUT/'recovery-results.json').read_text())
for retry in json.loads((OUT/'conflict-retry-results.json').read_text()):
 results=[retry if r['product_id']==retry['product_id'] else r for r in results]
actor=app.get_workbench_user(user_id=1)
with product_access.scope(actor):
 for r in results:
  if r['status']!='blocked':continue
  with product_generation_lock(r['product_id']) as check:
   row=get_product_items_by_ids([r['product_id']])[0]
   s=row['source_snapshot_json'];s=json.loads(s) if isinstance(s,str) else s
   a=s.setdefault('ai_original',{});revision=int(a.get('edit_revision') or 0)
   # Only record this run's blocked pending rows; never replace another worker's result.
   if a.get('status') not in ('pending','failed','incomplete'):continue
   (OUT/f"{r['product_id']}-credential-before.json").write_text(json.dumps(row,ensure_ascii=False,default=str))
   a.update(status='failed',workflow_status='failed',failed_step='account',error=r['message'])
   s.setdefault('marketplace_listings',{})['mercadolibre']=dict(a)
   check();update_ai_original_product(r['product_id'],{'source_snapshot_json':s,'expected_ai_original_revision':revision})
 rows=get_product_items_by_ids([r['product_id'] for r in results])
(OUT/'final-products.json').write_text(json.dumps(rows,ensure_ascii=False,default=str))
(OUT/'final-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
issues=[]
for r in rows:
 a=r.get('ai_original') or {}
 if a.get('status')=='completed':continue
 issues.append({'product_id':r['id'],'title':(r.get('original_1688') or {}).get('title') or r.get('title'),
  'source_url':(r.get('original_1688') or {}).get('source_url'),'status':a.get('status'),
  'failed_step':a.get('failed_step'),'error':a.get('error'),'input_blockers':a.get('input_blockers')})
(OUT/'remaining-products.json').write_text(json.dumps(issues,ensure_ascii=False,indent=2))
print(json.dumps({'total':len(rows),'states':dict(Counter((r.get('ai_original') or {}).get('status') for r in rows)),
 'remaining_causes':dict(Counter(i['error'] for i in issues))},ensure_ascii=False))
