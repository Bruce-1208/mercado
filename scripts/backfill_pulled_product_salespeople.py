"""Reconcile pulled product owners from store/site assignments in bounded batches.

No list request runs this job. --apply saves a gzip JSONL audit before each commit.
Ambiguous ownership across authorized stores is reported and left untouched.
"""
import argparse
import gzip
import json
import time
from pathlib import Path


def run(connection, *, apply=False, audit_path=None, batch_size=5000):
    stats = {'links': 0, 'matched': 0, 'ambiguous': 0, 'changed': 0}
    started = time.monotonic()
    audit = gzip.open(audit_path, 'wt', encoding='utf-8') if apply else None
    try:
        with connection.cursor() as q:
            q.execute('SET SESSION innodb_lock_wait_timeout=5')
            q.execute('SET SESSION max_execution_time=15000')
            q.execute('CREATE TEMPORARY TABLE pulled_owner_candidates (id BIGINT PRIMARY KEY, salesperson VARCHAR(128) NOT NULL, ambiguous TINYINT NOT NULL DEFAULT 0) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci')
            q.execute('SELECT COALESCE(MAX(id), 0) AS maximum FROM erp_mercadolibre_store_links')
            maximum = q.fetchone()['maximum']
            last = 0
            while last < maximum:
                q.execute('SELECT id FROM erp_mercadolibre_store_links WHERE id > %s AND id <= %s ORDER BY id LIMIT %s', (last, maximum, batch_size))
                ids = q.fetchall()
                if not ids:
                    break
                upper = ids[-1]['id']
                q.execute('''SELECT p.id, TRIM(COALESCE(s.salesperson, '')) AS salesperson
                    FROM erp_mercadolibre_store_links l
                    JOIN mercado_store_tokens t ON t.id = l.token_id
                    JOIN erp_mercadolibre_products p ON p.source_item_id = l.item_id
                      AND p.organization_key = CASE WHEN COALESCE(t.organization_key, '') IN ('', 'default') THEN 'wuhan-zeshun' ELSE t.organization_key END
                    LEFT JOIN mercado_store_site_settings s ON s.token_id = l.token_id AND s.site_id = l.site_id
                    WHERE l.id > %s AND l.id <= %s AND p.source_type = 'pulled' ''', (last, upper))
                rows = q.fetchall()
                if rows:
                    q.executemany('''INSERT INTO pulled_owner_candidates (id, salesperson) VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE ambiguous = IF(salesperson <> VALUES(salesperson), 1, ambiguous)''',
                        [(r['id'], r['salesperson']) for r in rows])
                connection.commit()
                stats['links'] += len(ids)
                last = upper
                if stats['links'] % 100000 == 0:
                    print(json.dumps({'phase': 'map', **stats}), flush=True)
            q.execute('SELECT COUNT(*) AS matched, COALESCE(SUM(ambiguous), 0) AS ambiguous FROM pulled_owner_candidates')
            counts = q.fetchone()
            stats.update({k: int(v) for k, v in counts.items()})
            print(json.dumps({'phase': 'mapped', **stats}), flush=True)
            last = 0
            while True:
                q.execute('SELECT id FROM pulled_owner_candidates WHERE id > %s ORDER BY id LIMIT %s', (last, batch_size))
                ids = q.fetchall()
                if not ids:
                    break
                upper = ids[-1]['id']
                q.execute('''SELECT p.id, p.owner_name, p.owner_user_id, p.organization_key, c.salesperson
                    FROM pulled_owner_candidates c JOIN erp_mercadolibre_products p ON p.id = c.id
                    WHERE c.id > %s AND c.id <= %s AND c.ambiguous = 0 AND p.source_type = 'pulled'
                    AND (BINARY COALESCE(p.owner_name, '') <> BINARY c.salesperson OR p.owner_user_id IS NOT NULL) FOR UPDATE''', (last, upper))
                changes = q.fetchall()
                if apply and changes:
                    for row in changes:
                        audit.write(json.dumps(row, ensure_ascii=False) + '\n')
                    audit.flush()
                    q.execute('''UPDATE erp_mercadolibre_products p JOIN pulled_owner_candidates c ON c.id = p.id
                        SET p.owner_name = c.salesperson, p.owner_user_id = NULL
                        WHERE c.id > %s AND c.id <= %s AND c.ambiguous = 0 AND p.source_type = 'pulled'
                        AND (BINARY COALESCE(p.owner_name, '') <> BINARY c.salesperson OR p.owner_user_id IS NOT NULL)''', (last, upper))
                    assert q.rowcount == len(changes)
                stats['changed'] += len(changes)
                connection.commit()
                last = upper
                if stats['changed'] and stats['changed'] % 100000 < batch_size:
                    print(json.dumps({'phase': 'update' if apply else 'preview', **stats}), flush=True)
        return dict(stats, seconds=round(time.monotonic() - started, 2), applied=apply)
    except BaseException:
        connection.rollback()
        raise
    finally:
        if audit:
            audit.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--audit', default='artifacts/pulled-product-salespeople-before.jsonl.gz')
    args = parser.parse_args()
    if args.apply:
        Path(args.audit).parent.mkdir(parents=True, exist_ok=True)
        if Path(args.audit).exists():
            raise SystemExit('Audit file already exists; use a new --audit path')
    import pymysql
    from bit.db_pool import default_connection_config
    connection = pymysql.connect(**dict(default_connection_config(), read_timeout=30))
    try:
        print(json.dumps(run(connection, apply=args.apply, audit_path=args.audit)), flush=True)
    finally:
        connection.close()


if __name__ == '__main__':
    main()
