"""Resume the real draft from the fully validated fallback preparation result."""
import json
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('BIT_SERVICE_MODE','web')
from bit import bit_interface as app, browser_extension_models
from erp.mercadolibre_collection_store import get_product_items_by_ids, _connect_list
from erp.ai_original_cost_control import product_generation_lock
from erp.ai_generation_budget import QWEN_MODEL,QWEN_BASE_URL
OUT=Path(__file__).resolve().parent
verified=json.loads((OUT/'live-fallback.json').read_text())
assert verified['status']=='completed' and not verified['result']['missing_required_attributes']
pid=verified['product_id']
with product_generation_lock(pid):
    row=get_product_items_by_ids([pid])[0]
    snapshot=row['source_snapshot_json']
    if isinstance(snapshot,str): snapshot=json.loads(snapshot)
    state=snapshot['ai_original']
    assert state['status']=='failed' and state['failed_step']=='variations'
    checkpoint={**verified['result'],'variations_ready':True}
    state.update(checkpoint)
    state['category_checkpoint']=checkpoint
    state['preparation'].update(category_ready=True,variations_ready=True,required_ready=False)
    app.db_update_ai_original_product(pid,{'source_snapshot_json':snapshot,
        'expected_ai_original_revision':int(state.get('edit_revision') or 0)})
connection=_connect_list()
with connection.cursor() as cur:
    cur.execute("SELECT id FROM mercado_store_tokens WHERE enabled=1 AND site_id='CBT' ORDER BY display_name,id LIMIT 1")
    token_id=cur.fetchone()['id']
connection.close()
key=browser_extension_models.get_api_key(1,'dashscope',app.app.secret_key)
app._run_ai_original_task(get_product_items_by_ids([pid]),api_key=key,model=QWEN_MODEL,
    base_url=QWEN_BASE_URL,workers=1,category_token_id=token_id,generated_by=state['generated_by'],billing_actor=app.ai_billing_actor_context(app.get_workbench_user(user_id=1)),cost_control=True)
result=app._ai_original_task_snapshot()['results']
(OUT/'verified-recovery-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
(OUT/f'{pid}-after.json').write_text(json.dumps(get_product_items_by_ids([pid])[0],ensure_ascii=False,default=str))
print(json.dumps(result,ensure_ascii=False))
