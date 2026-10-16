"""Resume original products in the current enterprise with existing actor credentials."""
import os,sys,json
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('BIT_SERVICE_MODE','web')
from bit import bit_interface as app,browser_extension_models,product_access
from erp.mercadolibre_collection_store import _connect_list,get_product_items_by_ids
from erp.ai_original_products import original_generation_skip_reason
from erp.ai_generation_budget import QWEN_MODEL,QWEN_BASE_URL
OUT=Path(__file__).resolve().parent
admin=app.get_workbench_user(user_id=1)
c=_connect_list()
with c.cursor() as q:
 q.execute("SELECT id FROM erp_mercadolibre_products WHERE source_type='ai_original' AND organization_key=%s ORDER BY id",(admin['organization_key'],));ids=[r['id'] for r in q.fetchall()]
 q.execute("SELECT id FROM mercado_store_tokens WHERE enabled=1 AND site_id='CBT' ORDER BY id LIMIT 1");token=q.fetchone()['id']
c.close()
with product_access.scope(admin): rows=get_product_items_by_ids(ids)
groups=defaultdict(list);results=[]
for row in rows:
 s=row['source_snapshot_json'];s=json.loads(s) if isinstance(s,str) else s
 a=s.get('ai_original') or {}
 reason=original_generation_skip_reason(row)
 if reason:
  results.append({'product_id':row['id'],'status':'skipped','message':reason});continue
 uid=int(a.get('generated_by_user_id') or row.get('owner_user_id') or (7 if a.get('generated_by')=='向子立' else 1))
 groups[uid].append(row)
print(json.dumps({'total':len(rows),'skipped':len(results),'groups':{uid:len(rs) for uid,rs in groups.items()}},ensure_ascii=False),flush=True)
(OUT/'recovery-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
for uid,batch in groups.items():
 actor=app.get_workbench_user(user_id=uid)
 assert actor and actor['organization_key']==admin['organization_key']
 key=browser_extension_models.get_api_key(uid,'dashscope',app.app.secret_key)
 if not key:
  results.extend({'product_id':r['id'],'status':'blocked','message':'生成账号未配置百炼凭证'} for r in batch)
 else:
  with product_access.scope(admin):
   app._run_ai_original_task(batch,api_key=key,model=QWEN_MODEL,base_url=QWEN_BASE_URL,
     workers=3,category_token_id=token,generated_by=actor.get('display_name') or actor['username'],
     billing_actor=app.ai_billing_actor_context(actor),cost_control=True)
  results.extend({k:v for k,v in r.items() if k!='generation_budget'} for r in app._ai_original_task_snapshot()['results'])
 (OUT/'recovery-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
 print(json.dumps({'actor_id':uid,'finished':len(batch)},ensure_ascii=False),flush=True)
with product_access.scope(admin): fresh=get_product_items_by_ids(ids)
(OUT/'after.json').write_text(json.dumps(fresh,ensure_ascii=False,default=str))
print('REPAIR COMPLETE',flush=True)
