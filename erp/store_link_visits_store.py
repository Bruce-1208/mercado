"""Small companion table avoids rebuilding the large listing snapshot table."""
TABLE = 'erp_mercadolibre_store_link_visits'


def ensure_visits_table(cursor):
    cursor.execute(f'''CREATE TABLE IF NOT EXISTS `{TABLE}` (
        token_id BIGINT NOT NULL, item_id VARCHAR(64) NOT NULL,
        total_visits BIGINT NULL, visits_updated_at DATETIME NULL,
        visits_error VARCHAR(128) NULL, checked_at DATETIME NOT NULL,
        PRIMARY KEY (token_id,item_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')


def save_visits(cursor, rows):
    if not rows:
        return
    cursor.executemany(f'''INSERT INTO `{TABLE}`
        (token_id,item_id,total_visits,visits_updated_at,visits_error,checked_at)
        VALUES (%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE
        total_visits=COALESCE(VALUES(total_visits),total_visits),
        visits_updated_at=COALESCE(VALUES(visits_updated_at),visits_updated_at),
        visits_error=VALUES(visits_error), checked_at=NOW()''',
        [(r['token_id'],r['item_id'],r.get('total_visits'),r.get('visits_updated_at'),r.get('visits_error')) for r in rows])
