"""Persistent visit refresh queue, separate from the large listing table."""
from datetime import datetime, timedelta, timezone

TABLE = 'erp_mercadolibre_store_link_visit_schedule'


def utc_naive(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def interval_days(listed_at, now):
    listed = utc_naive(listed_at)
    now = utc_naive(now)
    return 1 if listed is not None and now - timedelta(days=14) < listed <= now else 7


def next_refresh(listed_at, now, *, success=True):
    now = utc_naive(now)
    return now + (timedelta(days=interval_days(listed_at, now)) if success else timedelta(hours=6))


def ensure_schedule_table(cursor):
    cursor.execute(f'''CREATE TABLE IF NOT EXISTS `{TABLE}` (
        token_id BIGINT NOT NULL, item_id VARCHAR(64) NOT NULL,
        listed_at DATETIME NULL, next_sync_at DATETIME NOT NULL,
        PRIMARY KEY (token_id,item_id),
        KEY idx_visit_schedule_due (next_sync_at),
        KEY idx_visit_schedule_recent (listed_at,next_sync_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')


def enqueue(cursor, rows):
    if not rows:
        return
    cursor.executemany(f'''INSERT INTO `{TABLE}`
        (token_id,item_id,listed_at,next_sync_at) VALUES (%s,%s,%s,UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE
        next_sync_at=IF(VALUES(listed_at)>UTC_TIMESTAMP()-INTERVAL 14 DAY
            AND VALUES(listed_at)<=UTC_TIMESTAMP()
            AND next_sync_at>UTC_TIMESTAMP()+INTERVAL 1 DAY, UTC_TIMESTAMP(),next_sync_at),
        listed_at=COALESCE(VALUES(listed_at),listed_at)''',
        [(r['token_id'],r['item_id'],r.get('listed_at')) for r in rows])
