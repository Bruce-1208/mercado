import sys,os,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));os.environ.setdefault('BIT_SERVICE_MODE','web')
from bit import bit_interface as app,browser_extension_models,product_access
from erp.mercadolibre_collection_store import _connect_list,get_product_items_by_ids
from erp.ai_generation_budget import QWEN_MODEL,QWEN_BASE_URL
actor=app.get_workbench_user(user_id=1);key=browser_extension_models.get_api_key(1,'dashscope',app.app.secret_key)
c=_connect_list()
with c.cursor() as q:
 q.execute("SELECT id FROM mercado_store_tokens WHERE enabled=1 AND site_id='CBT' ORDER BY id LIMIT 1");token=q.fetchone()['id']
c.close()
with product_access.scope(actor):
 rows=get_product_items_by_ids([16764276])
 app._run_ai_original_task(rows,api_key=key,model=QWEN_MODEL,base_url=QWEN_BASE_URL,workers=1,
  category_token_id=token,generated_by=actor['display_name'],billing_actor=app.ai_billing_actor_context(actor),cost_control=True)
results=[{k:v for k,v in r.items() if k!='generation_budget'} for r in app._ai_original_task_snapshot()['results']]
Path(__file__).with_name('conflict-retry-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False))
