"""Upload attempts and a complete, permission-scoped video upload journal."""
import json
import logging
import uuid

from bit import video_reviews


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS video_upload_attempts (
        id TEXT PRIMARY KEY, uploaded_at TEXT NOT NULL, details TEXT NOT NULL)''')


def snapshot(row, token=None, item=None):
    token, item = token or {}, item or {}
    setting = next((s for s in token.get('site_settings') or []
                    if s.get('site_id') == row.get('site_id')), {})
    pictures = item.get('pictures') or []
    picture = pictures[0] if pictures and isinstance(pictures[0], dict) else {}
    return {
        **{k: row.get(k) or '' for k in (
            'token_id', 'site_id', 'item_id', 'store_name', 'title', 'permalink', 'thumbnail_url',
        )},
        'link_id': row.get('link_id') or row.get('id'),
        'store_name': row.get('store_name') or token.get('display_name') or token.get('nickname') or '',
        'group_name': row.get('group_name') or setting.get('group_name') or '',
        'title': item.get('title') or row.get('title') or '',
        'permalink': item.get('permalink') or row.get('permalink') or '',
        'thumbnail_url': picture.get('secure_url') or picture.get('url') or
                         item.get('secure_thumbnail') or item.get('thumbnail') or row.get('thumbnail_url') or '',
    }


def save_attempt(attempt):
    with video_reviews.database() as db:
        _schema(db)
        db.execute('INSERT OR REPLACE INTO video_upload_attempts VALUES (?, ?, ?)',
                   (attempt['attempt_id'], attempt['uploaded_at'], json.dumps(attempt, ensure_ascii=False)))


def begin_attempt(details):
    attempt = {**details, 'attempt_id': details.get('attempt_id') or uuid.uuid4().hex,
               'uploaded_at': video_reviews._now(), 'status': 'uploading'}
    save_attempt(attempt)
    return attempt


def _identity(row):
    return tuple(str(row.get(k) or '') for k in ('token_id', 'site_id', 'item_id', 'clip_uuid'))


def _historical_rows():
    # Manifests recover uploader identities and attempts written before the journal.
    from bit.bit_ai_video import storage_root
    rows = []
    for path in storage_root().glob('*/job.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        for published in job.get('published') or []:
            rows.append({**published, 'job_id': job.get('id'), 'video_name': job.get('name'),
                         'uploaded_at': published.get('published_at'),
                         'status': published.get('status') or 'UNDER_REVIEW'})
        for index, attempt in enumerate(job.get('publish_attempts') or []):
            if attempt.get('status') == 'accepted':
                continue
            target = job.get('target') or {}
            details = target if str(target.get('link_id')) == str(attempt.get('link_id')) else {}
            rows.append({**details, **attempt, 'job_id': job.get('id'), 'video_name': job.get('name'),
                         'attempt_id': attempt.get('attempt_id') or f"legacy-{job.get('id')}-{index}",
                         'uploaded_at': attempt.get('started_at')})
    return rows


def _enrich(rows):
    """Fill old snapshots from local listing metadata, without contacting Mercado."""
    from bit import bit_mysql
    from erp.mercadolibre_store_link_store import get_store_links_by_ids
    ids = {int(r['link_id']) for r in rows if str(r.get('link_id') or '').isdigit()
           and any(not r.get(k) for k in ('token_id', 'title', 'thumbnail_url'))}
    links = {}
    # A deleted link must not prevent other historical records from displaying.
    for link_id in ids:
        try:
            links[link_id] = get_store_links_by_ids([link_id])[0]
        except Exception:
            logging.debug('视频上传日志链接已不可用：%s', link_id, exc_info=True)
    tokens = {}
    for row in rows:
        link = links.get(int(row.get('link_id') or 0), {})
        # Listing row IDs can be replaced during sync; never attach another product.
        if row.get('item_id') and (link.get('item_id') != row['item_id'] or
                                  (row.get('token_id') and str(link.get('token_id')) != str(row['token_id'])) or
                                  (row.get('site_id') and link.get('site_id') != row['site_id'])):
            link = {}
        for key in ('token_id', 'item_id', 'site_id', 'title', 'permalink', 'thumbnail_url', 'store_name'):
            if not row.get(key):
                row[key] = link.get(key) or ''
        token_id = int(row.get('token_id') or 0)
        if 'group_name' not in row and token_id:
            if token_id not in tokens:
                try:
                    tokens[token_id] = bit_mysql.get_mercado_store_token_summary(token_id) or {}
                except Exception:
                    tokens[token_id] = {}
            row['group_name'] = snapshot(row, tokens[token_id])['group_name']


def list_uploads(*, page=1, limit=20, allowed_token_ids=None, search='', salesperson='',
                 uploaded_from=None, uploaded_before=None, job_id=None):
    from bit.bit_ai_video import _parse_job_datetime
    lower = _parse_job_datetime(uploaded_from, '上传开始时间')
    upper = _parse_job_datetime(uploaded_before, '上传结束时间')
    if lower and upper and lower >= upper:
        raise ValueError('上传时间范围无效')
    page, limit = max(1, int(page)), max(1, min(100, int(limit)))
    historical = _historical_rows()
    with video_reviews.database() as db:
        _schema(db)
        attempts = [json.loads(r['details']) for r in db.execute('SELECT details FROM video_upload_attempts')]
    successes = {}
    failures = {}
    for row in historical + attempts + video_reviews.records():
        if row.get('clip_uuid'):
            key = _identity(row)
            existing = successes.setdefault(key, {})
            existing.update({k: v for k, v in row.items() if v not in (None, '') or k == 'group_name'})
            # Review status is authoritative; accepted attempts are pending moderation.
            if existing.get('status') == 'accepted':
                existing['status'] = 'UNDER_REVIEW'
        else:
            # Merge a manifest attempt with its durable journal entry.
            key = row['attempt_id']
            failures[key] = row
    successful_attempts = {r.get('attempt_id') for r in successes.values() if r.get('attempt_id')}
    rows = list(successes.values()) + [r for r in failures.values()
                                      if r.get('attempt_id') not in successful_attempts]
    # Older failed manifests may contain only a link ID; resolve before scoping.
    _enrich([r for r in rows if not r.get('token_id')])
    if allowed_token_ids is not None:
        allowed = {int(i) for i in allowed_token_ids}
        rows = [r for r in rows if int(r.get('token_id') or 0) in allowed]
    if job_id:
        rows = [r for r in rows if r.get('job_id') == job_id]
    if salesperson:
        rows = [r for r in rows if salesperson.casefold() in str(r.get('salesperson') or '').casefold()]
    if lower or upper:
        filtered = []
        for row in rows:
            try:
                at = _parse_job_datetime(row.get('uploaded_at'), '上传时间')
            except ValueError:
                continue
            if at and (not lower or at >= lower) and (not upper or at < upper):
                filtered.append(row)
        rows = filtered
    if search:
        _enrich(rows)
        needle = search.strip().casefold()
        rows = [r for r in rows if any(needle in str(r.get(k) or '').casefold() for k in
                ('video_name', 'store_name', 'group_name', 'title', 'item_id', 'link_id', 'clip_uuid'))]
    rows.sort(key=lambda r: str(r.get('uploaded_at') or ''), reverse=True)
    total = len(rows)
    pages = max(1, (total + limit - 1) // limit)
    page = min(page, pages)
    selected = rows[(page - 1) * limit:page * limit]
    _enrich(selected)
    for row in selected:
        row['status_label'] = video_reviews.LABELS.get(row.get('status')) or {
            'failed': '上传失败', 'uploading': '正在上传', 'accepted': '已提交待审核',
        }.get(row.get('status'), row.get('status') or '已提交待审核')
    return {'rows': selected, 'total': total, 'page': page, 'page_size': limit, 'total_pages': pages}
