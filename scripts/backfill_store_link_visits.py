"""Refresh visits for all current store links; checkpoint each committed batch."""
import argparse
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import time
from bit.bit_runtime_lock import InterProcessLock
import pymysql
from bit.db_pool import default_connection_config
from bit.store_link_visits import fetch_total_visits
from mercado_api.client import MercadoLibreClient
from erp.store_link_visits_store import ensure_visits_table, save_visits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=None)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--requests-per-second', type=float, default=4)
    parser.add_argument('--state', default='runtime_logs/store-link-visits-backfill.json')
    args = parser.parse_args()
    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else dict(last_id=0, checked=0, updated=0, failed=0, started_at=datetime.now().isoformat())
    config = default_connection_config()
    if args.host: config['host'] = args.host
    config.update(read_timeout=120,write_timeout=120)
    db = pymysql.connect(**config)
    try:
        with db.cursor() as q:
            q.execute('SET SESSION lock_wait_timeout=15')
            ensure_visits_table(q)
            if 'max_id' not in state:
                q.execute('SELECT MAX(id) AS max_id FROM erp_mercadolibre_store_links')
                state['max_id']=q.fetchone()['max_id'] or 0
                q.execute('SELECT COUNT(*) AS total FROM erp_mercadolibre_store_links WHERE is_current=1')
                state['total']=q.fetchone()['total']
        db.commit()
        state['status']='running'
        def save():
            state['heartbeat_at']=datetime.now().isoformat()
            temp=state_path.with_suffix('.tmp'); temp.write_text(json.dumps(state,ensure_ascii=False,indent=2)); temp.replace(state_path)
            print(json.dumps(state,ensure_ascii=False),flush=True)
        save()
        local = threading.local()
        pacing_lock=threading.Lock()
        next_request=[0.0]
        def paced_fetch(client,item_id):
            with pacing_lock:
                delay=max(0,next_request[0]-time.monotonic())
                next_request[0]=max(next_request[0],time.monotonic())+1/max(0.1,args.requests_per_second)
            if delay: time.sleep(delay)
            return fetch_total_visits(client,item_id)
        with ThreadPoolExecutor(max_workers=max(1,min(args.workers,8))) as pool:
            while True:
                with db.cursor() as q:
                    q.execute('SELECT id,token_id,item_id FROM erp_mercadolibre_store_links WHERE is_current=1 AND id>%s AND id<=%s ORDER BY id LIMIT 100',(state['last_id'],state['max_id']))
                    rows=q.fetchall()
                    if not rows: break
                    ids=sorted({r['token_id'] for r in rows})
                    q.execute('SELECT id,access_token,enabled FROM mercado_store_tokens WHERE id IN ('+','.join(['%s']*len(ids))+')',ids)
                    tokens={r['id']:r for r in q.fetchall()}
                db.commit()
                def fetch(row):
                    token=tokens.get(row['token_id']) or {}
                    if not token.get('access_token') or not token.get('enabled'):
                        return row,dict(total_visits=None,visits_updated_at=None,visits_error='店铺授权缺失或已关闭')
                    cached=getattr(local,'client',None)
                    if cached is None or cached.access_token != token['access_token']:
                        if cached is not None: cached.session.close()
                        local.client=MercadoLibreClient(token['access_token'],timeout=20)
                    value=paced_fetch(local.client,row['item_id'])
                    for attempt in range(2):
                        if value['total_visits'] is not None or '不支持' in value['visits_error']: break
                        time.sleep(15*(attempt+1))
                        value=paced_fetch(local.client,row['item_id'])
                    return row,value
                results=list(pool.map(fetch,rows))
                with db.cursor() as q:
                    save_visits(q,[{**r,**v} for r,v in results])
                db.commit()
                successes=sum(v['total_visits'] is not None for r,v in results)
                state['last_id']=rows[-1]['id'];state['checked']+=len(rows);state['updated']+=successes;state['failed']+=len(rows)-successes
                save()
        state['status']='completed';save()
    finally: db.close()


if __name__=='__main__':
    lock=InterProcessLock('store_link_visits_backfill',owner='visits-backfill')
    if not lock.acquire():
        raise SystemExit('访问量回填任务已运行')
    try:
        for attempt in range(10):
            try:
                main()
                break
            except pymysql.Error as exc:
                print(json.dumps({'status':'retrying_database','error':type(exc).__name__,'attempt':attempt+1}),flush=True)
                if attempt==9: raise
                time.sleep(30)
    finally: lock.release()
