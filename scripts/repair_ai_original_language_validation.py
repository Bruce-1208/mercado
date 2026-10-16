"""Recheck legacy title-language detection errors without regenerating content."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ERROR = '标题语言无法核实，请检查语言识别服务后重试'


def repaired_snapshot(snapshot):
    draft = snapshot.get('ai_original') or {}
    if draft.get('status') == 'processing' or not any(
        ERROR in str(error) for error in (draft.get('validation') or {}).get('errors', [])
    ):
        return None
    from erp.ai_original_platforms import validate_platform_listing
    validation = validate_platform_listing(draft, 'mercadolibre')
    if validation == draft.get('validation'):
        return None
    result = deepcopy(snapshot)
    result['ai_original']['validation'] = validation
    mercado = (result.get('marketplace_listings') or {}).get('mercadolibre')
    if isinstance(mercado, dict):
        mercado['validation'] = deepcopy(validation)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from erp.mercadolibre_collection_store import _connect
    connection = _connect()
    changes = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, source_snapshot_json FROM erp_mercadolibre_products "
                "WHERE source_type='ai_original' AND organization_key='wuhan-zeshun' "
                "AND JSON_UNQUOTE(JSON_EXTRACT(source_snapshot_json, '$.ai_original.validation.errors')) LIKE %s",
                ('%' + ERROR + '%',),
            )
            for row in cursor.fetchall():
                previous = json.loads(row['source_snapshot_json'] or '{}')
                repaired = repaired_snapshot(previous)
                if repaired is None:
                    continue
                validation = repaired['ai_original']['validation']
                changed = None
                if args.apply:
                    # Compare the read snapshot so concurrent edits are never lost.
                    cursor.execute(
                        "UPDATE erp_mercadolibre_products SET source_snapshot_json=%s "
                        "WHERE id=%s AND source_snapshot_json=%s",
                        (json.dumps(repaired, ensure_ascii=False), row['id'], row['source_snapshot_json']),
                    )
                    changed = cursor.rowcount == 1
                changes.append({'id': row['id'], 'validation': validation, 'applied': changed})
        if args.apply:
            connection.commit()
        else:
            connection.rollback()
        print(json.dumps({'apply': args.apply, 'changes': changes}, ensure_ascii=False))
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    main()
