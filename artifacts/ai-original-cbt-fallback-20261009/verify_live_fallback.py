"""Exercise discovery outage with a real failed product, model and live schema."""
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('BIT_SERVICE_MODE', 'web')
from bit import bit_interface as app, browser_extension_models
from erp.mercadolibre_collection_store import _connect_list, get_product_items_by_ids
from erp.ai_original_products import complete_marketplace_category, OriginalPreparationError, _listing_json_chat
from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient
from erp.ai_generation_budget import QWEN_MODEL, QWEN_BASE_URL
OUT = Path(__file__).resolve().parent
row = get_product_items_by_ids([16326847])[0]
snapshot = row['source_snapshot_json']
if isinstance(snapshot, str): snapshot = json.loads(snapshot)
original = snapshot['original_1688']
copy = snapshot['ai_original']['generated_copy']
c = _connect_list()
with c.cursor() as cur:
    cur.execute("SELECT id FROM mercado_store_tokens WHERE enabled=1 AND site_id='CBT' ORDER BY display_name,id LIMIT 1")
    token_id = cur.fetchone()['id']
c.close()
real = DatabaseMercadoLibreClient(token_id)
calls = []
class OutageClient:
    def request(self, method, path, **kwargs):
        calls.append(path)
        if 'domain_discovery' in path:
            raise RuntimeError('Verification: simulated discovery outage')
        return real.request(method, path, **kwargs)
key = browser_extension_models.get_api_key(1, 'dashscope', app.app.secret_key)
assert key
report = {'product_id': row['id'], 'simulated_discovery_outage': True, 'calls': calls}
def checkpoint(category):
    report['category'] = category
    (OUT/'live-fallback.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
try:
    result = app.bind_ai_usage(complete_marketplace_category,
        **app.ai_billing_actor_context(app.get_workbench_user(user_id=1)),
        task_id=str(row["source_item_id"]))(original, copy, OutageClient(), api_key=key,
        model=QWEN_MODEL, base_url=QWEN_BASE_URL, chat=_listing_json_chat(model=QWEN_MODEL, base_url=QWEN_BASE_URL), on_category_ready=checkpoint)
    report.update(status='completed', result=result)
except OriginalPreparationError as exc:
    report.update(status='failed', step=exc.step, error=str(exc), state=exc.state)
(OUT/'live-fallback.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({'status':report['status'], 'category': report.get('category'), 'step': report.get('step'), 'error':report.get('error')},ensure_ascii=False))
