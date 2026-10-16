"""Backfill only audited generator identities and actionable missing-input state."""
import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from erp.mercadolibre_collection_store import _connect_list,get_product_items_by_ids,update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
from erp.ai_original_state import source_input_state
out=Path(__file__).parent/'attribution';out.mkdir(exist_ok=True)
billing=sqlite3.connect('runtime_logs/ai_usage_billing.sqlite3');billing.row_factory=sqlite3.Row
actors={}
for row in billing.execute('SELECT actor_id,actor_name,task_id FROM ai_usage_records WHERE actor_id>0 AND actor_name IS NOT NULL ORDER BY created_at'):
    if row['actor_name']: actors[str(row['task_id'])]=dict(row)
c=_connect_list()
with c.cursor() as q:
    q.execute("SELECT id FROM erp_mercadolibre_products WHERE source_type='ai_original'"); ids=[r['id'] for r in q.fetchall()]
c.close();report=[]
for pid in ids:
    try:
        with product_generation_lock(pid) as check:
            row=get_product_items_by_ids([pid])[0];s=row['source_snapshot_json'];s=json.loads(s) if isinstance(s,str) else s
            old=json.loads(json.dumps(s,default=str));state=s.get('ai_original') or {}
            actor=actors.get(str(row.get('source_item_id') or ''))
            if actor and not state.get('generated_by'):
                state.update(generated_by=actor['actor_name'],generated_by_user_id=actor['actor_id'],generated_by_source='ai_usage_record')
            state=source_input_state(state,s.get('original_1688') or {})
            s['ai_original']=state
            if s==old: continue
            (out/f'{pid}-before.json').write_text(json.dumps(old,ensure_ascii=False))
            s.setdefault('marketplace_listings',{})['mercadolibre']=state.copy()
            check();update_ai_original_product(pid,{'source_snapshot_json':s,'expected_ai_original_revision':int(state.get('edit_revision') or 0)})
            report.append({'id':pid,'generated_by':state.get('generated_by'),'status':state.get('status')})
    except Exception as exc: report.append({'id':pid,'error':str(exc)})
(out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
