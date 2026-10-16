import os,sys,json
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));os.environ.setdefault('BIT_SERVICE_MODE','web')
from bit import bit_interface as app,browser_extension_models
from erp.mercadolibre_collection_store import get_product_items_by_ids
from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient
from erp.ai_original_products import complete_marketplace_category
from erp.ai_original_cost_control import product_generation_lock
from erp.ai_generation_budget import BudgetedChat,QWEN_MODEL,QWEN_BASE_URL
from erp.ai_usage_billing import bind_usage
OUT=Path(__file__).resolve().parent
class FactsReady(Exception):pass
pid=13088985
with product_generation_lock(pid) as check:
 row=get_product_items_by_ids([pid])[0];snapshot=row['source_snapshot_json'];snapshot=json.loads(snapshot) if isinstance(snapshot,str) else deepcopy(snapshot)
 state=snapshot['ai_original'];rev=int(state.get('edit_revision') or 0);budgetrev=int((state.get('generation_budget') or {}).get('revision') or 0)
 def save_budget(ledger):
  global budgetrev
  check();state['generation_budget']=ledger
  app.db_update_ai_original_product(pid,{'source_snapshot_json':snapshot,'expected_ai_original_revision':rev,'expected_generation_budget_revision':budgetrev})
  budgetrev=ledger['revision']
 chat=BudgetedChat(ledger=state.get('generation_budget'),on_update=save_budget,before_request=check,enforce_cap=False,model=QWEN_MODEL);chat.cost_control=True
 def saved(values):
  if not values.get('attributes_ready'):return
  state.update(attributes=deepcopy(values['attributes']),status='failed',failed_step='variations',workflow_status='needs_variations',error='商品类目和属性已补齐，SKU 规格映射仍需核对')
  state.setdefault('category_checkpoint',{}).update(values)
  app.db_update_ai_original_product(pid,{'source_snapshot_json':snapshot,'expected_ai_original_revision':rev})
  raise FactsReady()
 key=browser_extension_models.get_api_key(1,'dashscope',app.app.secret_key)
 try:
  bind_usage(complete_marketplace_category,**app.ai_billing_actor_context(app.get_workbench_user(user_id=1)),task_id=row['source_item_id'])(
   snapshot['original_1688'],state.get('generated_copy') or state,DatabaseMercadoLibreClient(1),
   api_key=key,model=QWEN_MODEL,base_url=QWEN_BASE_URL,chat=chat,
   category_override={'category_id':row['category_id'],'category_name':row.get('category_name')},on_category_ready=saved)
 except FactsReady:pass
 fresh=get_product_items_by_ids([pid])[0]
 (OUT/f'{pid}-after.json').write_text(json.dumps(fresh,ensure_ascii=False,default=str))
 print(json.dumps({'id':pid,'category':fresh['category_id'],'attributes':state['attributes']},ensure_ascii=False))
