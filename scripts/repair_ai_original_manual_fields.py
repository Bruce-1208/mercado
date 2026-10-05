"""Recover saved categories and packaging-only failures; no AI/API calls.

Run with --apply to persist; default is a read-only preview.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def repaired_snapshot(raw):
    snapshot = deepcopy(raw)
    state = snapshot.get('ai_original') or {}
    progress = state.get('preparation') or {}
    missing = state.get('missing_required_attributes') or []
    manual = {prefix + axis for prefix in ('PACKAGE_', 'SELLER_PACKAGE_')
              for axis in ('LENGTH', 'WIDTH', 'HEIGHT', 'WEIGHT')}
    # Recover only the precise old failure after all generated stages succeeded.
    if (state.get('status') != 'failed' or state.get('failed_step') != 'attributes'
            or not str(state.get('error') or '').startswith('DeepSeek 已尝试补全，仍缺少必填属性：')
            or not missing or any(a.get('id') not in manual for a in missing)
            or not all(progress.get(k) for k in ('image_ready', 'copy_ready', 'category_ready', 'variations_ready'))):
        return None
    copy = state.get('generated_copy') or {}
    if not all(copy.get(k) for k in ('title_es', 'title_pt', 'description_es', 'description_pt')):
        return None
    state.update(copy)
    # The category checkpoint contains the platform-mapped attributes/SKUs.
    checkpoint = state.get('category_checkpoint') or {}
    for key in ('attributes', 'variations'):
        if key in checkpoint:
            state[key] = deepcopy(checkpoint[key])
    state.update(status='completed', error='', failed_step='', workflow_status='completed',
                 current_step='completed', missing_required_attributes=[], target_platform='mercadolibre',
                 title=copy['title_es'], description=copy['description_es'], bullet_points=[])
    progress['required_ready'] = True
    from erp.ai_original_platforms import validate_platform_listing
    state['validation'] = validate_platform_listing(state, 'mercadolibre')
    snapshot.setdefault('marketplace_listings', {})['mercadolibre'] = deepcopy(state)
    source = snapshot.setdefault('source', {})
    source.update(title=state['title'], category_id=state['category_id'],
                  category_name=state.get('category_name') or state['category_id'],
                  attributes=state.get('attributes') or [], variations=state.get('variations') or [])
    image = state.get('main_image_url')
    if image:
        source['pictures'] = [{'source': image}] + [p for p in source.get('pictures') or [] if p.get('source') != image]
    snapshot['description'] = {'plain_text': state['description']}
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from erp.mercadolibre_collection_store import _connect_list
    connection = _connect_list()
    changes = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, category_id, source_snapshot_json FROM erp_mercadolibre_products WHERE source_type='ai_original'" + (' FOR UPDATE' if args.apply else ''))
            for row in cursor.fetchall():
                snapshot = json.loads(row['source_snapshot_json'] or '{}')
                state = snapshot.get('ai_original') or {}
                if state.get('status') == 'processing':
                    continue
                category = str(state.get('category_id') or '')
                if not re.fullmatch(r'CBT[A-Z0-9_-]{1,63}', category):
                    continue
                recovered = repaired_snapshot(snapshot)
                category_missing = not row['category_id']
                if not recovered and not category_missing:
                    continue
                changes.append({'id': row['id'], 'category_recovered': category_missing, 'generation_recovered': bool(recovered)})
                if args.apply:
                    if recovered:
                        a = recovered['ai_original']
                        cursor.execute("UPDATE erp_mercadolibre_products SET source_snapshot_json=%s, title=%s, description_text=%s, review_status='unreviewed' WHERE id=%s", (json.dumps(recovered, ensure_ascii=False), a['title'], a['description'], row['id']))
                    if category_missing:
                        cursor.execute('UPDATE erp_mercadolibre_products SET category_id=%s, category_name=%s WHERE id=%s', (category, state.get('category_name') or category, row['id']))
        if args.apply:
            connection.commit()
        else:
            connection.rollback()
        print(json.dumps({'applied': args.apply, 'changes': changes}, ensure_ascii=False))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    main()
