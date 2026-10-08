"""Independent, paced daily/weekly visits worker with a durable initial sweep."""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from bit.bit_runtime_lock import InterProcessLock
from bit.db_pool import default_connection_config
from bit.store_link_visits import fetch_total_visits
from erp.store_link_visits_store import ensure_visits_table, save_visits
from erp.store_link_visit_schedule import TABLE, ensure_schedule_table, next_refresh
from mercado_api.client import MercadoLibreClient
import pymysql

_guard = threading.Lock()
_thread = None
STATE = Path(__file__).resolve().parents[1] / 'runtime_logs/store-link-visits-schedule.json'


def connect():
    config = default_connection_config()
    config.update(read_timeout=60, write_timeout=30)
    return pymysql.connect(**config)


def seed_page(after_id):
    """Bound the large-table scan to one primary-key page; never request visits here."""
    db = connect()
    try:
        with db.cursor() as q:
            q.execute('SELECT id,token_id,item_id,listed_at,is_current FROM erp_mercadolibre_store_links WHERE id>%s ORDER BY id LIMIT 1000', (after_id,))
            rows = q.fetchall()
            current = [r for r in rows if r['is_current']]
            if current:
                # Visit timestamps use the existing server-local convention; queue dates are UTC.
                # Existing successful data keeps its daily/weekly due date.
                q.executemany(f'''INSERT INTO `{TABLE}` (token_id,item_id,listed_at,next_sync_at)
                    SELECT %s,%s,%s,COALESCE(DATE_ADD(
                        TIMESTAMPADD(SECOND,TIMESTAMPDIFF(SECOND,NOW(),UTC_TIMESTAMP()),v.visits_updated_at),
                        INTERVAL IF(%s>UTC_TIMESTAMP()-INTERVAL 14 DAY AND %s<=UTC_TIMESTAMP(),1,7) DAY),UTC_TIMESTAMP())
                    FROM (SELECT 1) AS dummy LEFT JOIN erp_mercadolibre_store_link_visits v
                        ON v.token_id=%s AND v.item_id=%s
                    ON DUPLICATE KEY UPDATE listed_at=COALESCE(VALUES(listed_at),listed_at)''',
                    [(r['token_id'],r['item_id'],r['listed_at'],r['listed_at'],r['listed_at'],r['token_id'],r['item_id']) for r in current])
        db.commit()
        return rows[-1]['id'] if rows else after_id, not rows
    finally:
        db.close()


def due_rows():
    db = connect()
    try:
        with db.cursor() as q:
            # Two small indexed queries prioritize new listings without sorting the entire queue.
            columns = 's.token_id,s.item_id,s.listed_at,t.access_token,l.is_current,t.enabled'
            joins = '''LEFT JOIN mercado_store_tokens t ON t.id=s.token_id
                LEFT JOIN erp_mercadolibre_store_links l ON l.token_id=s.token_id AND l.item_id=s.item_id'''
            q.execute(f'''SELECT {columns} FROM `{TABLE}` s {joins}
                WHERE s.next_sync_at<=UTC_TIMESTAMP() AND s.listed_at>UTC_TIMESTAMP()-INTERVAL 14 DAY
                AND s.listed_at<=UTC_TIMESTAMP() ORDER BY s.listed_at LIMIT 20''')
            rows = list(q.fetchall())
            if len(rows)<20:
                q.execute(f'''SELECT {columns} FROM `{TABLE}` s {joins}
                    WHERE s.next_sync_at<=UTC_TIMESTAMP()
                    AND (s.listed_at IS NULL OR s.listed_at<=UTC_TIMESTAMP()-INTERVAL 14 DAY OR s.listed_at>UTC_TIMESTAMP())
                    ORDER BY s.next_sync_at LIMIT %s''',(20-len(rows),))
                rows.extend(q.fetchall())
            return rows
    finally:
        db.close()


def run_batch(rows):
    updates = []
    schedules = []
    succeeded = 0
    for row in rows:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if not row.get('is_current') or not row.get('enabled') or not row.get('access_token'):
            # Recheck eligibility weekly; a normal listing sync can enqueue a reactivated link.
            schedules.append((next_refresh(None,now),row['token_id'],row['item_id']))
            continue
        client = MercadoLibreClient(row['access_token'], timeout=20)
        try:
            value = fetch_total_visits(client,row['item_id'])
        finally:
            client.session.close()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ok = value['total_visits'] is not None
        updates.append({**row, **value})
        schedules.append((next_refresh(row['listed_at'],now,success=ok),row['token_id'],row['item_id']))
        succeeded += int(ok)
        time.sleep(0.5)  # At most two calls/second, independent of listing synchronization.
    db = connect()
    try:
        with db.cursor() as q:
            save_visits(q, updates)
            if schedules:
                q.executemany(f'UPDATE `{TABLE}` SET next_sync_at=%s WHERE token_id=%s AND item_id=%s', schedules)
        db.commit()
    finally:
        db.close()
    return {'checked':len(updates),'updated':succeeded,'failed':len(updates)-succeeded}


def _save(state):
    state['heartbeat_at']=datetime.now(timezone.utc).isoformat()
    STATE.parent.mkdir(parents=True,exist_ok=True)
    temp=STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    temp.replace(STATE)


def _run():
    # Share the lock with the old one-time backfill: only one visit worker may run.
    lease=InterProcessLock('store_link_visits_backfill',owner='daily-weekly-visits')
    if not lease.acquire():
        return
    try:
        db=connect()
        try:
            with db.cursor() as q:
                ensure_visits_table(q)
                ensure_schedule_table(q)
            db.commit()
        finally:
            db.close()
        state=json.loads(STATE.read_text()) if STATE.exists() else {'seed_after_id':0,'seed_complete':False,'checked':0,'updated':0,'failed':0}
        state['status']='running'
        _save(state)
        last_seed=0.0
        while True:
            try:
                # Seed quickly before spending the API quota; this resumes after restarts.
                if not state['seed_complete']:
                    for _ in range(20):
                        state['seed_after_id'],state['seed_complete']=seed_page(state['seed_after_id'])
                        _save(state)
                        if state['seed_complete']: break
                elif time.monotonic()-last_seed>=3600:
                    state['seed_after_id'],_=seed_page(state['seed_after_id'])
                    last_seed=time.monotonic()
                rows=due_rows()
                result=run_batch(rows)
                for key,value in result.items(): state[key]+=value
                state.pop('error',None)
                _save(state)
                if not rows: time.sleep(1 if not state['seed_complete'] else 60)
            except Exception as exc:
                logging.exception('独立流量调度暂时失败，将自动重试')
                state['error']=type(exc).__name__
                _save(state)
                time.sleep(30)
    finally:
        lease.release()


def _supervise():
    while True:
        try:
            _run()
        except Exception:
            logging.exception('启动独立流量调度失败，将自动重试')
        time.sleep(60)


def start_scheduler():
    global _thread
    with _guard:
        if _thread and _thread.is_alive(): return False
        _thread=threading.Thread(target=_supervise,name='store-link-visit-scheduler',daemon=True)
        _thread.start()
        return True
