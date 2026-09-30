"""Durable upload history and daily Mercado Libre clip moderation checks."""
import json
import logging
import threading
from erp.mercadolibre_store_link_marker_store import _database, _now

LABELS = {'UNDER_REVIEW': '已提交待审核', 'PUBLISHED': '视频审核通过', 'REJECTED': '视频审核不通过', 'TRANSCODING_REJECTED': '视频审核不通过（转码失败）', 'PAUSED': '已暂停'}


def database():
    return _database()


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS video_upload_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_id INTEGER NOT NULL,
        site_id TEXT NOT NULL, item_id TEXT NOT NULL, clip_uuid TEXT NOT NULL,
        uploaded_at TEXT NOT NULL, checked_at TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'UNDER_REVIEW', error TEXT NOT NULL DEFAULT '',
        details TEXT NOT NULL, UNIQUE(token_id, site_id, item_id, clip_uuid))''')


def record_upload(row, result, cbt_item_id, logistic_type, uploaded_at=None):
    details = {key: row.get(key) for key in ('id', 'store_name', 'seller_id', 'permalink', 'title', 'token_id', 'site_id')}
    details.update(cbt_item_id=cbt_item_id, logistic_type=logistic_type)
    with database() as db:
        schema(db)
        db.execute('''INSERT OR IGNORE INTO video_upload_history
            (token_id, site_id, item_id, clip_uuid, uploaded_at, details)
            VALUES (?, ?, ?, ?, ?, ?)''', (row['token_id'], row['site_id'], row['item_id'], result['clip_uuid'], uploaded_at or _now(), json.dumps(details)))
    return details


def records(clips=None):
    with database() as db:
        schema(db)
        if clips is not None:
            clips = list(clips)
            if not clips:
                return []
            rows = db.execute('SELECT * FROM video_upload_history WHERE clip_uuid IN (' + ','.join('?' for _ in clips) + ') ORDER BY id DESC', clips).fetchall()
        else:
            rows = db.execute('SELECT * FROM video_upload_history ORDER BY id DESC').fetchall()
    return [{**json.loads(r['details']), **dict(r), 'status_label': LABELS.get(r['status'], r['status'])} for r in rows]


def backfill_history():
    """Recover earlier uploads from durable link markers without re-uploading."""
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token
    known = {(r['token_id'], r['site_id'], r['item_id'], r['clip_uuid']) for r in records()}
    with database() as db:
        legacy = [dict(r) for r in db.execute("SELECT * FROM store_link_markers WHERE video_uploaded=1 AND video_clip_uuid<>''")]
    for row in legacy:
        if (row['token_id'], row['site_id'], row['item_id'], row['video_clip_uuid']) in known:
            continue
        try:
            token = dict(bit_mysql.get_mercado_store_token(row['token_id']) or {})
            client, _ = _client_and_token(token)
            item = client.get_marketplace_item(row['item_id'])
            cbt = item.get('cbt_item_id')
            payload = client.request('GET', f'/marketplace/items/{cbt}/clips')
            matches = [m for clip in payload.get('clips', []) if clip.get('clip_uuid') == row['video_clip_uuid']
                       for m in clip.get('metadata', []) if m.get('item_id') == row['item_id'] and m.get('site_id') == row['site_id']]
            if len(matches) != 1 or not cbt:
                continue
            record_upload({**row, 'permalink': item.get('permalink'), 'store_name': token.get('display_name') or token.get('nickname')},
                          {'clip_uuid': row['video_clip_uuid']}, cbt, matches[0]['logistic_type'], row['updated_at'])
        except Exception:
            logging.exception('历史视频审核记录补录失败：%s', row['item_id'])


def sync_pending():
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token
    for row in records():
        if row['status'] != 'UNDER_REVIEW' or row['checked_at'][:10] == _now()[:10]:
            continue
        status, error = row['status'], ''
        try:
            client, _ = _client_and_token(dict(bit_mysql.get_mercado_store_token(row['token_id']) or {}))
            cbt = row['cbt_item_id']
            payload = client.request('GET', f'/marketplace/items/{cbt}/clips')
            matches = [m for clip in payload.get('clips', []) if clip.get('clip_uuid') == row['clip_uuid']
                       for m in clip.get('metadata', []) if m.get('item_id') == row['item_id']
                       and m.get('site_id') == row['site_id'] and m.get('logistic_type') == row['logistic_type']]
            if len(matches) != 1:
                raise ValueError('平台尚未返回该链接的视频审核记录')
            remote_status = matches[0].get('moderation_status')
            if remote_status not in LABELS:
                raise ValueError(f'平台返回未知审核状态：{remote_status}')
            status = remote_status
            error = json.dumps(matches[0].get('moderation_reasons') or {}, ensure_ascii=False) if matches[0].get('moderation_reasons') else ''
        except Exception as exc:
            error = str(exc)[:500]
        with database() as db:
            db.execute('UPDATE video_upload_history SET status=?, checked_at=?, error=? WHERE id=?', (status, _now(), error, row['id']))


def start_scheduler():
    def run():
        last_backfill = ""
        while True:
            try:
                if last_backfill != _now()[:10]:
                    backfill_history()
                    last_backfill = _now()[:10]
                sync_pending()
            except Exception:
                logging.exception('视频审核状态同步失败')
            threading.Event().wait(3600)
    thread = threading.Thread(target=run, name='video-review-daily-sync', daemon=True)
    thread.start()
    return thread
