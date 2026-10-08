"""Repair the explicitly requested Zhou Le store assignment; refuse conflicting data."""
import json
from pathlib import Path
from bit import bit_mysql as db

out = Path(__file__).resolve().parent
connection = db.pymysql.connect(**db.config)
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT id,username,display_name,organization_key,role_key FROM workbench_users WHERE id=19')
        user = cursor.fetchone()
        cursor.execute('SELECT id,display_name,organization_key,enabled FROM mercado_store_tokens WHERE id=154 FOR UPDATE')
        store = cursor.fetchone()
        assert user['display_name'] == store['display_name'] == '周乐'
        assert user['organization_key'] == store['organization_key'] == 'wuhan-zeshun'
        cursor.execute('SELECT * FROM mercado_store_site_settings WHERE token_id=154 FOR UPDATE')
        before = cursor.fetchall()
        assert all(row.get('salesperson') in (None, '', '周乐') for row in before), 'Conflicting owner; abort'
        backup = out / 'before.json'
        if not backup.exists():
            backup.write_text(json.dumps({'user': user, 'store': store, 'settings': before}, ensure_ascii=False, indent=2, default=str))
        for site in db.MERCADO_CONFIGURABLE_SITES:
            cursor.execute('''INSERT INTO mercado_store_site_settings
                (token_id,site_id,salesperson,appeal_enabled,reputation_update_enabled,visit_stats_enabled,created_at,updated_at)
                VALUES (154,%s,'周乐',0,0,0,NOW(),NOW())
                ON DUPLICATE KEY UPDATE salesperson=VALUES(salesperson),updated_at=NOW()''', (site,))
    connection.commit()
    print('Repaired store 154 salesperson assignment for six sites.')
except Exception:
    connection.rollback()
    raise
finally:
    connection.close()
