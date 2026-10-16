"""Restore missing Zeshun store assignments and unassigned pulled product owners.

Dry run by default. --apply requires a fresh gzip JSONL audit path. Legacy browser
configuration is scoped to wuhan-zeshun; current per-site assignments take priority.
Historical names may be linked by an exact seller/site identity with one owner.
No assignment is inferred from a person's name or from the synchronization actor.
"""
import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path

SITES = ('MLM', 'MLB', 'MLC', 'MCO', 'MLA', 'MLU')
LEGACY_ORG = 'wuhan-zeshun'


def organization(value):
    return value if value not in (None, '', 'default') else LEGACY_ORG


def plan(tokens, settings, browsers, products, *, excluded_stores=()):
    excluded_stores = set(excluded_stores)
    legacy = {str(r['shop_name']).strip(): str(r.get('salesperson') or '').strip()
              for r in browsers if str(r.get('salesperson') or '').strip()}
    current = {(r['token_id'], r['site_id']): str(r.get('salesperson') or '').strip()
               for r in settings}
    owners, names = {}, defaultdict(set)
    restore = []
    for t in tokens:
        org = organization(t.get('organization_key'))
        name = str(t.get('display_name') or '').strip()
        # Only restore wholly unassigned stores; preserve mixed per-site settings.
        person = legacy.get(name) if name not in excluded_stores and org == LEGACY_ORG and not any(
            current.get((t['id'], site)) for site in SITES) else None
        for site in SITES:
            owner = current.get((t['id'], site), '') or person or ''
            owners[org, t['id'], site] = owner
            names[org, name, site].add(owner)
            if person:
                restore.append(dict(token_id=t['id'], site_id=site, salesperson=person))

    def exact(row):
        org, p = row['organization_key'], row['plugin']
        site = str(p.get('site_id') or '').strip().upper()
        if str(p.get('store_name') or '').strip() in excluded_stores:
            return None, 'excluded_store'
        token = p.get('token_id')
        if token not in (None, '', 'null'):
            try:
                return owners.get((org, int(token), site)), 'explicit_store'
            except (TypeError, ValueError):
                return None, 'invalid_store'
        name = str(p.get('store_name') or '').strip()
        choices = names.get((org, name, site))
        if choices is not None:
            return (next(iter(choices)) if len(choices) == 1 else None), 'current_name'
        return (legacy.get(name) if org == LEGACY_ORG and site in SITES else None), 'legacy_name'

    seller_owners = defaultdict(set)
    for row in products:
        person, _ = exact(row)
        seller = str(row.get('seller') or '').strip()
        if person and seller:
            site = str(row['plugin'].get('site_id') or '').strip().upper()
            seller_owners[row['organization_key'], seller, site].add(person)
    changes, counts = [], Counter()
    for row in products:
        person, reason = exact(row)
        p = row['plugin']
        # Never replace an explicit token or current store's empty assignment.
        if not person and reason == 'legacy_name' and row.get('seller'):
            key = (row['organization_key'], str(row['seller']).strip(),
                   str(p.get('site_id') or '').strip().upper())
            choices = seller_owners.get(key, set())
            if len(choices) == 1:
                person, reason = next(iter(choices)), 'seller_identity'
        if person:
            changes.append(dict(row, salesperson=person, reason=reason))
            counts[reason] += 1
        else:
            counts['unresolved'] += 1
    return restore, changes, dict(counts)


def run(connection, *, apply=False, audit_path=None, excluded_stores=()):
    with connection.cursor() as q:
        q.execute('SET SESSION max_execution_time=8000')
        q.execute('SET SESSION innodb_lock_wait_timeout=5')
        q.execute('SELECT id,organization_key,display_name FROM mercado_store_tokens')
        tokens = q.fetchall()
        q.execute('SELECT token_id,site_id,salesperson FROM mercado_store_site_settings')
        settings = q.fetchall()
        q.execute('SELECT shop_name,salesperson FROM bit_browser_configs')
        browsers = q.fetchall()
        rows = []
        for org in sorted({organization(t.get('organization_key')) for t in tokens}):
            for empty in ('IS NULL', "= ''"):
                last = 0
                while True:
                    q.execute(f'''SELECT id,organization_key,owner_name,owner_user_id,
                        JSON_EXTRACT(source_snapshot_json,'$.plugin_snapshot') plugin,
                        JSON_UNQUOTE(JSON_EXTRACT(source_snapshot_json,'$.source.seller_id')) seller
                        FROM erp_mercadolibre_products FORCE INDEX(idx_product_salesperson)
                        WHERE organization_key=%s AND owner_name {empty} AND source_type='pulled'
                        AND id>%s ORDER BY id LIMIT 1000''', (org, last))
                    batch = q.fetchall()
                    if not batch:
                        break
                    for row in batch:
                        row['plugin'] = json.loads(row['plugin'] or '{}')
                    rows.extend(batch)
                    last = batch[-1]['id']
                    connection.commit()
        restore, changes, counts = plan(tokens, settings, browsers, rows, excluded_stores=excluded_stores)
        result = dict(checked=len(rows), settings=len(restore), planned=len(changes),
                      reasons=counts, applied=apply, changed=0)
        if not apply:
            connection.rollback()
            return result
        with gzip.open(audit_path, 'wt', encoding='utf-8') as audit:
            # Store the old directory and all affected product owners before writes.
            audit.write(json.dumps(dict(kind='settings', before=settings, restore=restore), ensure_ascii=False)+'\n')
            for row in changes:
                audit.write(json.dumps(dict(kind='product', **row), ensure_ascii=False)+'\n')
            audit.flush()
            try:
                # Reject a concurrent directory change before modifying ownership.
                q.execute('SELECT token_id,site_id,salesperson FROM mercado_store_site_settings FOR UPDATE')
                if sorted(q.fetchall(), key=lambda r: (r['token_id'], r['site_id'])) != sorted(settings, key=lambda r: (r['token_id'], r['site_id'])):
                    raise RuntimeError('Store assignments changed; rerun the preview')
                for r in restore:
                    q.execute('''INSERT INTO mercado_store_site_settings
                        (token_id,site_id,salesperson,created_at,updated_at)
                        VALUES (%s,%s,%s,NOW(),NOW()) ON DUPLICATE KEY UPDATE
                        salesperson=VALUES(salesperson),updated_at=NOW()''',
                        (r['token_id'], r['site_id'], r['salesperson']))
                connection.commit()
                for offset in range(0, len(changes), 500):
                    batch = changes[offset:offset+500]
                    q.executemany('''UPDATE erp_mercadolibre_products SET owner_name=%s,owner_user_id=NULL
                        WHERE id=%s AND organization_key=%s AND source_type='pulled'
                        AND (owner_name IS NULL OR owner_name='')''',
                        [(r['salesperson'], r['id'], r['organization_key']) for r in batch])
                    result['changed'] += q.rowcount
                    connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--exclude-store', action='append', default=[])
    parser.add_argument('--audit', default='artifacts/pulled-owner-repair-before.jsonl.gz')
    args = parser.parse_args()
    if args.apply:
        if Path(args.audit).exists():
            raise SystemExit('Audit already exists; choose a new path')
        Path(args.audit).parent.mkdir(parents=True, exist_ok=True)
    import pymysql
    from bit.db_pool import default_connection_config
    c = pymysql.connect(**dict(default_connection_config(), read_timeout=15))
    try:
        print(json.dumps(run(c, apply=args.apply, audit_path=args.audit, excluded_stores=args.exclude_store), ensure_ascii=False))
    finally:
        c.close()


if __name__ == '__main__':
    main()
