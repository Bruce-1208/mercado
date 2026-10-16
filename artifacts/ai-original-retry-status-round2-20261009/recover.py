"""Recover failed and abandoned drafts through the regular generation worker."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('BIT_SERVICE_MODE', 'web')
from bit import bit_interface as app
from bit import browser_extension_models
from erp.mercadolibre_collection_store import _connect_list, get_product_items_by_ids
from erp.ai_generation_budget import QWEN_MODEL, QWEN_BASE_URL

OUT = Path(__file__).resolve().parent
IDS = [16326848, 16327260]
connection = _connect_list()
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM mercado_store_tokens WHERE enabled=1 AND site_id='CBT' ORDER BY display_name,id LIMIT 1")
        token_id = cursor.fetchone()['id']
    connection.rollback()
finally:
    connection.close()

summaries = []
for product_id in IDS:
    rows = get_product_items_by_ids([product_id])
    assert len(rows) == 1
    row = rows[0]
    snapshot = row['source_snapshot_json']
    if isinstance(snapshot, str): snapshot = json.loads(snapshot)
    previous = snapshot['ai_original']
    (OUT / f'{product_id}-before.json').write_text(json.dumps(row, ensure_ascii=False, default=str))
    if previous['status'] == 'processing':
        from erp.ai_original_cost_control import product_generation_lock
        with product_generation_lock(product_id):
            previous.update(status='failed', error='恢复服务重启前已中断的生成任务')
            app.db_update_ai_original_product(product_id, {'source_snapshot_json': snapshot})
        rows = get_product_items_by_ids([product_id])
    if previous['status'] != 'failed':
        print(json.dumps({'id': product_id, 'skipped_status': previous['status']}), flush=True)
        continue
    actor = previous['generated_by']
    user_id = {'管理员': 1, '向子立': 7}[actor]
    api_key = browser_extension_models.get_api_key(user_id, 'dashscope', app.app.secret_key)
    assert api_key, 'Original generation account has no configured key'
    app._run_ai_original_task(rows, api_key=api_key, model=QWEN_MODEL,
        base_url=QWEN_BASE_URL, workers=1, category_token_id=token_id,
        generated_by=actor, billing_actor=app.ai_billing_actor_context(app.get_workbench_user(user_id=user_id)), template_id=previous.get('template_id') or 'standard', cost_control=True)
    state = app._ai_original_task_snapshot()
    summaries.extend(state['results'])
    print(json.dumps(state['results'], ensure_ascii=False), flush=True)
    fresh = get_product_items_by_ids([product_id])[0]
    (OUT / f'{product_id}-after.json').write_text(json.dumps(fresh, ensure_ascii=False, default=str))
    (OUT / 'recovery-results.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2))
