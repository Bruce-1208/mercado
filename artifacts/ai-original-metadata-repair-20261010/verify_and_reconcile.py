"""Read back repaired metadata and reconcile this run's provider usage journal."""
import json,sys,sqlite3,re
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from erp.mercadolibre_collection_store import get_product_items_by_ids, update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
from erp.ai_original_products import _source_supports_part_number
from erp.ai_generation_budget import BudgetedChat,QWEN_MODEL
from erp.ai_usage_billing import _path
from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient
from erp.mercadolibre_attribute_rules import is_required_attribute
client=DatabaseMercadoLibreClient(1)
schemas={}
OUT=Path(__file__).resolve().parent
START='2026-10-10T10:11:00+00:00'
db=sqlite3.connect(_path());db.row_factory=sqlite3.Row
prior=json.loads((OUT/'verification.json').read_text()) if (OUT/'verification.json').exists() else []
prior_removed={r['id']:r.get('removed_unsupported',[]) for r in prior}
report=[]
for file in sorted(OUT.glob('*-after.json')):
 pid=int(file.name.split('-')[0])
 with product_generation_lock(pid):
  row=get_product_items_by_ids([pid])[0]
  snapshot=row['source_snapshot_json'];snapshot=json.loads(snapshot) if isinstance(snapshot,str) else deepcopy(snapshot)
  state=snapshot['ai_original']; original=snapshot['original_1688']
  manual={a.get('id') for a in state.get('manual_attributes') or []}
  removed=[]
  facts=json.dumps(original,ensure_ascii=False).lower()
  def keep(a):
   if a.get('id') in manual:return True
   if pid==13088985 and a.get('id') in {'POWER_SUPPLY_TYPE','INCLUDES_CELL_BATTERIES','WITH_REMOTE_CONTROL','AMBIENTS'}:
    removed.append(a);return False
   if a.get('id')=='PART_NUMBER' and not _source_supports_part_number(original,a.get('value_name')):
    removed.append(a);return False
   if (a.get('id')=='BLADE_MATERIAL' and str(a.get('value_name') or '').lower()=='steel'
       and not re.search('钢|鋼|steel',facts)):
    removed.append(a);return False
   return True
  state['attributes']=[a for a in state.get('attributes') or [] if keep(a)]
  if removed:
   checkpoint=state.get('category_checkpoint') or {}
   checkpoint['attributes']=deepcopy(state['attributes'])
   category=row['category_id']
   if category not in schemas: schemas[category]=client.request('GET',f'/categories/{category}/attributes')
   if any(a['id']=='PART_NUMBER' for a in removed) and any(a.get('id')=='PART_NUMBER' and is_required_attribute(a) for a in schemas[category]):
    missing=[a for a in state.get('missing_required_attributes') or [] if a.get('id')!='PART_NUMBER']
    missing.append({'id':'PART_NUMBER','name':'Part number','reason':'原始资料未提供实际零件编号'})
    state['missing_required_attributes']=missing
    checkpoint['missing_required_attributes']=deepcopy(missing)
    if state.get('status')!='failed':
     state.update(status='failed',failed_step='attributes',workflow_status='needs_attributes',
                  error='原始资料未提供实际零件编号 PART_NUMBER，请按供应商资料补充；不能用商品名称代替')
    state.setdefault('preparation',{})['required_ready']=False
   snapshot.setdefault('source',{})['attributes']=deepcopy(state['attributes'])
  usage=[dict(r) for r in db.execute("SELECT * FROM ai_usage_records WHERE actor_id=1 AND task_id=? AND datetime(created_at)>=datetime(?) ORDER BY id",(row['source_item_id'],START))]
  old=deepcopy(state.get('generation_budget') or {})
  revision=int(old.get('revision') or 0)
  # Preserve same-day calls that preceded this repair (timezone-aware comparison).
  from datetime import datetime
  calls=[c for c in old.get('calls') or [] if not c.get('started_at') or datetime.fromisoformat(c['started_at'])<datetime.fromisoformat(START)]
  for u in usage:
   entry={'state':'complete' if u['usage_known'] and u['estimated_cost_cny'] is not None else 'unknown',
          'started_at':u['created_at'],'ended_at':u['created_at'],'model':u['model'],
          'request_hash':'usage-journal:'+u['record_id'],'usage_record_id':u['record_id'],
          'reconciled_from':'ai_usage_records','reserved_nano':0}
   if entry['state']=='complete':
    entry.update(prompt_tokens=u['input_tokens'],completion_tokens=u['output_tokens'],
                 cache_hit_tokens=u['cached_tokens'] or 0,cache_miss_tokens=u['input_tokens']-(u['cached_tokens'] or 0),
                 cost_nano=round(u['estimated_cost_cny']*1_000_000_000),rate_period='standard')
   else:entry.update(error_type='UnconfirmedProviderUsage',reservation_unavailable=True)
   calls.append(entry)
  if usage:
   old.update(version=1,revision=revision+1,calls=calls)
   state['generation_budget']=BudgetedChat(ledger=old,enforce_cap=False,model=QWEN_MODEL).snapshot()
  change={'source_snapshot_json':snapshot,'expected_ai_original_revision':int(state.get('edit_revision') or 0)}
  if usage:change['expected_generation_budget_revision']=revision
  update_ai_original_product(pid,change)
  fresh=get_product_items_by_ids([pid])[0]
  (OUT/f'{pid}-verified.json').write_text(json.dumps(fresh,ensure_ascii=False,default=str))
  report.append({'id':pid,'category_id':fresh.get('category_id'),'attributes':len(state.get('attributes') or []),
                 'status':state['status'],'missing':state.get('missing_required_attributes') or [],
                 'removed_unsupported':prior_removed.get(pid,[])+removed,'usage_records':len(usage)})
(OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))
