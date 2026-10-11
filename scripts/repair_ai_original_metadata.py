"""Fill missing CBT metadata without waiting on supplier images. Preview by default."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def needs_repair(row, state):
    return (not re.fullmatch(r'CBT[0-9]+', str(row.get('category_id') or ''))
            or not any(a.get('id') not in ('BRAND', 'ITEM_CONDITION') for a in state.get('attributes') or [])
            or bool(state.get('missing_required_attributes'))
            or state.get('failed_step') in ('category', 'attributes', 'variations'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--actor-id', type=int, default=1)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--ids', type=int, nargs='*')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    os.environ.setdefault('BIT_SERVICE_MODE', 'web')
    from erp.mercadolibre_collection_store import _connect_list, get_product_items_by_ids
    connection = _connect_list()
    with connection.cursor() as cursor:
        cursor.execute("SELECT id,category_id,source_snapshot_json FROM erp_mercadolibre_products WHERE source_type='ai_original'")
        rows = cursor.fetchall()
        cursor.execute("SELECT id FROM mercado_store_tokens WHERE enabled=1 AND site_id='CBT' ORDER BY id LIMIT 1")
        token = cursor.fetchone()
    connection.close()
    ids = [r['id'] for r in rows if (not args.ids or r['id'] in args.ids)
           and needs_repair(r, json.loads(r['source_snapshot_json'] or '{}').get('ai_original') or {})]
    print(json.dumps({'candidates': ids, 'count': len(ids), 'apply': args.apply}), flush=True)
    if not args.apply:
        return
    from bit import bit_interface as app, browser_extension_models
    from erp.ai_original_cost_control import product_generation_lock
    from erp.ai_original_products import prepare_ai_original_product, OriginalPreparationError
    from erp.ai_generation_budget import QWEN_MODEL, QWEN_BASE_URL
    from erp.mercadolibre_batch_publish import DatabaseMercadoLibreClient
    from erp.ai_usage_billing import bind_usage
    actor = app.get_workbench_user(user_id=args.actor_id)
    key = browser_extension_models.get_api_key(args.actor_id, 'dashscope', app.app.secret_key)
    if not key or not token:
        raise RuntimeError('缺少执行账号的模型凭证或 CBT 授权店铺')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    def repair(pid):
        try:
            with product_generation_lock(pid) as check_owner:
                row = get_product_items_by_ids([pid])[0]
                snapshot = row['source_snapshot_json']
                if isinstance(snapshot, str): snapshot = json.loads(snapshot)
                snapshot = deepcopy(snapshot)
                state = snapshot.get('ai_original') or {}
                if not needs_repair(row, state): return {'id': pid, 'status': 'already_repaired'}
                (output / f'{pid}-before.json').write_text(json.dumps(row, ensure_ascii=False, default=str))
                revision = int(state.get('edit_revision') or 0)
                budget_revision = int((state.get('generation_budget') or {}).get('revision') or 0)
                # Acquiring the distributed lock proves an old processing marker has no worker.
                state.update(status='pending', error='')
                snapshot['ai_original'] = state
                row['source_snapshot_json'] = snapshot
                def save(step, partial):
                    nonlocal budget_revision
                    check_owner()
                    snapshot['ai_original'] = deepcopy(partial)
                    snapshot.setdefault('marketplace_listings', {})['mercadolibre'] = deepcopy(partial)
                    change = {'source_snapshot_json': snapshot, 'expected_ai_original_revision': revision,
                              'review_status': 'unreviewed'}
                    next_budget_revision = int((partial.get('generation_budget') or {}).get('revision') or 0)
                    if next_budget_revision != budget_revision:
                        change['expected_generation_budget_revision'] = budget_revision
                    if partial.get('category_id'):
                        change.update(category_id=partial['category_id'], category_name=partial.get('category_name') or '')
                    app.db_update_ai_original_product(pid, change)
                    budget_revision = next_budget_revision
                try:
                    result = bind_usage(prepare_ai_original_product,
                        **app.ai_billing_actor_context(actor), task_id=str(row['source_item_id']))(
                        row, api_key=key, model=QWEN_MODEL, base_url=QWEN_BASE_URL,
                        category_client=DatabaseMercadoLibreClient(token['id']), cost_control=True,
                        before_model_request=check_owner, on_preparation_checkpoint=save, metadata_only=True)
                    final = result['ai_original']
                    save('metadata_ready', final)
                    status = 'metadata_ready'
                    error = ''
                except OriginalPreparationError as exc:
                    final = exc.state
                    save(exc.step, final)
                    status = exc.step + '_incomplete'
                    error = str(exc)
                fresh = get_product_items_by_ids([pid])[0]
                (output / f'{pid}-after.json').write_text(json.dumps(fresh, ensure_ascii=False, default=str))
                return {'id': pid, 'status': status, 'category_id': fresh.get('category_id'),
                        'attributes': len(final.get('attributes') or []), 'error': error,
                        'missing': final.get('missing_required_attributes') or []}
        except Exception as exc:
            return {'id': pid, 'status': 'error', 'error': str(exc)}
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
        from concurrent.futures import as_completed
        results = []
        for job in as_completed([pool.submit(repair, pid) for pid in ids]):
            result = job.result()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
