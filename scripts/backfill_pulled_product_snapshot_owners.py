"""Fill remaining unassigned pulled products from their saved store identity.

Uses current store/site settings, with exact unambiguous store-name fallback only
for historical snapshots that predate token_id. Reads only unassigned index ranges.
"""
import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path


def resolve(plugin, organization, by_id, by_name):
    site = str(plugin.get('site_id') or '').strip().upper()
    token = plugin.get('token_id')
    if token not in (None, '', 'null'):
        try:
            person = by_id.get((organization, int(token), site))
        except (ValueError, TypeError):
            return None, 'invalid_store'
    else:
        choices = by_name.get((organization, str(plugin.get('store_name') or '').strip(), site), set())
        if len(choices) != 1:
            return None, 'unmatched_store' if not choices else 'ambiguous_store'
        person = next(iter(choices))
    return (person, 'matched') if person else (None, 'store_unassigned')


def run(connection, *, apply=False, audit_path=None):
    counts = Counter()
    by_id, by_name = {}, defaultdict(set)
    audit = gzip.open(audit_path, 'wt', encoding='utf-8') if apply else None
    try:
        with connection.cursor() as q:
            q.execute('SET SESSION max_execution_time=8000')
            q.execute('SET SESSION innodb_lock_wait_timeout=5')
            q.execute('''SELECT t.id,t.organization_key,t.display_name,s.site_id,s.salesperson
                FROM mercado_store_tokens t LEFT JOIN mercado_store_site_settings s ON s.token_id=t.id''')
            organizations=set()
            for r in q.fetchall():
                org=r['organization_key'] if r['organization_key'] not in (None,'','default') else 'wuhan-zeshun'
                organizations.add(org)
                site=str(r['site_id'] or '').strip().upper()
                person=str(r['salesperson'] or '').strip()
                by_id[org,r['id'],site]=person
                by_name[org,str(r['display_name'] or '').strip(),site].add(person)
            for org in sorted(organizations):
                for empty in ['IS NULL', "= ''"]:
                    last=0
                    while True:
                        q.execute(f'''SELECT id,owner_name,owner_user_id,
                            JSON_EXTRACT(source_snapshot_json,'$.plugin_snapshot') AS plugin
                            FROM erp_mercadolibre_products FORCE INDEX(idx_product_salesperson)
                            WHERE organization_key=%s AND owner_name {empty} AND id>%s AND source_type='pulled'
                            ORDER BY id LIMIT 1000''',(org,last))
                        rows=q.fetchall()
                        if not rows:break
                        changes=[]
                        for row in rows:
                            counts['checked']+=1
                            plugin=json.loads(row.pop('plugin') or '{}')
                            person,reason=resolve(plugin,org,by_id,by_name)
                            counts[reason]+=1
                            if person:
                                changes.append((person,row['id'],org))
                                if audit:audit.write(json.dumps(dict(row,organization_key=org,salesperson=person),ensure_ascii=False)+'\n')
                        if changes and apply:
                            audit.flush()
                            q.executemany('''UPDATE erp_mercadolibre_products SET owner_name=%s,owner_user_id=NULL
                                WHERE id=%s AND organization_key=%s AND source_type='pulled'
                                AND (owner_name IS NULL OR owner_name='')''',changes)
                            counts['changed']+=q.rowcount
                        connection.commit()
                        last=rows[-1]['id']
        return dict(counts,applied=apply)
    except BaseException:
        connection.rollback()
        raise
    finally:
        if audit:audit.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--audit',default='artifacts/pulled-snapshot-owners-before.jsonl.gz')
    args=parser.parse_args()
    if args.apply:
        Path(args.audit).parent.mkdir(parents=True,exist_ok=True)
        if Path(args.audit).exists():raise SystemExit('Audit file already exists')
    import pymysql
    from bit.db_pool import default_connection_config
    c=pymysql.connect(**dict(default_connection_config(),read_timeout=15))
    try:print(json.dumps(run(c,apply=args.apply,audit_path=args.audit)),flush=True)
    finally:c.close()


if __name__=='__main__':main()
