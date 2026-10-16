"""Targeted, visually verified image repair. No paid generation or publishing."""
import json
from copy import deepcopy
from pathlib import Path
from erp.mercadolibre_collection_store import _connect_list, update_ai_original_product
from erp.ai_original_cost_control import product_generation_lock
from erp.ai_original_images import prepare_variation_white_images, merge_recollected_sku_images
from erp.ai_original_platforms import validate_platform_listing
from erp.ai_original_state import manual_approval_issues
from erp.sku_matrix import supplier_skus, sync_supplier_skus

out = Path('artifacts/sku-image-repair-1041447098398')
with product_generation_lock(17572452) as check_owner:
    c = _connect_list()
    with c.cursor() as cur:
        cur.execute('SELECT * FROM erp_mercadolibre_products WHERE id=%s', (17572452,))
        row = cur.fetchone()
    c.close()
    snapshot = json.loads(row['source_snapshot_json'])
    revision = int(snapshot['ai_original'].get('edit_revision') or 0)
    original = snapshot['original_1688']
    assert row['source_item_id'] == '16881041447098398'
    assert [v['label'] for v in original['variations']] == ['毒液+手套 / S','毒液+手套 / M','毒液+手套 / L']
    # Gallery image 3 has been visually inspected: one front-facing costume with gloves.
    source = original['images'][3]
    collected = deepcopy(original)
    for sku in collected['variations']:
        sku.update(price=40, price_text='40', image_url=source, images=[source], image_status='confirmed',
                   image_source_basis='verified_shared_style', source_image_url=source)
    snapshot = merge_recollected_sku_images(snapshot, collected)
    sync_supplier_skus(snapshot, supplier_skus(collected['variations'], validate=True))
    def annotate(value):
        if isinstance(value, dict):
            if value.get('sku_id') in {'6227955879792','6227955879793','6227955879794'}:
                value.update(image_status='confirmed', image_source_basis='verified_shared_style')
            for child in value.values(): annotate(child)
        elif isinstance(value, list):
            for child in value: annotate(child)
    annotate(snapshot)
    state = snapshot['ai_original']
    assert all(state['preparation'].get(k) for k in ('copy_ready','category_ready','variations_ready','required_ready'))
    rows, cache = prepare_variation_white_images(state['variations'], item_id='1041447098398', cache=state.get('sku_image_cache'))
    state.update(variations=rows, sku_image_cache=cache, main_image_url=rows[0]['white_image_url'],
                 image_generation_method=rows[0]['image_generation_method'])
    state['preparation'].update(image_ready=True, sku_images_ready=True, image_path=rows[0]['white_image_path'])
    state.update(title_es='Disfraz infantil de Venom con músculos y guantes', title_pt='Fantasia de Venom para crianças com músculos e luvas')
    state['generated_copy'].update(title_es=state['title_es'], title_pt=state['title_pt'])
    state['validation'] = validate_platform_listing(state)
    assert not state['validation']['errors'], state['validation']
    candidate = dict(row, main_image_url=state['main_image_url'], net_proceeds_usd=7)
    state.update(status='completed', error='', failed_step='', current_step='completed', workflow_status='completed', input_blockers=[])
    issues = manual_approval_issues(candidate, state, snapshot['original_1688'])
    assert not issues, issues
    snapshot['marketplace_listings']['mercadolibre'] = deepcopy(state)
    snapshot.setdefault('source', {}).update(variations=deepcopy(rows), main_image_url=state['main_image_url'])
    snapshot['original_1688'].update(error_message='', scrape_status='ok')
    snapshot['source'].update(title=state['title_es'], net_proceeds_usd=7)
    snapshot['original_1688']['repair_note'] = '2026-10-14：核对图库单人正面图后关联三个尺码；用户确认各尺码采购价均为40元。'
    check_owner()
    update_ai_original_product(row['id'], {'source_snapshot_json':snapshot, 'main_image_url':state['main_image_url'],
        'expected_ai_original_revision':revision,
        'expected_generation_budget_revision':int((state.get('generation_budget') or {}).get('revision') or 0),
        'review_status':'unreviewed', 'title':state['title_es'], 'net_proceeds_usd':7})
    (out/'after-snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
    print(json.dumps({'status':state['status'],'image':rows[0]['white_image_path'],'validation':state['validation'],'remaining':issues},ensure_ascii=False),flush=True)
