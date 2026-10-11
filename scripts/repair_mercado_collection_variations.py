"""Repair proven native spec-table/variant confusion; dry-run by default."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erp.mercadolibre_collection_store import _connect_list
from erp.mercadolibre_page_facts import repair_source_facts


def repair(*, report_path, apply=False):
    connection = _connect_list()
    plan = []
    try:
        with connection.cursor() as cursor:
            targets = [
                ('erp_mercadolibre_collection_items', 'source_json', ''),
                ('erp_mercadolibre_products', 'source_snapshot_json', "WHERE source_type = 'collected'"),
                ('erp_mercadolibre_source_items', 'source_json', ''),
            ]
            for table, column, where in targets:
                cursor.execute('SHOW TABLES LIKE %s', (table,))
                if not cursor.fetchone():
                    continue
                extra = ', attributes_json, variations_json' if table.endswith('source_items') else ''
                if table.endswith('collection_items'):
                    extra = ', page_snapshot_json'
                cursor.execute(f'SELECT id, `{column}`{extra} FROM `{table}` {where}')
                for row in cursor.fetchall():
                    try:
                        payload = json.loads(row[column] or '{}')
                    except (ValueError, TypeError):
                        continue
                    source = payload.get('source', {}) if column == 'source_snapshot_json' else payload
                    if not isinstance(source, dict):
                        continue
                    repaired = repair_source_facts(source)
                    if repaired == source:
                        continue
                    if column == 'source_snapshot_json':
                        payload['source'] = repaired
                        page = payload.get('page_snapshot')
                        if isinstance(page, dict):
                            page['variation_count'] = len(repaired.get('variations') or [])
                    else:
                        payload = repaired
                    after = {column: json.dumps(payload, ensure_ascii=False)}
                    if table.endswith('source_items'):
                        after.update(attributes_json=json.dumps(repaired['attributes'], ensure_ascii=False),
                                     variations_json=json.dumps(repaired['variations'], ensure_ascii=False))
                    if 'page_snapshot_json' in row:
                        try:
                            page = json.loads(row['page_snapshot_json'] or '{}')
                            page['variation_count'] = len(repaired['variations'])
                            after['page_snapshot_json'] = json.dumps(page, ensure_ascii=False)
                        except (ValueError, TypeError):
                            pass
                    plan.append({'table': table, 'id': row['id'], 'before': {key: row[key] for key in after}, 'after': after})
        connection.rollback()
        report = {'apply': apply, 'planned': len(plan), 'saved': 0, 'items': plan}
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write the complete before-image before any mutation, and do not overwrite a backup.
        with path.open('x') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        if apply:
            with connection.cursor() as cursor:
                for item in plan:
                    fields = list(item['after'])
                    setters = ', '.join(f'`{field}` = %s' for field in fields)
                    guards = ' AND '.join(f'`{field}` <=> %s' for field in fields)
                    cursor.execute(f"UPDATE `{item['table']}` SET {setters} WHERE id = %s AND {guards}",
                                   tuple(item['after'][field] for field in fields) + (item['id'],) + tuple(item['before'][field] for field in fields))
                    if cursor.rowcount != 1:
                        raise RuntimeError('Snapshot changed concurrently; transaction rolled back')
                connection.commit()
            report['saved'] = len(plan)
            path.with_suffix('.result.json').write_text(json.dumps({'saved': len(plan)}, ensure_ascii=False))
        return {key: value for key, value in report.items() if key != 'items'}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    print(json.dumps(repair(report_path=args.report, apply=args.apply)))
